"""兑换码提取与合并（B站动态正文 + 公告正文兜底，双来源）。

零额外请求：资讯轮询本来就拉回两家公告全文（1999 更是整篇 HTML），
顺手用同一个正则扫一遍，值回不去也不花钱。B站风控持续抽风时，
码仍能从公告里捞到——这就是「兜底扫描」的意义。

不提供手动登记通道（用户决策）：码的来源只有自动提取，报漏了就等下一次轮询。
"""

import re
import time

try:
    from .providers.base import CodeHit, strip_html
except ImportError:  # 兜底：被以非包方式加载时
    from providers.base import CodeHit, strip_html  # type: ignore

# 兑换码本体：字母数字 6~20 位（1999 常见 8 位纯字母，B站直播码常带数字）。
# 必须紧跟在「兑换码/礼包码/…」这类引导词后面，否则正文里随便一个长单词
# 都会被当成码。
CODE_RE = re.compile(
    r"(?:兑换码|礼品码|礼包码|激活码|序列码|cdk|redeem[\s_-]*code)"
    r"\s*[码]?\s*[：:是为\-_]{0,2}\s*"
    # 尾部否定前瞻：21 位以上的长串不是码，不许截前 20 位就当命中
    r"([A-Za-z0-9]{6,20})(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 「码」后面跟冒号直接给码的极简写法（直播回放里很常见）
BARE_CODE_RE = re.compile(r"码\s*[：:]\s*([A-Za-z0-9]{6,20})(?![A-Za-z0-9])")

# 过期时间：优先认「有效期至/截止/过期时间」
EXPIRE_RE = re.compile(
    r"(?:有效期至?|有效期限?|截止(?:时间|日期)?|过期时间|失效时间)\s*[：:]?\s*"
    r"((?:\d{4}\s*年)?\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s*\d{1,2}\s*[:：]\s*\d{2})?)"
)

# 文案里出现这些词、但没给具体日期时的通用提醒
NO_DATE_HINT = "未标注过期时间，直播码一般次日失效，速兑"

# 明显的非码噪声：这些词纯字母且长度落在码区间里，先排掉
NOISE = {"HYPERGRYPH", "BLUECPOCH", "BILIBILI", "ANNOUNCEMENT", "MAINTENANCE"}


def extract_codes(
    text: str,
    game: str,
    source: str,
    url: str = "",
    *,
    html: bool = False,
    limit: int = 8,
) -> list[CodeHit]:
    """从一段文本里抽出所有兑换码（纯函数，便于单测）。

    html=True 时先剥标签——公告 content 是整篇 HTML，不剥会截断码。
    同一个码在一段文本里出现多次只留一条，顺序按首次出现。
    """
    if not text:
        return []
    blob = strip_html(text) if html else str(text)
    expire_hint = _expire_hint(blob)
    out: list[CodeHit] = []
    seen: set[str] = set()
    for pattern in (CODE_RE, BARE_CODE_RE):
        for m in pattern.finditer(blob):
            code = m.group(1)
            key = code.upper()
            if key in seen or key in NOISE:
                continue
            seen.add(key)
            out.append(
                CodeHit(
                    code=code,
                    game=game,
                    source=source,
                    url=url,
                    expire_hint=expire_hint,
                    context=_context(blob, m.start()),
                )
            )
            if len(out) >= limit:
                return out
    return out


def _expire_hint(text: str) -> str:
    m = EXPIRE_RE.search(text)
    if not m:
        return ""
    s = re.sub(r"\s+", " ", m.group(1)).replace("：", ":").strip()
    # "11月5日24:00" → "11月5日 24:00"，展示时挤在一起不好读
    return re.sub(r"(\d日)(\d)", r"\1 \2", s)


def _context(text: str, pos: int, width: int = 40) -> str:
    return re.sub(r"\s+", " ", text[max(0, pos - width) : pos + width]).strip()


def scan_item(item, source: str = "notice") -> list[CodeHit]:
    """从一条 NewsItem 里扫码（1999 的 content 是 HTML，交给 html=True 剥）。"""
    html = "<" in (item.content or "")
    return extract_codes(
        item.content or item.text_blob,
        item.game,
        source,
        item.url,
        html=html,
    )


def scan_dynamics(dyn, source: str = "bilibili") -> list[CodeHit]:
    """从一条 B站动态里扫码（正文是纯文本）。"""
    return extract_codes(f"{dyn.title}\n{dyn.text}", dyn.game, source, dyn.url)


def merge_into(existing: list[dict], hits: list[CodeHit], now: float, ttl_hours: int) -> list[dict]:
    """把新扫到的码并进已有码库，返回合并后的列表。

    - 同码已存在：不改首次发现时间（保「新鲜度」排序稳定），但补齐缺失的
      过期提示与第二个来源（一条码从动态和公告都能抽到时，来源更有说服力）。
    - 新码：落 found_at，expire_at 由 expire_hint 解析，解析不出就用配置的
      ttl_hours 兜底算个「大致失效时间」。
    - 过期（expire_at 已过）的码直接剔除，列表里不留过期码误导用户。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("code") or "").upper()
        if not key:
            continue
        merged[key] = dict(rec)
        order.append(key)
    for hit in hits:
        key = hit.code.upper()
        expire_at = _parse_expire(hit.expire_hint) or (now + ttl_hours * 3600 if ttl_hours else 0.0)
        if key in merged:
            rec = merged[key]
            rec.setdefault("expire_hint", "")
            if hit.expire_hint and not rec.get("expire_hint"):
                rec["expire_hint"] = hit.expire_hint
                rec["expire_at"] = expire_at
            sources = rec.setdefault("sources", [str(rec.get("source") or "")])
            if hit.source and hit.source not in sources:
                sources.append(hit.source)
            continue
        merged[key] = {
            "code": hit.code,
            "game": hit.game,
            "source": hit.source,
            "sources": [hit.source],
            "url": hit.url,
            "context": hit.context,
            "expire_hint": hit.expire_hint,
            "expire_at": expire_at,
            "found_at": now,
        }
        order.append(key)
    fresh = [merged[k] for k in order if not _expired(merged[k], now)]
    fresh.sort(key=lambda r: float(r.get("found_at") or 0), reverse=True)
    return fresh


def _expired(rec: dict, now: float) -> bool:
    try:
        return float(rec.get("expire_at") or 0) < now
    except (TypeError, ValueError):
        return False


def _parse_expire(hint: str) -> float:
    """把 "2026年12月31日 24:00" / "11月5日" 转成 Unix 秒；认不出回 0。

    没有年份时按「最近的将来」补：12月看到 1月 显然是明年。
    """
    from datetime import datetime, timedelta

    s = re.sub(r"\s+", " ", str(hint or "")).replace("：", ":").strip()
    if not s:
        return 0.0
    year = None
    m = re.match(r"(\d{4})年", s)
    if m:
        year = int(m.group(1))
        s = s[m.end() :]
    # 日与时间之间可留空格也可能没有（原文两种都出现过），冒号可全角
    m = re.match(r"(\d{1,2})月(\d{1,2})日(?:\s*:?\s*(\d{1,2}):(\d{2}))?", s)
    if not m:
        return 0.0
    month, day = int(m.group(1)), int(m.group(2))
    hour = int(m.group(3)) if m.group(3) is not None else 23
    minute = int(m.group(4)) if m.group(4) is not None else 59
    # 「24:00」= 次日零点。不处理的话会算成当天 00:00，提前一天判过期。
    day_shift = 1 if hour >= 24 else 0
    hour = hour % 24
    now = datetime.now()
    if year is None:
        for candidate in (now.year, now.year + 1):
            try:
                base = datetime(candidate, month, day, hour, minute)
            except ValueError:
                return 0.0
            if base + timedelta(days=day_shift) > now - timedelta(days=1):
                year = candidate
                break
    if year is None:
        return 0.0
    try:
        return (
            datetime(year, month, day, hour, minute) + timedelta(days=day_shift)
        ).timestamp()
    except ValueError:
        return 0.0


def expiry_note(rec: dict, ttl_hours: int = 24) -> str:
    """给一条码生成展示用的过期提示文案。"""
    try:
        expire_at = float(rec.get("expire_at") or 0)
    except (TypeError, ValueError):
        expire_at = 0.0
    hint = str(rec.get("expire_hint") or "")
    if not expire_at:
        return NO_DATE_HINT
    left = expire_at - time.time()
    if left <= 0:
        return "⚠️ 已过期"
    if left < 3600:
        return "⚠️ 不到 1 小时失效"
    if left < 3600 * 24:
        return f"⏰ 约 {int(left // 3600)} 小时后失效"
    if hint:
        return f"⏰ {hint} 失效"
    return f"⏰ 约 {ttl_hours} 小时内失效"
