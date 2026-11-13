"""数据源通用数据结构、HTML 处理与文本归一。

刻意与具体游戏无关：新增游戏只写 providers/<game>.py，不必碰这里。
strip_html / fold 也被 keywords.py、codes.py 复用（正向依赖，无环）。
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

# 先摘掉整块的脚本/样式，再把换行语义标签转成 \n，最后剥标签。
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_BLOCK_END_RE = re.compile(
    r"</\s*(p|div|br|li|tr|h[1-6]|section)\s*>|<br\s*/?>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")
_BLANK_RE = re.compile(r"\n{3,}")


def strip_html(raw: str, limit: int = 0) -> str:
    """把官网 HTML 正文压成纯文本（1999 的 content 是整篇 HTML）。

    limit > 0 时截断到该字符数并补省略号——列表展示只要前几十字。
    """
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", str(raw))
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n\n", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def fold(text: str) -> str:
    """归一化用于匹配的文本：NFKC + 小写 + 去空白与常见分隔符。

    全角「ＡＫ」与半角「ak」要落到同一个键上；用户输入里的空格、连字符、
    间隔号也不该影响识别——所以先折再比，别在各处各写一套清洗。
    """
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return "".join(ch for ch in s if ch not in " \t\r\n-_·・,.，。!！?？")


# 资讯渠道名（v1.3.0 真实度标注）：档案条目与列表渲染标明每条信息从哪来。
# 聊天输出不放裸链接（v1.1.0 用户决策），渠道名就是来源标注。
ARCHIVE_SOURCE_OFFICIAL = "官网"
ARCHIVE_SOURCE_BILI_DYN = "B站动态"
ARCHIVE_SOURCE_BILI_VIDEO = "B站视频"


@dataclass(frozen=True)
class NewsItem:
    """一条官方资讯。

    content 存正文原文（1999 是 HTML 全文，方舟用官网给的 brief），
    summary 是给列表展示的一行短摘要。
    source 是**渠道名**（官网 / B站动态 / B站视频，v1.3.0 起档案条目回填
    此字段）：真实度标注用——用户能看出每条信息从哪个渠道来，模型编造
    无所遁形。官网直拉的条目默认空串（category 已表达官网栏目）。
    """

    game: str
    item_id: str
    title: str
    category: str
    published: float
    url: str
    summary: str = ""
    content: str = ""
    source: str = ""

    @property
    def text_blob(self) -> str:
        """供关键词分类与兑换码扫描的合并文本。"""
        return f"{self.title}\n{self.summary}\n{self.content}"


@dataclass(frozen=True)
class CodeHit:
    """从 B站动态或公告正文中抽出的一条兑换码（尚未入库）。"""

    code: str
    game: str
    source: str  # "bilibili" | "notice"
    url: str
    expire_hint: str = ""
    context: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


class SourceError(Exception):
    """数据源不可用：网络异常 / 官网改版 / 被风控拦。调用方据此退避重试。"""


class GameSource(Protocol):
    """单个游戏的官网资讯源。"""

    key: str
    display_name: str

    async def latest(self, limit: int = 10) -> list[NewsItem]: ...

    async def by_category(self, category: str, limit: int = 20) -> list[NewsItem]: ...

    async def close(self) -> None: ...
