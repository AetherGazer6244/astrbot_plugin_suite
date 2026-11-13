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
    from .archive import (
        ARCHIVE_SOURCE_BILI_DYN,
        ARCHIVE_SOURCE_BILI_VIDEO,
        ARCHIVE_SOURCE_OFFICIAL,
        ArchiveStore,
    )
    from .cache import TTLCache
    from .codes import merge_into, scan_dynamics, scan_item
    from .digest import DigestMixin
    from .formatting import (
        codes_text,
        dynamics_text,
        fmt_date,
        format_mute_until,
        gacha_lines,
        help_text,
        mute_text,
        news_text,
        one_line,
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
        NewsItem,
        SourceError,
        enabled_keys,
        game_display,
        normalize_game,
        resolve_uids,
        strip_html,
    )
    from .storage import Store
    from .summarizer import (
        DIGEST_WAIT_SECONDS,
        PUSH_DIGEST_WAIT_SECONDS,
    )
except ImportError:  # 兜底：被以非包方式加载时
    import webui  # type: ignore
    from archive import (  # type: ignore
        ARCHIVE_SOURCE_BILI_DYN,
        ARCHIVE_SOURCE_BILI_VIDEO,
        ARCHIVE_SOURCE_OFFICIAL,
        ArchiveStore,
    )
    from cache import TTLCache  # type: ignore
    from codes import merge_into, scan_dynamics, scan_item  # type: ignore
    from digest import DigestMixin  # type: ignore
    from formatting import (  # type: ignore
        codes_text,
        dynamics_text,
        fmt_date,
        format_mute_until,
        gacha_lines,
        help_text,
        mute_text,
        news_text,
        one_line,
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
        NewsItem,
        SourceError,
        enabled_keys,
        game_display,
        normalize_game,
        resolve_uids,
        strip_html,
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

# —— 资讯档案库（v1.3.0，用户选「深」档）——
# 深抓翻的分类：公告/活动/新闻（「最新」是官网给的混合小窗，轮询每 10 分钟
# 已经在拉了，深抓不重复覆盖）。
DEEP_CATEGORIES: tuple[str, ...] = ("公告", "活动", "新闻")
# 档案低于这个条数就判定「还没回填过」，initialize 后台补全量
ARCHIVE_BACKFILL_THRESHOLD = 50
# 每轮深抓最多翻几页（游标推进的节流阀）：一页方舟 6 条 / 1999 8 条
DEEP_PAGES_PER_RUN = 3
# 回填时方舟单分类内的并发页数（官网无风控直连；1999 必须**串行**防 EdgeOne）
BACKFILL_PARALLEL_ARK = 6
# 安全帽：单个分类最多翻多少页（防官网改行为导致无限翻）
MAX_PAGES_PER_CATEGORY = 400
# 档案新鲜度兜底：库内最新比 2×轮询间隔还旧 → 查询前先探一次官网
ARCHIVE_STALE_FACTOR = 2

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
        # 资讯档案库（v1.3.0）：全量历史公告落盘，查询走本地不打官网
        self.archive: ArchiveStore | None = None
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

        # 资讯档案库（v1.3.0）：独立文件，查询走本地；坏文件隔离、原子写+fsync
        if self._cfg_bool("archive_enabled", True):
            archive_dir = Path(get_astrbot_data_path()) / "plugin_data" / SUITE_NAME / MODULE_NAME
            self.archive = ArchiveStore(
                archive_dir / "archive.json",
                keep_days=self._cfg_int("archive_keep_days", 365),
                max_per_game=self._cfg_int("archive_max_per_game", 1500),
            )
            self.archive.prune()
            self.archive.save(force=True)

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
        if self.archive:
            self.scheduler.add_job(
                self._deep_archive,
                IntervalTrigger(minutes=self._poll_minutes("archive_deep_minutes", 60, 30, 720)),
                id="deep_archive",
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
        # 深档首启回填：档案还空就后台全量翻页（方舟并发、1999 串行防风控），
        # 30~60 秒量级，不阻塞启动；中断有游标，重启续走。
        if self.archive and self.archive.count() < ARCHIVE_BACKFILL_THRESHOLD:
            self._spawn(self._backfill_archive())
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

        v1.3.0 起数据源是**本地档案库**：秒回、离线可用、官网风控也不怕；
        库空/太旧时自动探一次官网（_archive_items 的新鲜度兜底）。
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

        async def _fetch(key: str):
            return await self._archive_items(key, fetch_n)

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
                # 真实度三件套②：摘要下方附原文标题清单——模型编造无所遁形，
                # 用户拿标题去官网一搜就能对质。
                titles = "／".join(one_line(i.title, 28) for i in items[: self._digest_max_items()])
                blocks.append(
                    f"🎮 {game_display(key)} 资讯要点\n\n{digest}\n\n📌 摘要依据原文：{titles}"
                )
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
        # v1.3.0：数据源 = 本地档案库（新鲜度兜底在 _archive_items 里）。
        # 不限分类：卡池词条可能出现在公告或活动里，pick 负责过滤。
        async def _fetch_cat(key: str, cat: str):
            return await self._archive_items(key, 30, category=cat)

        fetched = await asyncio.gather(*(_fetch_cat(k, None) for k in games))
        for key, items in zip(games, fetched, strict=True):
            if items is None:
                errors.append(game_display(key))
                continue
            hits = pick(items, GACHA_KEYWORDS, limit=self._max_push())
            if hits:
                ranges = {
                    i.item_id: extract_time_ranges(i.text_blob, cues=ACTIVITY_CUES, published=i.published)
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
            i.item_id: extract_time_ranges(i.text_blob, cues=ACTIVITY_CUES, published=i.published)
            for i in acts
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
        # v1.3.0：数据源 = 本地档案库，公告分类直查
        async def _fetch_ver(key: str):
            return await self._archive_items(key, 30, category="公告")

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
                i.item_id: extract_time_ranges(i.text_blob, cues=MAINTENANCE_CUES, published=i.published)
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

    # ================= 资讯档案库（v1.3.0） =================
    # 用户两条需求的落点：「信息渠道少/信息少」= 每个渠道只取了第一页（方舟
    # 918 条只看到 6 条）；「定时抓取保存、快速调用」= 全量落本地库，查询
    # 秒回、离线可用、官网风控也不怕。

    @staticmethod
    def _entry_to_item(rec: dict) -> NewsItem:
        """档案条目 → NewsItem（round-trip：视图/AI 工具/摘要零改动复用）。"""
        return NewsItem(
            game=str(rec.get("game") or ""),
            item_id=str(rec.get("id") or ""),
            title=str(rec.get("title") or ""),
            category=str(rec.get("category") or "资讯"),
            published=float(rec.get("published") or 0),
            url=str(rec.get("url") or ""),
            summary=str(rec.get("summary") or ""),
            content=str(rec.get("text") or ""),
            source=str(rec.get("source") or ""),
        )

    def _archive_as_items(self, recs: list[dict]) -> list[NewsItem]:
        return [self._entry_to_item(r) for r in recs]

    def _archive_absorb(self, items, source: str = ARCHIVE_SOURCE_OFFICIAL) -> int:
        """入库 + 落盘。archive 未启用时静默跳过（返回 0）。"""
        if not self.archive or not items:
            return 0
        added = self.archive.upsert(items, source)
        if added:
            self.archive.save()  # 去抖：轮询 10 分钟一次，回填批次结束再 force
        return added

    async def _archive_items(self, key: str, limit: int, category: str | None = None) -> list | None:
        """查询路径的取数入口：**先查本地库**，空/太旧才探官网。

        新鲜度兜底（真实度与时效的平衡）：库内最新条目比 2×轮询间隔还旧
        → 先拉一次官网增量入库再回库查；官网挂了就照常回库里的数据
        （这正是档案库的意义：官网风控/宕机时查询照常工作）。
        返回 None 仅当库和官网都拿不到任何条目。
        archive 未启用（None）时回退旧直连路径——schema 承诺
        「关闭档案库 = 旧行为」，测试桩与关闭配置的用户都走这条。
        """
        if self.archive is None:
            # 回退要和旧视图逐桶对齐：category=None（默认/卡池）走 news: 桶
            # （轮询每轮刷新的就是它），具体分类走 cat: 桶——否则轮询刚刷完
            # 视图还去打官网，缓存白做。
            if category:
                return await self._safe_call(
                    f"news:{key}",
                    lambda k=key, c=category, n=limit: self._category_cached(k, c, limit=n),
                )
            return await self._safe_call(
                f"news:{key}",
                lambda k=key, n=limit: self._latest_cached(k, n),
            )
        recs = self.archive.entries(key, category=category, limit=limit)
        if recs:
            stale_after = (
                self._poll_minutes("poll_interval_minutes", 10, 5, 120)
                * 60
                * ARCHIVE_STALE_FACTOR
            )
            newest = float(recs[0].get("published") or 0)
            fresh = newest >= time.time() - stale_after
            enough = len(recs) >= limit
            if fresh and enough:
                return self._archive_as_items(recs)
        # 库空 / 条数不足 / 太旧 → 探一次官网（走缓存与退避，失败不致命）
        live = await self._safe_call(
            f"news:{key}",
            lambda k=key, c=category or "最新", n=limit: self._category_cached(k, c, limit=n),
        )
        if live:
            self._archive_absorb(live, ARCHIVE_SOURCE_OFFICIAL)
            recs = self.archive.entries(key, category=category, limit=limit)
            if recs:
                return self._archive_as_items(recs)
        return self._archive_as_items(recs) if recs else None

    async def _walk_category(self, key: str, category: str, start_page: int,
                             max_pages: int) -> tuple[int, int]:
        """从 start_page 翻一个分类，直到「整页已归档」或翻完 max_pages。

        返回 (游标落点, 新入库条数)。整页已归档 → 游标重置 1（巡检模式：
        下轮从第 1 页找新公告——新公告只会出现在最前面）；页里有新条目 →
        游标推进（回填进行中）。空页 → 翻过历史末端，重置 1。
        ⚠️ 拉取失败（None）与空页（[]）严格区分：失败不动游标、下轮原处
        再试；把失败当末端会把游标打回 1，深处永远追不到。
        """
        source = self.sources.get(key)
        if source is None:
            return start_page, 0
        cursor = start_page
        added_total = 0
        for _ in range(max_pages):
            batch = await self._safe_call(
                f"deep:{key}:{category}",
                lambda s=source, c=category, p=cursor: s.fetch_page(c, p),
            )
            if batch is None:
                # 拉取失败（网络/风控/退避中）：下轮从本页原处再试
                return cursor, added_total
            if not batch:
                # 空页 = 翻过历史末端 → 重置回 1 进入巡检
                self.archive.set_cursor(key, category, 1)
                self.archive.save(force=True)
                return 1, added_total
            known = sum(1 for i in batch if self.archive.has(key, i.item_id))
            if known == len(batch):
                # 整页都已归档 → 前面没有缺口，进入巡检
                self.archive.set_cursor(key, category, 1)
                self.archive.save(force=True)
                return 1, added_total
            added_total += self._archive_absorb(batch, ARCHIVE_SOURCE_OFFICIAL)
            cursor += 1
            self.archive.set_cursor(key, category, cursor)
            self.archive.save(force=True)
        return cursor, added_total

    async def _backfill_walk(self, key: str, category: str, parallel: int) -> int:
        """回填单个分类：并发批量翻页到末端。返回新入库条数。"""
        source = self.sources.get(key)
        if source is None or self.archive is None:
            return 0
        added_total = 0
        page = self.archive.cursor(key, category)
        while page <= MAX_PAGES_PER_CATEGORY:
            pages = list(range(page, min(page + parallel, MAX_PAGES_PER_CATEGORY + 1)))
            results = await asyncio.gather(
                *(source.fetch_page(category, p) for p in pages),
                return_exceptions=True,
            )
            batch: list = []
            hit_end = False
            failed = False
            for _p, res in zip(pages, results, strict=True):
                if isinstance(res, BaseException):
                    failed = True
                    break  # 一页失败就停：页与页之间有顺序，跳页会留洞
                if not res:
                    hit_end = True
                    break
                batch.extend(res)
            # 「追平」判定必须在 absorb **之前**统计——刚入库的条目必然全
            # known，先入库再数会把「有新内容的普通批次」误判成追平，
            # 回填永远停在第二批（写规格测试时抓到的顺序 bug）。
            known = sum(1 for i in batch if self.archive.has(key, i.item_id))
            if batch:
                added_total += self._archive_absorb(batch, ARCHIVE_SOURCE_OFFICIAL)
            if hit_end:
                # 翻过历史末端 → 游标重置回 1 进入巡检
                self.archive.set_cursor(key, category, 1)
                self.archive.save(force=True)
                return added_total
            if failed:
                # 中途失败：**不动游标**（还是本批起始页），下次从这里续走——
                # 打回 1 会把百页级的回填进度全部作废。
                return added_total
            # 批内全是已归档条目且不是第一批 → 已追平，直接进巡检
            if batch and known == len(batch) and page > 1:
                self.archive.set_cursor(key, category, 1)
                self.archive.save(force=True)
                return added_total
            page = pages[-1] + 1
            self.archive.set_cursor(key, category, page)
            self.archive.save(force=True)
        return added_total

    async def _backfill_archive(self) -> None:
        """首启全量回填（深档用户决策）：方舟并发 6、1999 串行防风控。

        幂等：已回填过（档案条数够）直接跳过；中断有游标，重启续走。
        """
        if not self.archive:
            return
        logger.info(f"[{PLUGIN_NAME}] 档案库回填开始（当前 {self.archive.count()} 条）")
        t0 = time.time()
        plan = [("arknights", cat, BACKFILL_PARALLEL_ARK) for cat in DEEP_CATEGORIES]
        plan += [("re1999", cat, 1) for cat in DEEP_CATEGORIES]
        for key, category, parallel in plan:
            if key not in self.sources or key not in self._enabled_games():
                continue
            try:
                added = await self._backfill_walk(key, category, parallel)
                logger.info(
                    f"[{PLUGIN_NAME}] 回填 {game_display(key)}/{category}：+{added}"
                    f"（累计 {self.archive.count()} 条）"
                )
            except Exception as e:
                logger.warning(
                    f"[{PLUGIN_NAME}] 回填 {game_display(key)}/{category} 中断"
                    f"（游标已保存，重启续走）：{type(e).__name__}: {e!r}"
                )
        self.archive.set_meta("backfilled_at", time.time())
        self.archive.save(force=True)
        logger.info(
            f"[{PLUGIN_NAME}] 档案库回填完成：{self.archive.count()} 条，"
            f"耗时 {time.time() - t0:.0f}s"
        )

    async def _deep_archive(self) -> None:
        """每小时深抓：从游标继续翻（≤3 页/轮），碰到整页已归档即停。

        与轮询分工：poll_news（10 分钟）管「最新」，这里管「更深的历史」——
        回填中断的续走、大量补发的追赶、偶尔漏掉的中间页。
        """
        if not self.archive:
            return
        for key in self._enabled_games():
            for category in DEEP_CATEGORIES:
                try:
                    page = self.archive.cursor(key, category)
                    _, added = await self._walk_category(
                        key, category, page, DEEP_PAGES_PER_RUN
                    )
                    if added:
                        logger.info(
                            f"[{PLUGIN_NAME}] 深抓 {game_display(key)}/{category}：+{added}"
                        )
                except Exception as e:
                    # 逐源隔离：一个分类炸了不拖累其他分类/游戏
                    logger.warning(
                        f"[{PLUGIN_NAME}] 深抓 {game_display(key)}/{category} 异常（本轮跳过）："
                        f"{type(e).__name__}: {e!r}"
                    )
        self.archive.save()

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
                # 增量入库（v1.3.0）：轮询拉到的每一条都进档案库
                self._archive_absorb(items, ARCHIVE_SOURCE_OFFICIAL)
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
                # B站动态也进档案库（渠道标注「B站动态」，查询可见）
                self._archive_absorb(
                    [
                        NewsItem(
                            game=d.game,
                            item_id=d.item_id,
                            title=d.title,
                            category="动态",
                            published=d.published,
                            url=d.url,
                            summary=d.text[:80],
                            content=d.text,
                            source=ARCHIVE_SOURCE_BILI_DYN,
                        )
                        for d in dyns
                    ],
                    ARCHIVE_SOURCE_BILI_DYN,
                )
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
                continue
            # 第四渠道：视频投稿（v1.3.0）——PV/联动预告常只发视频不发动态。
            # 只入档案（查询可见），不推送：动态推送已覆盖视频型动态，
            # 纯投稿的推送留给后续版本。拉取失败只影响档案增量，不影响动态。
            try:
                vids = await self.bili.dynamics.space_videos(
                    uids.get(key, ""), key, limit=5
                )
                if vids:
                    self._archive_absorb(vids, ARCHIVE_SOURCE_BILI_VIDEO)
            except Exception as e:
                logger.info(
                    f"[{PLUGIN_NAME}] {game_display(key)} 视频投稿拉取失败（忽略）："
                    f"{type(e).__name__}: {e!r}"
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
            # 真实度②：推送摘要同样附原文标题（与 /游戏 视图同口径）
            titles = "／".join(one_line(i.title, 28) for i in items[: self._digest_max_items()])
            body = f"🆕 {game_display(key)} 资讯要点\n\n{digest}\n\n📌 摘要依据原文：{titles}"
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
            # v1.3.0：查本地档案库（不限分类，keyword 由档案查询过滤）
            return await self._archive_items(key, 30, category=None if cat == "最新" else cat)

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

    async def tool_games_history(
        self,
        event: AstrMessageEvent,
        game: str = "",
        keyword: str = "",
        days: float = 90,
    ) -> str:
        """查游戏**历史**公告档案（本地库，支持关键词与时间范围）。

        用户问「9月的维护公告说了啥」「上个月的卡池是哪个」「去年联动是什么时候」
        这类翻历史的问题时用这个。档案覆盖两家官网公告/活动/新闻全文 +
        B站动态，最早可到一年前。查询走本地库，官网挂了也能答。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
            keyword(string): 关键词，对标题+正文匹配，例如 维护/卡池/联动/补偿。留空不过滤。
            days(number): 往前查多少天，默认 90，最大 365。
        """
        if not self._ai_enabled():
            return "游戏 AI 工具已被插件配置关闭。"
        if self.archive is None:
            return "档案库未启用（插件配置 archive_enabled）。"
        games = self._resolve_ai_games(game)
        if not games:
            return UNKNOWN_GAME_HINT
        try:
            days = min(365.0, max(1.0, float(days)))
        except (TypeError, ValueError):
            days = 90.0
        out: list[str] = []
        for key in games:
            recs = self.archive.entries(key, keyword=keyword, days=days, limit=15)
            if not recs:
                continue
            out.append(f"【{game_display(key)} 近 {days:g} 天{('含「' + keyword + '」') if keyword else ''}的档案】")
            for rec in recs:
                head = one_line(str(rec.get("title") or ""), 50)
                src = str(rec.get("source") or "")
                label = src or str(rec.get("category") or "")
                out.append(f"· {fmt_date(float(rec.get('published') or 0))} [{label}] {head}")
            # 给模型一段可引用的正文（最近一条），方便直接回答「说了啥」
            if recs:
                body = one_line(strip_html(str(recs[0].get("text") or recs[0].get("summary") or "")), 200)
                if body:
                    out.append(f"（最新一条《{one_line(str(recs[0].get('title') or ''), 30)}》正文摘录：{body}）")
        if not out:
            return (
                f"档案里最近 {days:g} 天没有相关公告"
                + (f"（关键词：{keyword}）" if keyword else "")
                + "。可以放大时间范围（days 最多 365）或换关键词。"
            )
        return "\n".join(out)

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
