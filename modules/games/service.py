"""游戏资讯监控模块（重返未来：1999 / 明日方舟；已并入套件 astrbot_plugin_suite，装饰器注册面在仓库根 main.py）。

- /游戏 一条裸指令内部分发：AI 要点摘要 / 原始列表 / 卡池 / 兑换码 / 版本 / 订阅 / 帮助
- 定时轮询两家官网：公告变化时后台调 LLM 预生成要点摘要，/游戏 读缓存秒回；
  新兑换码按会话订阅推送
- B站官方动态轮询（兑换码的第二个来源），扫码登录可在 WebUI 页面完成
- 3 个 LLM 工具 + on_llm_request 幕后提示钩子，聊到游戏话题自动查真数据
- 数据源：ak.hypergryph.com（方舟）、re.bluepoch.com（1999）、api.bilibili.com（动态）

⚠️ 为什么用裸 @filter.command("游戏") 而不是 @filter.command_group("游戏")：
AstrBot v4.27 的指令组在只发组名（不带子指令）时会抛 ValueError 并打印指令树，
组内也没有「默认子指令」这回事（@group.command("") 注册出的名字是 "游戏 "，
永远匹配不上）。而本插件的 `/游戏` 无参必须给出两游戏最新资讯，所以只能裸指令
+ 内部路由。详见 docs/ARCHITECTURE.md。
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context

try:
    from . import webui
    from .cache import TTLCache
    from .codes import merge_into, scan_dynamics, scan_item
    from .digest import DigestMixin
    from .formatting import (
        codes_text,
        dynamics_text,
        format_mute_until,
        gacha_lines,
        help_text,
        mute_text,
        news_text,
        push_text,
        scope_text,
        subscribe_text,
        summary_of,
    )
    from .keywords import (
        ACTIVITY_CUES,
        ACTIVITY_KEYWORDS,
        GACHA_KEYWORDS,
        MAINTENANCE_CUES,
        MAINTENANCE_TITLE_RE,
        VERSION_KEYWORDS,
        extract_time_ranges,
        is_subscribe_all,
        is_subscribe_off,
        parse_route,
        pick,
    )
    from .providers import (
        GAMES,
        BilibiliClient,
        HttpClient,
        SourceError,
        enabled_keys,
        game_display,
        normalize_game,
        resolve_uids,
    )
    from .storage import Store
    from .summarizer import (
        DIGEST_WAIT_SECONDS,
        PUSH_DIGEST_WAIT_SECONDS,
    )
except ImportError:  # 兜底：被以非包方式加载时
    import webui  # type: ignore
    from cache import TTLCache  # type: ignore
    from codes import merge_into, scan_dynamics, scan_item  # type: ignore
    from digest import DigestMixin  # type: ignore
    from formatting import (  # type: ignore
        codes_text,
        dynamics_text,
        format_mute_until,
        gacha_lines,
        help_text,
        mute_text,
        news_text,
        push_text,
        scope_text,
        subscribe_text,
        summary_of,
    )
    from keywords import (  # type: ignore
        ACTIVITY_CUES,
        ACTIVITY_KEYWORDS,
        GACHA_KEYWORDS,
        MAINTENANCE_CUES,
        MAINTENANCE_TITLE_RE,
        VERSION_KEYWORDS,
        extract_time_ranges,
        is_subscribe_all,
        is_subscribe_off,
        parse_route,
        pick,
    )
    from providers import (  # type: ignore
        GAMES,
        BilibiliClient,
        HttpClient,
        SourceError,
        enabled_keys,
        game_display,
        normalize_game,
        resolve_uids,
    )
    from storage import Store  # type: ignore
    from summarizer import (  # type: ignore
        DIGEST_WAIT_SECONDS,
        PUSH_DIGEST_WAIT_SECONDS,
    )

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:

    def get_astrbot_data_path() -> str:
        return "data"


PLUGIN_NAME = "astrbot_plugin_games_monitor"

# 套件布局：数据落盘与 Web 路由都用套件名；日志前缀沿用旧插件名便于排查。
SUITE_NAME = "astrbot_plugin_suite"
MODULE_NAME = "games"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 连续失败后的退避上限（秒）。官网凌晨抽风是常态，指数退避到 30 分钟封顶，
# 既不轰炸对方也不让插件彻底停摆。
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 1800.0

# 单次推送的条目上限（配置 max_items_per_push 的兜底）
DEFAULT_MAX_PUSH = 5

# 群聊订阅闸门文案
GROUP_SUBSCRIBE_DENIED = (
    "🔒 群聊里只有管理员能开关订阅推送。\n"
    "想订阅的话，私聊机器人发送 /游戏 订阅 全部；"
    "或者让群管理员来开。"
)

# 群聊静音闸门文案（与订阅同款语义：群推送是公共体验，只有管理员能改）
GROUP_MUTE_DENIED = (
    "🔒 群聊里只有管理员能调整推送静音。\n"
    "想静音的话，私聊机器人发送 /游戏 静音；"
    "或者让群管理员来操作。"
)

# 静音时长上限（小时）：7 天。跟电费插件 ALERT_MUTE_MAX_HOURS 同一口径——
# 静音是「别打扰」不是「永远别告诉我」，可逆且要有上限防手滑填 99999。
MAX_MUTE_HOURS = 168

UNKNOWN_GAME_HINT = (
    "没认出是哪个游戏。可以用：重返未来 / 1999 / 九九 / 明日方舟 / 方舟 / ak。"
)

# —— AI 触达（幕后指令注入）——
# 用户消息或近几轮上下文命中这些词时，才把 AI_GAMES_HINT 追加进 system_prompt。
# 宁宽勿漏：误命中最多多花一轮 token，模型不会乱调工具；漏命中则 AI 想不起来用插件。
AI_HINT_KEYWORDS: tuple[str, ...] = (
    "重返未来", "1999", "九九", "明日方舟", "方舟", "arknights", "re1999",
    "卡池", "寻访", "干员", "神秘学家", "版本更新", "维护", "前瞻",
    "兑换码", "礼包码", "福利码", "游戏", "联动", "活动",
)

AI_GAMES_HINT = (
    "【游戏资讯插件】机器人接有游戏资讯监控插件，覆盖重返未来：1999 与明日方舟"
    "官方公告、活动、版本与 B站动态。用户问游戏资讯、卡池时间、版本更新、"
    "兑换码时：必须调用 games_* 工具获取真实数据，严禁编造公告标题、时间或兑换码。"
    "查公告用 games_news；查兑换码用 games_codes；查 B站官方动态用 games_dynamics。"
    "兑换码只从 B站动态与公告正文自动提取，查不到就如实说没有兑换码QWQ，"
    "不要凭印象编一个。/游戏 指令只是兜底，不要主动让用户记指令。"
)


class GamesCore(DigestMixin):
    """DigestMixin 提供 AI 要点摘要的生成/缓存/落盘（见 digest.py）。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        # 套件版不再继承 Star（super().__init__ 已随转换移除），context 自行保存
        self.context = context
        self.config = config
        self.store: Store | None = None
        self.sources: dict[str, Any] = {}
        self.bili = None
        self.scheduler: AsyncIOScheduler | None = None
        self._tasks: list[asyncio.Task] = []
        # AI 摘要缓存：游戏 key → (公告 id 元组, 摘要文本)。v1.2.0 起落盘
        # （store 的 digests 组），重启时 _restore_digests 装回，公告列表没变
        # 就直接命中，不用等下一轮轮询重新生成。
        self._digest_cache: dict[str, tuple[tuple, str]] = {}
        # 每个游戏最多一个在途生成任务：查询/推送/轮询三方共用，绝不双调 LLM
        self._digest_tasks: dict[str, asyncio.Task] = {}
        # 数据源名 → {"fail": 连续失败次数, "until": 退避截止时间戳}
        self._failure_backoff: dict[str, dict] = {}
        # 广播失败的兑换码队列 v1.2.0 起持久化在 store 的 pending_codes 组
        # （内存 dict 退役）：推送失败也要扛住重启，否则又是「码在库里但永远
        # 不再广播」的 v1.1.1 回归。补发前过滤已过期的码。
        # 查询缓存：news:{key} / cat:{key}:{cat} → 条目列表。60 秒内的重复
        # 提问直接吃缓存，不再打官网（1999 的 EdgeOne 风控扛不住连问）。
        # 轮询每轮都会刷新 news 缓存，命中即最新不超过一个轮询周期。
        self._news_cache = TTLCache(ttl_seconds=float(self._cfg_int("query_cache_seconds", 60)))
        # 最近一次生成二维码的时间，用于本地判过期（省一趟必然失败的请求）
        self._qr_at: float = 0.0

    # ================= 生命周期 =================

    async def initialize(self):
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / SUITE_NAME / MODULE_NAME
        self.store = Store(
            data_dir / "store.json",
            keep_days=self._cfg_int("history_keep_days", 30),
        )
        self.store.prune()
        self._restore_digests()
        self._build_sources()

        self.scheduler = AsyncIOScheduler()
        # 轮询无条件启动：资讯轮询除了推送，还承担 AI 摘要后台预生成与兑换码
        # 入库（公告正文+B站动态都是码源）——关推送的用户查询也要用。
        # 推送资格在发送端按订阅逐会话判断，这里不再按 push_enabled 裁剪任务。
        news_min = self._poll_minutes("poll_interval_minutes", 10, 5, 120)
        self.scheduler.add_job(
            self._poll_news,
            IntervalTrigger(minutes=news_min),
            id="poll_news",
            max_instances=1,
            coalesce=True,
        )
        dyn_min = self._poll_minutes("dynamics_poll_minutes", 15, 5, 120)
        self.scheduler.add_job(
            self._poll_dynamics,
            IntervalTrigger(minutes=dyn_min),
            id="poll_dynamics",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        push_line = (
            f"推送开（资讯={news_min}min 动态={dyn_min}min）"
            if self._cfg_bool("push_enabled", False)
            else "推送关（轮询照跑：AI 摘要预热 + 兑换码入库）"
        )
        logger.info(f"[{PLUGIN_NAME}] 已初始化，{push_line}")
        # 开机预热：不等第一个轮询周期，立刻拉一次资讯把摘要缓存填上，
        # 用户重启后马上发 /游戏 也能秒出要点。
        self._spawn(self._poll_news())
        webui.register(self)

    def _spawn(self, coro) -> asyncio.Task:
        """后台任务统一入口：进 _tasks 名册，terminate 时统一取消。"""
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def terminate(self):
        # 顺序：先停调度再取消任务，最后关连接——反过来会让运行中的轮询在
        # close 的 await 间隙重建一个再没人关的新 client（电费插件踩过）。
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        # 在途摘要任务先取消，避免 gather _tasks 之后还有 LLM 回调写缓存
        for t in self._digest_tasks.values():
            t.cancel()
        self._digest_tasks.clear()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._digest_cache.clear()
        self._news_cache.clear()
        for source in list(self.sources.values()):
            try:
                await source.close()
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 关闭 {type(source).__name__} 失败：{e!r}")
        self.sources.clear()
        if self.bili is not None:
            try:
                await self.bili.close()
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 关闭 B站客户端失败：{e!r}")
            self.bili = None
        if self.store:
            try:
                self.store.save(force=True)
            except OSError as e:
                logger.error(f"[{PLUGIN_NAME}] 退出保存失败：{e}")
        self._failure_backoff.clear()
        # pending 队列与摘要在 store 里持久化，terminate 不清（重启后继续补发/命中）

    # ================= 配置 =================

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except (AttributeError, TypeError):
            return default
        if value is None or value == "":
            return default
        return value

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _poll_minutes(self, key: str, default: int, low: int, high: int) -> int:
        return max(low, min(high, self._cfg_int(key, default)))

    def _max_push(self) -> int:
        return max(1, min(20, self._cfg_int("max_items_per_push", DEFAULT_MAX_PUSH)))

    def _ttl_hours(self) -> int:
        return max(1, self._cfg_int("code_ttl_hint_hours", 24))

    def _ai_enabled(self) -> bool:
        return self._cfg_bool("ai_tools_enabled", True)

    def _enabled_games(self) -> list[str]:
        return enabled_keys(self.config)

    def _build_sources(self) -> None:
        """按配置实例化各游戏数据源 + B站客户端。"""
        timeout = float(self._cfg_int("request_timeout_seconds", 15))
        ua = str(self._cfg("user_agent", DEFAULT_UA))
        for key in self._enabled_games():
            cls = GAMES.get(key)
            if cls is None:
                continue
            self.sources[key] = cls(HttpClient(timeout=timeout, user_agent=ua))
        self.bili = BilibiliClient(
            timeout=timeout,
            user_agent=ua,
            sessdata=str(self.config.get("bilibili_sessdata", "") or ""),
        )

    # ================= 失败退避 =================

    def _should_skip(self, name: str) -> bool:
        state = self._failure_backoff.get(name) or {}
        return float(state.get("until") or 0) > time.time()

    def _note_failure(self, name: str, reason: str) -> None:
        state = self._failure_backoff.setdefault(name, {"fail": 0, "until": 0.0})
        state["fail"] = int(state.get("fail") or 0) + 1
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (state["fail"] - 1)))
        state["until"] = time.time() + delay
        logger.warning(
            f"[{PLUGIN_NAME}] {name} 拉取失败（第 {state['fail']} 次）：{reason}；"
            f"{int(delay // 60) or 1} 分钟内不再重试"
        )

    def _note_success(self, name: str) -> None:
        self._failure_backoff.pop(name, None)

    # ================= 数据获取 =================

    async def _latest(self, key: str, limit: int = 10):
        source = self.sources.get(key)
        if source is None:
            raise SourceError(f"{game_display(key)} 数据源未启用")
        return await source.latest(limit=limit)

    async def _category(self, key: str, category: str, limit: int = 20):
        source = self.sources.get(key)
        if source is None:
            raise SourceError(f"{game_display(key)} 数据源未启用")
        return await source.by_category(category, limit=limit)

    # ---------- 查询缓存包装 ----------

    async def _latest_cached(self, key: str, limit: int):
        """带缓存的最新列表。失败直接向上抛（_safe_call 捕获），不会写入缓存。"""
        ck = f"news:{key}"
        hit = self._news_cache.get(ck, min_items=limit)
        if hit is not None:
            return hit[:limit]
        items = await self._latest(key, limit)
        self._news_cache.put(ck, items)
        return items

    async def _category_cached(self, key: str, category: str, limit: int):
        """带缓存的分类列表。卡池/版本视图与 AI 工具共用同一份。"""
        ck = f"cat:{key}:{category}"
        hit = self._news_cache.get(ck, min_items=limit)
        if hit is not None:
            return hit[:limit]
        items = await self._category(key, category, limit=limit)
        self._news_cache.put(ck, items)
        return items

    async def _safe_call(self, name: str, coro_factory):
        """统一的数据源调用包装：失败记日志+退避并返回 None，绝不让一条源拖垮整条指令。"""
        if self._should_skip(name):
            return None
        try:
            result = await coro_factory()
        except SourceError as e:
            self._note_failure(name, str(e))
            return None
        except Exception as e:
            self._note_failure(name, f"{type(e).__name__}: {e}")
            return None
        self._note_success(name)
        return result

    # ================= 指令 =================

    async def cmd_games(self, event: AstrMessageEvent):
        """游戏资讯监控：/游戏、/游戏 1999、/游戏 1999 卡池、/游戏 订阅 全部。"""
        sub, rest = parse_route(self._arg_text(event))
        if sub == "帮助":
            yield event.plain_result(self._help_text())
            return
        if sub == "订阅":
            yield event.plain_result(await self._handle_subscribe(event, rest))
            return
        if sub == "静音":
            yield event.plain_result(await self._handle_mute(event, rest))
            return
        if sub == "总结":
            # 显式要摘要——与默认视图同路（默认就是 AI 摘要），留给肌肉记忆
            yield event.plain_result(await self._news_view(rest))
            return
        if sub == "列表":
            # 逃生口：不想要 AI 摘要时看原始列表（同样不带链接）
            yield event.plain_result(await self._news_view(rest, raw=True))
            return
        if sub == "卡池":
            yield event.plain_result(await self._gacha_view(rest))
            return
        if sub == "兑换":
            yield event.plain_result(await self._codes_view(rest))
            return
        if sub == "版本":
            yield event.plain_result(await self._version_view(rest))
            return
        yield event.plain_result(await self._news_view(rest))

    def _arg_text(self, event) -> str:
        """取掉指令名后的参数部分。

        裸指令的参数是按空格切分后按位置绑定的（「1999 卡池」会被切成两个
        独立参数），所以这里直接读原始消息串自己切，不用框架的参数绑定。
        """
        raw = ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                raw = str(getter() or "")
            except Exception:
                raw = ""
        if not raw:
            raw = str(getattr(event, "message_str", "") or "")
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            return ""
        # 第一段一定是指令名（宿主就是按它匹配上的），后面才是参数
        return raw.partition(" ")[2].strip()

    def _help_text(self) -> str:
        return help_text(
            self._enabled_games(),
            self._poll_minutes("poll_interval_minutes", 10, 5, 120),
            self._poll_minutes("dynamics_poll_minutes", 15, 5, 120),
            self._cfg_bool("push_enabled", False),
        )

    def _target_games(self, rest: str) -> tuple[list[str], str | None]:
        """把用户写的游戏名解析成目标列表；认不出返回 (全部, 原文) 交上层提示。"""
        rest = (rest or "").strip()
        if not rest:
            return self._enabled_games(), None
        key = normalize_game(rest)
        if key:
            if key in self._enabled_games():
                return [key], None
            return [], key  # 认得出但没启用
        return [], None

    async def _news_view(self, rest: str, raw: bool = False) -> str:
        """默认视图 = AI 要点摘要（后台已预生成，这里只读缓存）。

        raw=True 时跳过摘要直接出原始列表（/游戏 列表）。
        摘要不可用（未开/没配模型/还在生成/生成失败）时自动退回原始列表，
        绝不让指令挂掉或返回空。
        """
        games, unknown = self._target_games(rest)
        if not games:
            if unknown:
                return f"⚠️ {game_display(unknown)} 的数据源当前是关闭的，可以在插件配置里打开。"
            return UNKNOWN_GAME_HINT + "\n\n" + self._help_text()
        push_n = self._max_push()
        # 取数必须覆盖摘要窗口（max_items）：轮询那边拿 15 条、这里只拿 5 条的话，
        # 两边的 cache_key（最新 N 条 id）对不上，缓存永远 miss、LLM 被重复调用——
        # 真跑渲染抓到的第二个 key 不一致 bug。展示仍按 push_n 截断。
        fetch_n = max(push_n, self._digest_max_items())
        blocks, errors, fallback = [], [], False
        # 两游戏并行拉（各自走缓存 + 退避，互不拖累）：串行时一个源慢 2 秒，
        # 总延迟就是 4 秒起
        async def _fetch(key: str):
            return await self._safe_call(
                f"news:{key}", lambda k=key, n=fetch_n: self._latest_cached(k, n)
            )

        fetched = await asyncio.gather(*(_fetch(k) for k in games))
        for key, items in zip(games, fetched, strict=True):
            if items is None:
                errors.append(game_display(key))
                continue
            items.sort(key=lambda i: i.published, reverse=True)
            digest = None
            if not raw:
                digest = await self._ensure_digest(key, items, DIGEST_WAIT_SECONDS)
            if digest:
                blocks.append(f"🎮 {game_display(key)} 资讯要点\n\n{digest}")
            else:
                if not raw:
                    fallback = True
                blocks.append(news_text(items[:push_n], f"🎮 {game_display(key)} 最新资讯"))
        text = self._join_blocks(blocks, errors)
        if fallback:
            text += (
                "\n\nℹ️ AI 摘要还没备好（刚启动/模型不可用/正在生成），先看原始列表；"
                "稍后再发一次 /游戏 就是要点版。"
            )
        return text

    async def _gacha_view(self, rest: str) -> str:
        games, unknown = self._target_games(rest)
        if not games:
            return (
                f"⚠️ {game_display(unknown)} 的数据源当前是关闭的。"
                if unknown
                else UNKNOWN_GAME_HINT
            )
        blocks, errors = [], []
        # 两游戏并行拉（缓存同前）：卡池关键词本就稀疏，别让串行请求雪上加霜
        async def _fetch_cat(key: str, cat: str):
            return await self._safe_call(
                f"news:{key}", lambda k=key, c=cat: self._category_cached(k, c, limit=30)
            )

        fetched = await asyncio.gather(*(_fetch_cat(k, "最新") for k in games))
        for key, items in zip(games, fetched, strict=True):
            if items is None:
                errors.append(game_display(key))
                continue
            hits = pick(items, GACHA_KEYWORDS, limit=self._max_push())
            if hits:
                ranges = {
                    i.item_id: extract_time_ranges(i.text_blob, cues=ACTIVITY_CUES)
                    for i in hits
                }
                body = "\n".join([""] + gacha_lines(hits, ranges))
                blocks.append(f"🎴 {game_display(key)} 卡池\n{body}")
                continue
            # 卡池公告本来就稀疏（两三个月才一波），死守关键词会长期空态。
            # 退一步给最近的限时活动 + 抽到的时间段，用户真正想问的其实是「现在有什么活动」。
            blocks.append(self._recent_activities(key, items))
        return self._join_blocks(blocks, errors)

    def _recent_activities(self, key: str, items: list) -> str:
        """卡池关键词没命中时的兜底：最近的限时活动 + 活动时间段。"""
        # 维护/停机公告归 /游戏 X 版本 管，别混进活动视图——
        # 实测「4.0版本更新维护公告」会因为排在最新而挤掉真正的活动。
        pool = [i for i in items if not MAINTENANCE_TITLE_RE.search(i.title)]
        acts = pick(pool, ACTIVITY_KEYWORDS, limit=self._max_push())
        head = f"🎴 {game_display(key)} 卡池"
        if not acts:
            return (
                f"{head}\n\n最近既没有卡池公告，也没有限时活动公告。\n"
                "（游戏的卡池/活动公告本来就稀疏，等下一次版本前瞻）"
            )
        ranges = {
            i.item_id: extract_time_ranges(i.text_blob, cues=ACTIVITY_CUES) for i in acts
        }
        note = "\n\n（最近没看到卡池相关的公告，先给你最近的活动；卡池一般随版本前瞻公布）"
        return head + note + "\n" + "\n".join([""] + gacha_lines(acts, ranges))

    async def _version_view(self, rest: str) -> str:
        games, unknown = self._target_games(rest)
        if not games:
            return (
                f"⚠️ {game_display(unknown)} 的数据源当前是关闭的。"
                if unknown
                else UNKNOWN_GAME_HINT
            )
        blocks, errors = [], []
        async def _fetch_ver(key: str):
            return await self._safe_call(
                f"news:{key}", lambda k=key: self._category_cached(k, "公告", limit=30)
            )

        fetched = await asyncio.gather(*(_fetch_ver(k) for k in games))
        for key, items in zip(games, fetched, strict=True):
            if items is None:
                errors.append(game_display(key))
                continue
            hits = pick(items, VERSION_KEYWORDS, limit=self._max_push())
            if not hits:
                blocks.append(f"🛠 {game_display(key)} 版本\n\n最近没有相关的版本公告。")
                continue
            ranges = {
                i.item_id: extract_time_ranges(i.text_blob, cues=MAINTENANCE_CUES)
                for i in hits
            }
            blocks.append(
                f"🛠 {game_display(key)} 版本与维护\n\n" + "\n".join(gacha_lines(hits, ranges))
            )
        return self._join_blocks(blocks, errors)

    async def _codes_view(self, rest: str) -> str:
        games, unknown = self._target_games(rest)
        if not games:
            return (
                f"⚠️ {game_display(unknown)} 的数据源当前是关闭的。"
                if unknown
                else UNKNOWN_GAME_HINT
            )
        # 方舟没有兑换码体系（补偿走游戏内邮件），空态是数据源现实而非故障
        blocks = [
            codes_text(self.store.codes(key) if self.store else [], game_display(key),
                       self._ttl_hours())
            for key in games
        ]
        return "\n\n".join(blocks)

    def _join_blocks(self, blocks: list[str], errors: list[str]) -> str:
        text = "\n\n".join(b for b in blocks if b)
        if errors:
            tip = "、".join(errors)
            text += (
                f"\n\n⚠️ {tip} 暂时拉不到（官网无响应或被风控），稍后再试。"
                f"其他功能不受影响。"
            )
        return text or "暂时没有查到相关资讯，稍后再试试。"

    # ================= 订阅 =================

    def _is_private(self, event) -> bool:
        fn = getattr(event, "is_private_chat", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    def _is_admin(self, event) -> bool:
        """群管理员判定。宿主只在 MessageEvent 上挂 is_admin()，取不到就放行。"""
        fn = getattr(event, "is_admin", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
        return True

    async def _handle_subscribe(self, event, rest: str) -> str:
        if not self.store:
            return "存储还没初始化，稍后再试。"
        umo = event.unified_msg_origin
        rest = (rest or "").strip()

        if not rest:
            return scope_text(self.store.subscription(umo))

        if not self._cfg_bool("push_enabled", False):
            return (
                "⚠️ 插件配置的「启用群订阅推送」当前是关闭的，开了才能收推送。\n"
                "查询功能不受影响，随时可以问我。"
            )

        if not self._is_private(event) and not self._is_admin(event):
            return GROUP_SUBSCRIBE_DENIED

        if is_subscribe_off(rest):
            if self.store.del_subscription(umo):
                self.store.save(force=True)
                return subscribe_text("已关闭本会话推送", "以后有新公告我就不再打扰你了。")
            return subscribe_text("本会话本来就没订阅", "想开的话：/游戏 订阅 全部")

        if is_subscribe_all(rest):
            games = self._enabled_games()
            self.store.set_subscription(umo, games, True)
            self.store.save(force=True)
            return subscribe_text(
                "已订阅全部游戏的推送",
                "· " + "、".join(game_display(g) for g in games)
                + "\n· B站官方动态：有新帖就推给你\n· 新兑换码也会单独提醒\n\n"
                + "取消：/游戏 订阅 关闭",
            )

        key = normalize_game(rest)
        if not key:
            return (
                f"没认出「{rest}」。可用：重返未来 / 1999 / 九九 / 明日方舟 / 方舟 / ak，"
                "或 /游戏 订阅 全部、/游戏 订阅 关闭。"
            )
        if key not in self._enabled_games():
            return f"⚠️ {game_display(key)} 的数据源当前是关闭的。"

        sub = dict(self.store.subscription(umo))
        games = list(sub.get("games") or [])
        if key not in games:
            games.append(key)
        self.store.set_subscription(umo, games, bool(sub.get("dynamics") or True))
        self.store.save(force=True)
        return subscribe_text(
            f"已订阅 {game_display(key)}",
            f"当前订阅：{'、'.join(game_display(g) for g in games)}\n取消：/游戏 订阅 关闭",
        )

    # ================= 轮询推送 =================

    async def _send(self, umo: str, text: str) -> bool:
        """发送一条消息。返回是否真的送达——失败不记账，下轮重发。"""
        if not text or not self.context:
            return False
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 推送到 {umo} 失败：{e!r}")
            return False
        return bool(ok)

    # 单游戏 pending 补发上限：码本身有 TTL（默认 24h），正常远到不了这个数；
    # 防御性硬顶，超出丢最老的（码库里有，/游戏 兑换 还能查到）。
    MAX_PENDING_CODES_PER_GAME = 20

    # ================= 静音（v1.2.0） =================

    async def _handle_mute(self, event, rest: str) -> str:
        """推送静音：无参=看状态；N=静音 N 小时；0/解除=恢复。群聊仅管理员。

        静音的作用面 = 三类推送全体（新公告/新动态/新兑换码），查询不受影响；
        静音期间错过的新公告不补发（v1.1.1 起 mark_seen 语义），解除后
        想看直接 /游戏——「别打扰」不是「替你攒着」。
        """
        if not self.store:
            return "存储还没初始化，稍后再试。"
        umo = event.unified_msg_origin
        rest = (rest or "").strip()

        if not rest:
            # 纯状态查询不设闸门：谁都能看本群现在静音到几点
            until = self.store.muted_until(umo)
            if until <= time.time():
                return mute_text(
                    "本会话推送未静音",
                    "静音一段时间：/游戏 静音 2（小时）\n恢复推送：/游戏 静音 解除",
                )
            return mute_text(
                f"推送已静音至 {format_mute_until(until)}",
                "期间新公告/动态/兑换码都不会推送（查询不受影响）。\n恢复推送：/游戏 静音 解除",
            )

        if not self._is_private(event) and not self._is_admin(event):
            return GROUP_MUTE_DENIED

        if is_subscribe_off(rest) or rest in ("0", "解除", "恢复"):
            prev = self.store.clear_muted(umo)
            if prev > 0:
                self.store.save(force=True)
                return mute_text("已恢复推送", "有新公告/动态/兑换码会继续提醒你。")
            return mute_text("本来就没静音", "推送一直是开着的。")

        # N 小时（支持「2」「2小时」「2.5」）
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:小时|h|H|hours?)?", rest)
        if not m:
            return (
                f"没认出「{rest}」。用法：/游戏 静音 2（静音 2 小时）、"
                "/游戏 静音 解除（恢复）、/游戏 静音（看状态）。"
            )
        hours = float(m.group(1))
        # 「0」在上面已按解除处理，到这里必然是正数
        hours = min(hours, MAX_MUTE_HOURS)
        until = self.store.set_muted(umo, hours)
        self.store.save(force=True)
        return mute_text(
            f"已静音 {hours:g} 小时（至 {format_mute_until(until)}）",
            "期间新公告/动态/兑换码都不推送，查询不受影响。\n提前恢复：/游戏 静音 解除",
        )

    async def _poll_news(self) -> None:
        for key in self._enabled_games():
            if not self.store:
                break
            # 逐源隔离（电费 v1.2.0 同款教训）：一个游戏的入库/推送/落盘炸了，
            # 不能把整轮轮询带崩——后面游戏当轮不拉，摘要预热与码入库全部停摆。
            try:
                limit = max(self._max_push() * 3, 15)
                items = await self._safe_call(
                    f"news:{key}", lambda k=key, n=limit: self._latest_cached(k, n)
                )
                if not items:
                    continue
                # 公告正文兜底扫码要在这批条目记已读之前做：码是新信息，不能因为
                # 推送失败而被跳过（seen 回滚只影响推送，不影响入库）。
                # ⚠️ 没有订阅者也要扫——/游戏 兑换 的公告码源全靠这里，与推送无关。
                await self._push_codes(key, await self._absorb_codes(key, items))
                # 后台摘要预生成：spawn 后立即继续，绝不阻塞轮询周期
                self._maybe_refresh_digest(key, items)
                fresh = await self._new_items(key, items)
                if fresh and self.store.subscribers(key):
                    await self._push_news(key, fresh, items)
            except Exception as e:
                logger.error(
                    f"[{PLUGIN_NAME}] {game_display(key)} 资讯轮询异常（本轮跳过，"
                    f"下轮继续）：{type(e).__name__}: {e!r}"
                )

    async def _poll_dynamics(self) -> None:
        if not (self.store and self.bili):
            return
        uids = resolve_uids(self.config)
        wanted = {
            umo: sub
            for umo, sub in self.store.subscriptions().items()
            if isinstance(sub, dict) and sub.get("dynamics")
        }
        if not wanted:
            return
        for key in self._enabled_games():
            if not any(key in (sub.get("games") or []) for sub in wanted.values()):
                continue
            source = f"bili:{key}"
            if self._should_skip(source):
                continue
            # 逐源隔离：B站风控/入库异常只废自己这一路，不拖累其他游戏
            try:
                dyns = await self.bili.dynamics.space_feed(
                    uids.get(key, ""), key, limit=max(self._max_push() * 3, 15)
                )
            except SourceError as e:
                self._note_failure(source, str(e))
                continue
            except Exception as e:
                self._note_failure(source, f"{type(e).__name__}: {e}")
                continue
            self._note_success(source)
            try:
                fresh = await self._new_dynamics(key, dyns)
                await self._push_codes(key, await self._absorb_dyn_codes(key, dyns))
                if not fresh:
                    continue
                body = dynamics_text(fresh[: self._max_push()], game_display(key))
                delivery_failed = False
                for umo, sub in wanted.items():
                    if key not in (sub.get("games") or []):
                        continue
                    if self.store.is_muted(umo):
                        continue
                    if not await self._send(umo, f"🆕 {body}"):
                        delivery_failed = True
                if not delivery_failed:
                    # 全部送达才记已读；有失败就不记，下轮整批重发（宁重发不漏推）
                    self.store.mark_seen(source, [d.item_id for d in fresh])
                    self.store.save()
            except Exception as e:
                logger.error(
                    f"[{PLUGIN_NAME}] {game_display(key)} 动态轮询异常（本轮跳过，"
                    f"下轮继续）：{type(e).__name__}: {e!r}"
                )

    async def _push_codes(self, key: str, new_codes: list[dict]) -> None:
        """广播新码 + 补发上轮失败的；仍失败的留在 store 的 pending 队列，下轮再来。

        红线「发送成功才记账」：入库（absorb）只是登记，广播送达才算数——
        旧实现入库后推送失败就再也不推，用户永远错过这个码。

        v1.2.0 起 pending 队列持久化（store 的 pending_codes 组）：推送失败
        也要扛住重启，否则崩溃后码就「在库里但永远不再广播」。补发前过滤
        已过期的码（过期了再推就是骚扰）。队列变化才写盘且用 force：
        广播失败是稀有事件，但丢了就是永久漏推，去抖窗口吞不得；
        无事发生时零 IO（本方法每轮轮询都会被调）。
        """
        if not self.store:
            return
        now = time.time()
        stored = self.store.pending_codes(key)

        def _alive(rec: dict) -> bool:
            try:
                return float(rec.get("expire_at") or 0) > now
            except (TypeError, ValueError):
                return True  # 过期时间认不出按未过期处理：宁推勿漏

        pending = [r for r in stored if _alive(r)]
        expired_dropped = len(pending) != len(stored)
        codes = list(new_codes)
        if pending:
            seen = {str(r.get("code")) for r in codes}
            codes.extend(r for r in pending if str(r.get("code")) not in seen)
        failed = await self._broadcast_codes(key, codes)
        after = failed[: self.MAX_PENDING_CODES_PER_GAME]
        if expired_dropped or after != pending:
            self.store.set_pending_codes(key, after)
            self.store.save(force=True)

    async def _broadcast_codes(self, key: str, codes: list[dict]) -> list[dict]:
        """把码广播给订阅了该游戏的会话。返回**未成功送达**的码。

        - 无活跃订阅者（没订阅/全在静音）→ 返回 []：不算失败，码在库里，
          /游戏 兑换 随时可查；静音是「别打扰」，不是「永远别告诉我」
        - 有订阅者但任一发送失败 → 整批返回（下轮补发；已收到的重发一次，
          宁重发不漏推——漏的那个会话才是真损失）
        """
        if not codes or not self.store:
            return []
        subscribers = [
            (umo, _sub)
            for umo, _sub in self.store.subscribers(key)
            if not self.store.is_muted(umo)
        ]
        if not subscribers:
            return []
        body = codes_text(codes, game_display(key), self._ttl_hours())
        all_ok = True
        for umo, _sub in subscribers:
            if not await self._send(umo, f"🎁 {game_display(key)} 有新兑换码\n\n{body}"):
                all_ok = False
        return [] if all_ok else list(codes)

    async def _new_items(self, key: str, items) -> list:
        if not self.store:
            return []
        source = f"news:{key}"
        seen = self.store.seen_ids(source)
        fresh = [i for i in items if i.item_id not in seen]
        return sorted(fresh, key=lambda i: i.published, reverse=True)

    async def _new_dynamics(self, key: str, dyns) -> list:
        if not self.store:
            return []
        source = f"bili:{key}"
        seen = self.store.seen_ids(source)
        fresh = [d for d in dyns if d.item_id not in seen]
        return sorted(fresh, key=lambda d: d.published, reverse=True)

    async def _push_news(self, key: str, fresh: list, items: list | None = None) -> None:
        """把新条目推给订阅了该游戏的每个会话，全部送达后才记已读。

        ⚠️ 发送失败就整批不记账（电费插件的教训）。宁可下一轮给已收到的人重发一次，
        也不能让没收到的人永远收不到——漏推比重复糟糕得多。

        推送正文优先用 AI 摘要（用户拍板：推送也总结）。摘要基于**全量最新列表**
        而不是 fresh 切片——新公告本来就在最前面，且与查询缓存共用同一份，
        不会为推送单独多调一次 LLM。等不到摘要就退原始列表（已去链接）。
        """
        if not self.store:
            return
        source = f"news:{key}"
        batch = fresh[: self._max_push()]
        digest = None
        if items:
            digest = await self._ensure_digest(key, items, PUSH_DIGEST_WAIT_SECONDS)
        if digest:
            body = f"🆕 {game_display(key)} 资讯要点\n\n{digest}"
        else:
            body = f"🆕 {game_display(key)} 有新公告\n\n{push_text(batch)}"
        all_ok = True
        for umo, _sub in self.store.subscribers(key):
            if self.store.is_muted(umo):
                continue
            if not await self._send(umo, body):
                all_ok = False
        if all_ok:
            self.store.mark_seen(source, [i.item_id for i in batch])
        self.store.save()

    async def _absorb_codes(self, key: str, items) -> list[dict]:
        """扫公告正文里的兑换码并入库（B站风控挂掉时的兜底来源）。"""
        if not self.store:
            return []
        now = time.time()
        ttl = self._ttl_hours()
        merged = merge_into(self.store.codes(key), [], now, ttl)
        hits = []
        for item in items:
            hits.extend(scan_item(item))
        before = {str(r.get("code")) for r in merged}
        merged = merge_into(merged, hits, now, ttl)
        self.store.set_codes(key, merged)
        new = [r for r in merged if str(r.get("code")) not in before]
        if new:
            # 新码入库即刻持久化且 force：去抖窗口内的第二次 save 会被吞，
            # 崩溃/重启就丢码（本测试当场抓到）；新码是稀有事件，多一次
            # fsync 毫无压力。没有新码就不写（扫正文每轮都跑，别浪费 IO）。
            self.store.save(force=True)
        return new

    async def _absorb_dyn_codes(self, key: str, dyns) -> list[dict]:
        if not self.store:
            return []
        now = time.time()
        ttl = self._ttl_hours()
        merged = merge_into(self.store.codes(key), [], now, ttl)
        hits = []
        for dyn in dyns:
            hits.extend(scan_dynamics(dyn))
        before = {str(r.get("code")) for r in merged}
        merged = merge_into(merged, hits, now, ttl)
        self.store.set_codes(key, merged)
        new = [r for r in merged if str(r.get("code")) not in before]
        if new:
            self.store.save(force=True)
        return new

    # ================= AI 幕后提示 =================

    @staticmethod
    def _collect_hint_texts(event, req) -> list[str]:
        """关键词扫描文本源：本轮消息 + prompt + 最近几轮上下文。"""
        texts: list[str] = []
        msg = str(getattr(event, "message_str", "") or "")
        if msg:
            texts.append(msg)
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and prompt:
            texts.append(prompt)
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            for item in contexts[-6:]:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, str) and content:
                    texts.append(content)
                elif isinstance(content, list):
                    # 多模态 content：[{"type": "text", "text": …}, …]
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
        return texts

    def _hit_games_topic(self, event, req) -> bool:
        return any(
            kw.lower() in text.lower()
            for text in self._collect_hint_texts(event, req)
            for kw in AI_HINT_KEYWORDS
        )

    async def inject_games_hint(self, event: AstrMessageEvent, req) -> None:
        """聊到游戏话题时在 system_prompt 末尾追加幕后提示（幂等 + 全包 try）。"""
        try:
            if not self._ai_enabled() or not self._cfg_bool("ai_prompt_hint", True):
                return
            if not self._hit_games_topic(event, req):
                return
            prompt = getattr(req, "system_prompt", None)
            if not isinstance(prompt, str):
                prompt = ""
            if AI_GAMES_HINT in prompt:
                return
            req.system_prompt = (prompt.rstrip() + "\n" + AI_GAMES_HINT).strip()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 幕后提示注入失败：{e!r}")

    # ================= AI 对话工具 =================
    # 返回值语义（AstrBot v4.27 astr_agent_tool_exec._execute_local）：
    #   return str  → 作为工具结果喂给 LLM ✅
    #   yield event.plain_result() → 直接发给用户，LLM 收不到 ❌
    # 三个工具都是只读查询，一律 return。

    async def tool_games_news(
        self,
        event: AstrMessageEvent,
        game: str = "",
        category: str = "",
        keyword: str = "",
    ) -> str:
        """查《重返未来：1999》《明日方舟》的官方资讯（公告/活动/新闻）。

        用户问「1999 最近有什么活动」「方舟这周更新了什么」「有没有新前瞻」时用这个。
        keyword 可以填「卡池」「版本」「维护」等词做进一步筛选。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
            category(string): 分类，可选「最新」「公告」「活动」「新闻」。留空按「最新」。
            keyword(string): 额外关键词筛选，例如 卡池/版本/前瞻/维护。留空不过滤。
        """
        if not self._ai_enabled():
            return "游戏 AI 工具已被插件配置关闭。"
        games = self._resolve_ai_games(game)
        if not games:
            return UNKNOWN_GAME_HINT
        cat = (category or "").strip() or "最新"
        if cat not in ("最新", "公告", "活动", "新闻"):
            return f"分类只能是：最新/公告/活动/新闻（收到「{cat}」）。"

        out, errors = [], []
        limit = self._max_push()

        async def _fetch(key: str):
            return await self._safe_call(
                f"news:{key}", lambda k=key, c=cat: self._category_cached(k, c, limit=30)
            )

        fetched = await asyncio.gather(*(_fetch(k) for k in games))
        for key, items in zip(games, fetched, strict=True):
            if items is None:
                errors.append(game_display(key))
                continue
            items.sort(key=lambda i: i.published, reverse=True)
            if keyword:
                kw = str(keyword).strip().lower()
                items = [i for i in items if kw in i.text_blob.lower()]
            if not items:
                continue
            out.append(f"【{game_display(key)}】")
            out.extend(summary_of(i) for i in items[:limit])
        if errors:
            out.append(f"（{'、'.join(errors)}暂时拉不到，官网无响应或被风控）")
        if not out:
            return "没有查到相关资讯。可以换个关键词，或试试 /游戏 帮助。"
        return "\n\n".join(out)

    async def tool_games_codes(self, event: AstrMessageEvent, game: str = "") -> str:
        """查游戏兑换码。

        用户问「1999 兑换码」「方舟有兑换码吗」「有没有礼包码」时用这个。
        兑换码由插件从 B站官方动态与公告正文自动提取，查不到就是真的没有，
        不要自己编一个码给用户。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
        """
        if not self._ai_enabled():
            return "游戏 AI 工具已被插件配置关闭。"
        games = self._resolve_ai_games(game)
        if not games:
            return UNKNOWN_GAME_HINT
        blocks = [
            codes_text(self.store.codes(key) if self.store else [], game_display(key),
                       self._ttl_hours())
            for key in games
        ]
        return "\n\n".join(blocks)

    async def tool_games_dynamics(self, event: AstrMessageEvent, game: str = "") -> str:
        """查 B站官方号的最新动态。

        用户问「1999 B站发什么了吗」「方舟官方号最近说了啥」时用这个。
        动态是兑换码的来源之一，直播预告通常也在这里。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
        """
        if not self._ai_enabled():
            return "游戏 AI 工具已被插件配置关闭。"
        if not self.bili:
            return "B站客户端还没初始化，稍后再试。"
        games = self._resolve_ai_games(game)
        if not games:
            return UNKNOWN_GAME_HINT
        uids = resolve_uids(self.config)
        blocks, errors = [], []
        for key in games:
            source = f"bili:{key}"
            dyns = await self._safe_call(
                source,
                lambda k=key: self.bili.dynamics.space_feed(uids.get(k, ""), k, limit=8),
            )
            if dyns is None:
                errors.append(game_display(key))
                continue
            dyns.sort(key=lambda d: d.published, reverse=True)
            blocks.append(dynamics_text(dyns[:5], game_display(key)))
        if errors:
            blocks.append(f"（{'、'.join(errors)}的 B站动态暂时拉不到，可能触发了风控）")
        return "\n\n".join(b for b in blocks if b)

    def _resolve_ai_games(self, game: str) -> list[str]:
        text = str(game or "").strip()
        if not text:
            return self._enabled_games()
        key = normalize_game(text)
        if key:
            return [key] if key in self._enabled_games() else []
        # 认不出就当"两游戏都要"，比回一句"没认出"对模型更友好
        return self._enabled_games()

    # ================= WebUI 端点 =================
    # 薄封装：真正的编排在 webui.py（纯逻辑，便于单测），这里只做属性转发。

    async def _web_generate(self) -> dict:
        return await webui.generate(self)

    async def _web_poll(self) -> dict:
        return await webui.poll(self)
