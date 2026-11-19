"""备忘录模块 Core：会话级待办 + 定时提醒。

- /备忘 添加 [时间] 内容 / 列表 / 完成 编号 / 删除 编号（裸指令 + 内部路由，
  同 /游戏 的理由：command_group 在只发组名时会抛 ValueError，且 /备忘 无参
  必须出帮助）
- 3 个 LLM 工具：memo_add / memo_list / memo_complete（对话记账，return str）
- 每分钟到期扫描：context.send_message 直接推送到期会话；发送成功才记
  「已提醒」，失败下轮重发（对齐套件可靠性红线）
- on_llm_request 钩子：聊到待办/提醒话题时幕后提示模型用 memo 工具

备忘是会话自有数据、随时可逆，写操作不需要验证码（与绑定全局凭证不同）。
群聊可用：备忘按会话隔离，群里记的就是这个群的备忘。
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:

    def get_astrbot_data_path() -> str:
        return "data"


try:
    from .storage import MemoStore
    from .timeparse import parse_when
except ImportError:  # 以仓库根为工作目录直接导入（测试/脚本）
    from storage import MemoStore
    from timeparse import parse_when

SUITE_NAME = "astrbot_plugin_suite"
MODULE_NAME = "memo"
LOG_PREFIX = f"{SUITE_NAME}/{MODULE_NAME}"

# —— 指令路由（裸指令第一个词）——
_ADD_ALIASES = {"添加", "加", "新增", "记一下", "提醒我"}
_LIST_ALIASES = {"列表", "清单", "看看", "查查"}
_DONE_ALIASES = {"完成", "好了", "搞定"}
_DEL_ALIASES = {"删除", "删掉", "取消"}

HELP_TEXT = (
    "📖 备忘录用法：\n"
    "/备忘 添加 [时间] 内容 —— 时间可放在任意位置：明天 8:00 交作业、"
    "30分钟后 起锅、21:30 吃药\n"
    "/备忘 列表 —— 看本会话的备忘\n"
    "/备忘 完成 编号 —— 例：/备忘 完成 3\n"
    "/备忘 删除 编号\n"
    "不带时间就是纯待办，不会定时提醒；带时间的到点自动推送本会话。\n"
    "也可以直接跟我说「提醒我明天 8 点交作业」。"
)

TIME_HINT = (
    "没看懂时间。可用：21:30、明天 8:00、后天 9点半、30分钟后、3天后、"
    "10月1日 9:00；不带时间就是纯待办。"
)

# —— AI 触达：关键词宁宽勿漏，误命中最多多一轮 token，漏了 AI 就想不起来用 ——
AI_MEMO_KEYWORDS: tuple[str, ...] = (
    "备忘",
    "待办",
    "提醒我",
    "别忘了",
    "记得提醒",
    "记一下",
    "帮我记",
    "定个闹钟",
    "闹钟",
    "到点叫我",
    "几点提醒",
)
AI_MEMO_HINT = (
    "[memo] 用户聊到了备忘/待办/提醒话题。必须调用 memo_add/memo_list/memo_complete "
    "工具，严禁编造编号或假装已记录。时间写法：21:30、明天 8:00、30分钟后、"
    "10月1日；用户没给时间就存纯待办，不要瞎猜时间。"
)

_ID_RE = re.compile(r"\d+")


class MemoCore:
    def __init__(self, context: Context, config):
        self.context = context
        self.config = config
        self.store: MemoStore | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._tasks: list[asyncio.Task] = []

    # ================= 配置 =================

    def _cfg(self, key, default=None):
        try:
            return self.config.get(key, default)
        except (AttributeError, TypeError):
            return default

    def _cfg_int(self, key, default, low, high):
        try:
            value = int(float(self._cfg(key, default)))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def _scan_seconds(self) -> int:
        return self._cfg_int("memo_scan_seconds", 60, 30, 600)

    def _max_per_session(self) -> int:
        return self._cfg_int("memo_max_per_session", 20, 1, 200)

    def _hint_enabled(self) -> bool:
        return self._cfg("ai_memo_prompt_hint", True) is not False

    def _ai_enabled(self) -> bool:
        return self._cfg("ai_memo_tools_enabled", True) is not False

    # ================= 生命周期 =================

    async def initialize(self):
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / SUITE_NAME / MODULE_NAME
        self.store = MemoStore(
            data_dir / "memo.json",
            keep_done_days=self._cfg_int("memo_keep_done_days", 7, 1, 365),
        )
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._scan,
            IntervalTrigger(seconds=self._scan_seconds()),
            id="memo_scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        self.scheduler.start()
        logger.info(f"[{LOG_PREFIX}] 备忘录已启动（扫描间隔 {self._scan_seconds()}s）")

    async def terminate(self):
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                logger.exception(f"[{LOG_PREFIX}] 调度器关闭失败")
            self.scheduler = None
        if self.store is not None:
            try:
                self.store.save()
            except OSError as e:
                logger.warning(f"[{LOG_PREFIX}] 退出时落盘失败：{e!r}")
            self.store = None

    async def _scan(self):
        """到期扫描：推送 → 发送成功才记账（失败下轮重发）→ 顺带清理。"""
        store = self.store
        if store is None:
            return
        now = int(time.time())
        due = store.due_items(now)
        pushed = 0
        for item in due:
            chain = MessageChain().message(f"⏰ 备忘提醒：{item.get('text', '')}")
            try:
                ok = await self.context.send_message(item.get("umo"), chain)
            except Exception as e:
                logger.warning(
                    f"[{LOG_PREFIX}] 提醒推送失败（#{item.get('id')}）：{e!r}"
                )
                ok = False
            if ok is False:
                continue
            store.mark_reminded(item["id"], now)
            pushed += 1
        removed = store.sweep(now)
        if pushed or removed:
            store.save()
        if pushed:
            logger.info(f"[{LOG_PREFIX}] 本轮推送 {pushed} 条提醒")

    # ================= 指令本体（装饰器在套件根 main.py） =================

    def _arg_text(self, event) -> str:
        """取掉指令名后的参数部分（裸指令按空格切，与 /游戏 同一套路）。"""
        raw = ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                raw = str(getter() or "")
            except Exception:
                raw = ""
        if not raw:
            raw = str(getattr(event, "message_str", "") or "")
        raw = " ".join(raw.split()).strip()
        if not raw:
            return ""
        # 第一段一定是指令名（宿主就是按它匹配上的），后面才是参数
        return raw.partition(" ")[2].strip()

    async def cmd_memo(self, event: AstrMessageEvent):
        """/备忘 裸指令本体：帮助 / 列表 / 完成 / 删除 / 添加（兜底）。"""
        arg = self._arg_text(event)
        words = arg.split(" ", 1)
        sub = words[0].strip() if words and words[0] else ""
        rest = words[1].strip() if len(words) > 1 else ""
        umo = event.unified_msg_origin

        if not sub or sub in ("帮助", "help", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return
        if self.store is None:
            yield event.plain_result("备忘录还没初始化完成，请稍后再试。")
            return

        if sub in _LIST_ALIASES:
            yield event.plain_result(self._list_memo(umo))
            return
        if sub in _DONE_ALIASES:
            yield event.plain_result(self._complete_memo(umo, rest))
            return
        if sub in _DEL_ALIASES:
            yield event.plain_result(self._delete_memo(umo, rest))
            return
        if _ID_RE.fullmatch(sub):
            yield event.plain_result(f"想完成它？发：/备忘 完成 {sub}")
            return
        # 兜底：整串当内容记下（「/备忘 交作业」=「/备忘 添加 交作业」）
        yield event.plain_result(self._add_memo(umo, arg))

    # ---------- 添加 ----------

    def _check_capacity(self, umo) -> str | None:
        limit = self._max_per_session()
        if self.store.pending_count(umo) >= limit:
            return (
                f"本会话未完成备忘已有 {limit} 条，先整理一下（/备忘 列表）再记吧。"
            )
        return None

    def _add_memo(self, umo, raw) -> str:
        if not raw:
            return "想记什么？例：/备忘 添加 明天 8:00 交作业"
        full = self._check_capacity(umo)
        if full:
            return full
        due, content = parse_when(raw)
        if not content:
            return "时间记下了，但要办的事呢？例：/备忘 添加 明天 8:00 交作业"
        item = self.store.add(umo, content, due)
        self.store.save()
        if due:
            when = time.strftime("%m-%d %H:%M", time.localtime(item["due"]))
            return f"✅ 已记下（#{item['id']}）：{content}\n⏰ {when} 会提醒这个会话"
        return f"✅ 已记下（#{item['id']}）：{content}\n（纯待办，不带时间不会定时提醒）"

    # ---------- 列表 ----------

    @staticmethod
    def _item_line(item, now=None) -> str:
        now = time.time() if now is None else now
        due = item.get("due")
        when = time.strftime("%m-%d %H:%M", time.localtime(due)) if due else "不限时"
        if item.get("reminded"):
            suffix = "｜已提醒"
        elif due and due <= now:
            suffix = "｜即将提醒"
        else:
            suffix = ""
        return f"#{item['id']}  {when}｜{item.get('text', '')}{suffix}"

    def _list_memo(self, umo) -> str:
        pending = self.store.items_of(umo, include_done=False)
        done_total = sum(
            1
            for i in self.store.data["items"]
            if i.get("umo") == umo and i.get("done")
        )
        if not pending and not done_total:
            return "本会话还没有备忘。记一条：/备忘 添加 明天 8:00 交作业"
        header = f"📝 备忘（未完成 {len(pending)} 条"
        if done_total:
            header += f"｜已完成 {done_total} 条（保留 {self.store.keep_done_days} 天后清理）"
        header += "）"
        now = time.time()
        lines = [self._item_line(i, now) for i in pending]
        if not lines:
            lines.append("（全部完成，干得漂亮）")
        return header + "\n" + "\n".join(lines)

    # ---------- 完成 / 删除 ----------

    @staticmethod
    def _parse_id(rest) -> int | None:
        m = _ID_RE.search(rest or "")
        return int(m.group()) if m else None

    def _complete_memo(self, umo, rest) -> str:
        memo_id = self._parse_id(rest)
        if memo_id is None:
            return "要完成哪条？例：/备忘 完成 3（编号看 /备忘 列表）"
        item = self.store.complete(umo, memo_id)
        if item is None:
            return f"没找到本会话未完成的 #{memo_id}。用 /备忘 列表 对一下编号。"
        self.store.save()
        return f"✅ 已完成：{item.get('text', '')}"

    def _delete_memo(self, umo, rest) -> str:
        memo_id = self._parse_id(rest)
        if memo_id is None:
            return "要删哪条？例：/备忘 删除 3（编号看 /备忘 列表）"
        item = self.store.delete(umo, memo_id)
        if item is None:
            return f"没找到本会话的 #{memo_id}。用 /备忘 列表 对一下编号。"
        self.store.save()
        return f"🗑 已删除：{item.get('text', '')}"

    # ================= 快捷数字菜单 =================

    def quick_actions(self) -> list[tuple[str, Any]]:
        """本模块供快捷数字菜单使用的条目：[(标题, 执行器)]。

        执行器签名 async (event) -> str，返回给用户看的文本。套件根
        main.py 按模块开关过滤后交给 quick 模块布防编号。
        """
        return [("备忘列表", self._quick_list)]

    async def _quick_list(self, event: AstrMessageEvent) -> str:
        if self.store is None:
            return "备忘录还没初始化完成，请稍后再试。"
        return self._list_memo(event.unified_msg_origin)

    # ================= LLM 工具本体（装饰器在套件根 main.py） =================

    async def tool_memo_add(self, event: AstrMessageEvent, text: str = "", when: str = "") -> str:
        """记一条备忘/待办，到点自动提醒用户。用户说「提醒我…」「记一下…」时调用。
        Args:
            text(string): 要记的内容，例如「交作业」「给妈妈打电话」
            when(string): 可选提醒时间：21:30 / 明天 8:00 / 30分钟后 / 3天后 / 10月1日 9:00；留空=纯待办不定时提醒
        """
        umo = event.unified_msg_origin
        if self.store is None:
            return "备忘录还没初始化完成，请稍后再试。"
        if not self._ai_enabled():
            return "备忘录 AI 工具已在插件配置里关闭，请联系管理员开启。"
        text = str(text or "").strip()
        when = str(when or "").strip()
        if not text and not when:
            return "想记什么？请先向用户要一句具体内容。"
        full = self._check_capacity(umo)
        if full:
            return full
        due, content = parse_when(f"{when} {text}".strip())
        if when and due is None:
            return (
                f"时间「{when}」没看懂。可用：21:30 / 明天 8:00 / 30分钟后 / "
                "10月1日 9:00，或留空当纯待办。"
            )
        if not content:
            return "要办的事呢？请向用户确认一句具体内容。"
        item = self.store.add(umo, content, due)
        self.store.save()
        if due:
            when_str = time.strftime("%m-%d %H:%M", time.localtime(item["due"]))
            return f"✅ 已记下（#{item['id']}）：{content}（{when_str} 会提醒）"
        return f"✅ 已记下（#{item['id']}）：{content}（纯待办，不定时提醒）"

    async def tool_memo_list(self, event: AstrMessageEvent) -> str:
        """列出当前会话的备忘/待办。用户问「我记了什么」「有什么待办」时调用。"""
        if self.store is None:
            return "备忘录还没初始化完成，请稍后再试。"
        if not self._ai_enabled():
            return "备忘录 AI 工具已在插件配置里关闭，请联系管理员开启。"
        listing = self._list_memo(event.unified_msg_origin)
        return listing

    async def tool_memo_complete(self, event: AstrMessageEvent, memo_id: str = "") -> str:
        """把当前会话的某条备忘标记为完成。编号来自 memo_list，不要编造。
        Args:
            memo_id(string): 备忘编号（纯数字），例如「3」
        """
        umo = event.unified_msg_origin
        if self.store is None:
            return "备忘录还没初始化完成，请稍后再试。"
        if not self._ai_enabled():
            return "备忘录 AI 工具已在插件配置里关闭，请联系管理员开启。"
        try:
            memo_id_int = int(float(str(memo_id).strip()))
        except (TypeError, ValueError):
            return "编号要是数字。先调 memo_list 拿到编号。"
        item = self.store.complete(umo, memo_id_int)
        if item is None:
            return f"没找到本会话未完成的 #{memo_id_int}。先调 memo_list 对一下编号。"
        self.store.save()
        return f"✅ 已完成：{item.get('text', '')}"

    # ================= AI 触达：幕后指令注入（装饰器在套件根 main.py） =================

    @staticmethod
    def _collect_hint_texts(event: AstrMessageEvent, req) -> list[str]:
        """关键词扫描的文本源：本轮消息 + 本轮 prompt + 近几轮历史。"""
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

    def _hit_memo_topic(self, event: AstrMessageEvent, req) -> bool:
        return any(
            kw in text
            for text in self._collect_hint_texts(event, req)
            for kw in AI_MEMO_KEYWORDS
        )

    async def inject_memo_hint(self, event: AstrMessageEvent, req) -> None:
        """关键词命中的对话，在 system_prompt 末尾追加备忘工具提示（幂等）。"""
        try:
            if not self._ai_enabled() or not self._hint_enabled():
                return
            if not self._hit_memo_topic(event, req):
                return
            prompt = getattr(req, "system_prompt", None)
            if not isinstance(prompt, str):
                prompt = ""
            if AI_MEMO_HINT in prompt:
                return
            req.system_prompt = (prompt.rstrip() + "\n" + AI_MEMO_HINT).strip()
        except Exception as e:  # 注入失败绝不能影响正常对话
            logger.warning(f"[{LOG_PREFIX}] 备忘提示注入失败：{e!r}")
