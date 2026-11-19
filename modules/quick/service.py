"""快捷数字菜单：/菜单 出一份编号菜单，用户回一个数字直达对应动作。

设计约束（每一条都是踩出来的坑）：

- **纯逻辑模块**：不 import electric/games/memo，也不 import astrbot。
  菜单条目由各模块自报 quick_actions()，套件根 main.py 按模块开关过滤后
  交给这里布防——本模块只管「布防状态 + 数字解析 + 菜单渲染」。
- **只认单个数字**：电费的 6 位确认码、备忘的条目编号都是多位数，一律
  不认。宁可漏接也不能劫持正常对话（确认码被吞 = 绑定确认直接断链）。
- **布防按会话隔离且带 TTL**：没布防/过期后，数字消息照旧走 AI，
  绝不劫持日常聊天。
- **单次消费 + 续期**：执行一个条目后续期，但同一条目不会重复执行到
  别的会话（状态按 umo 存）。
"""

import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# 菜单条目执行器：async (event) -> str（返回给用户看的文本）
QuickHandler = Callable[[Any], Awaitable[str]]

# 取消用的保留数字（「0 取消」永远在，不参与编号）
CANCEL_DIGIT = "0"

# 菜单标题
MENU_TITLE = "🔢 快捷菜单"

# 中文数字 → 阿拉伯数字（用户写「按一」也算）
_CN_DIGITS = {
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}

# 数字前可带的前缀词（长前缀在前：「选择1」不能被「选」截成「择1」）
_PREFIXES = ("选择", "回复", "按", "选")

# 数字首尾可带的标点与空白
_PUNCT = " \t\r\n　。．.！!？?、，,~～、·"


def parse_digit(text: str) -> str | None:
    """把一条消息解析成菜单数字；不是「单个数字」就返回 None。

    接受：``1`` ``１``（全角）``一`` ``按1`` ``选 1`` ``选择1`` ``1。``
    拒绝：``481526``（6 位确认码）``12``（多位数）``1 度电`` ``abc`` ``""``

    判定极严：去掉前缀与首尾标点后必须**恰好剩一个字符**，且是数字或
    中文数字。多位数一律不认——这是保护绑定确认码与备忘编号的关键。
    """
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    s = s.strip(_PUNCT)
    for prefix in _PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix) :].strip(_PUNCT)
            break
    if len(s) != 1:
        return None
    ch = s
    if ch in "0123456789":
        return ch
    return _CN_DIGITS.get(ch)


@dataclass
class QuickItem:
    """菜单里的一条：编号 + 标题 + 执行器。"""

    digit: str
    label: str
    handler: QuickHandler


@dataclass
class Pick:
    """一次数字回复的解析结果。

    kind: ``run``（命中条目）/ ``cancel``（回了 0）/ ``invalid``（菜单里没有）
    """

    kind: str
    digit: str = ""
    item: QuickItem | None = None
    menu: list[QuickItem] = field(default_factory=list)


@dataclass
class _Armed:
    """一个会话的布防状态。"""

    items: list[QuickItem]
    expires_at: float
    sender_id: str = ""


class QuickCore:
    """快捷数字菜单的状态机（纯逻辑，无 astrbot 依赖）。"""

    # 同时布防的会话上限：防止长期运行后内存无限增长
    MAX_SESSIONS = 64

    def __init__(self, context: Any, config: Any):
        self.context = context
        self.config = config
        # 会话 → 布防状态
        self._armed: dict[str, _Armed] = {}

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

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def enabled(self) -> bool:
        return self._cfg_bool("quick_menu_enabled", True)

    def ttl_seconds(self) -> int:
        return max(30, min(3600, self._cfg_int("quick_menu_ttl_seconds", 300)))

    # ================= 布防 =================

    def arm(
        self,
        umo: str,
        sender_id: str,
        entries: list[tuple[str, QuickHandler]],
    ) -> list[QuickItem]:
        """布防一个菜单，返回带编号的条目列表。

        entries: [(标题, 执行器)]——**按给定顺序编号**，编号从 1 起连续。
        同一个会话重复布防会覆盖旧的。
        """
        self._sweep()
        items = [
            QuickItem(digit=str(i), label=label, handler=handler)
            for i, (label, handler) in enumerate(entries, start=1)
        ]
        self._armed[umo] = _Armed(
            items=items,
            expires_at=time.time() + self.ttl_seconds(),
            sender_id=sender_id,
        )
        # 超出上限时丢掉最老的会话
        while len(self._armed) > self.MAX_SESSIONS:
            oldest = min(self._armed, key=lambda k: self._armed[k].expires_at)
            self._armed.pop(oldest, None)
        return items

    def disarm(self, umo: str) -> None:
        """解除布防（用户回 0，或菜单到期后被动清理）。"""
        self._armed.pop(umo, None)

    def current(self, umo: str) -> list[QuickItem]:
        """当前会话的菜单条目（未布防/已过期返回空表）。"""
        armed = self._live(umo)
        return list(armed.items) if armed else []

    # ================= 数字回复 =================

    def take(self, umo: str, sender_id: str, text: str) -> Pick | None:
        """解析一条数字回复。

        返回 None = **不接管**（没布防/已过期/不是纯数字/发送者不符），
        这时消息照旧走 AI。返回 Pick 时 kind ∈ {run, cancel, invalid}。
        """
        armed = self._live(umo)
        if armed is None:
            return None
        # 发送者不符（同一会话里别人打的数字不算数）
        if armed.sender_id and sender_id and armed.sender_id != sender_id:
            return None
        digit = parse_digit(text)
        if digit is None:
            return None
        if digit == CANCEL_DIGIT:
            self.disarm(umo)
            return Pick(kind="cancel", digit=digit, menu=list(armed.items))
        for item in armed.items:
            if item.digit == digit:
                # 续期：连续操作时菜单不会中途过期
                armed.expires_at = time.time() + self.ttl_seconds()
                return Pick(kind="run", digit=digit, item=item, menu=list(armed.items))
        return Pick(kind="invalid", digit=digit, menu=list(armed.items))

    # ================= 渲染 =================

    def menu_text(self, items: list[QuickItem]) -> str:
        """渲染菜单文本。"""
        ttl = self.ttl_seconds()
        if ttl >= 60:
            span = f"{ttl // 60} 分钟内有效"
        else:
            span = f"{ttl} 秒内有效"
        lines = [f"{MENU_TITLE}（回复数字即可，{span}）"]
        for item in items:
            lines.append(f"{item.digit}. {item.label}")
        lines.append(f"{CANCEL_DIGIT}. 取消菜单")
        return "\n".join(lines)

    def expired_text(self) -> str:
        """菜单过期时的提示。"""
        return "⌛ 快捷菜单已过期，发送 /菜单 重新打开。"

    # ================= 内部 =================

    def _live(self, umo: str) -> _Armed | None:
        """取仍有效的布防状态；过期就清掉并返回 None。"""
        armed = self._armed.get(umo)
        if armed is None:
            return None
        if armed.expires_at <= time.time():
            self._armed.pop(umo, None)
            return None
        return armed

    def _sweep(self) -> None:
        """清掉过期条目（布防时顺手做一次，避免内存堆积）。"""
        now = time.time()
        for umo in [k for k, v in self._armed.items() if v.expires_at <= now]:
            self._armed.pop(umo, None)

    def terminate(self) -> None:
        self._armed.clear()
