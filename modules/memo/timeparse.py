"""备忘录时间解析：把「明天 8:00 / 30分钟后 / 10月1日 9点」翻成到期时间戳。

纯函数、无副作用、可离线矩阵测试。解析原则：
- 只有「点 / 冒号 / 分 / 半 / …后 / N月N日」这些时间词才算时间——
  「买 3 斤苹果」「3-4 个人」不会被误伤
- 命中后把时间段从原文剥掉，剩余即备忘内容
- 无时间 → (None, 原文)：纯待办，不定时提醒
- 时刻已过：今天 → 明天；「N月N日」→ 明年
"""

import re
import unicodedata
from datetime import datetime, timedelta

_OFFSET_DAYS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}

# 相对时长：30分钟后 / 2小时后 / 3天之后 / 90秒后
_REL_RE = re.compile(r"(\d+)\s*(秒|分钟[钟]?|小时[钟]?|个钟头|天)[之]?后")

# 日期 + 可选时刻：10月1日 / 12月31号 / 10月1日 9:00 / 10月1日 9点半
_DATE_RE = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"
    r"(?:\s*(\d{1,2})[点:：]\s*(?:(\d{1,2})\s*分?|(半))?)?"
)

# 时刻（可带日偏移前缀）：8:00 / 8点 / 8点半 / 明天 7:30 / 后天 9点 / 今天 21:30
_TIME_RE = re.compile(
    r"(今天|明天|后天|大后天)?\s*(\d{1,2})[点:：]\s*(?:(\d{1,2})\s*分?|(半))?"
)


def _minute_of(num_text, half_text):
    """解析「分」的部分：数字分 / 半 / 缺省 0；越界返回 None（该写法无效）。"""
    if half_text:
        return 30
    if num_text is None:
        return 0
    value = int(num_text)
    return value if 0 <= value <= 59 else None


def _from_rel(match, now):
    n = int(match.group(1))
    if n <= 0:
        return None
    unit = match.group(2)
    if unit == "秒":
        return now + timedelta(seconds=n)
    if unit.startswith("分钟"):
        return now + timedelta(minutes=n)
    if unit.startswith("小时") or unit == "个钟头":
        return now + timedelta(hours=n)
    return now + timedelta(days=n)


def _from_date(match, now):
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    minute = _minute_of(match.group(4), match.group(5))
    if minute is None:
        return None
    hour = int(match.group(3)) if match.group(3) is not None else 0
    if not 0 <= hour <= 23:
        return None
    try:
        due = now.replace(
            year=now.year,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
    except ValueError:  # 2月30日 之类
        return None
    if due <= now:  # 今年的日子已过 → 明年
        try:
            due = due.replace(year=now.year + 1)
        except ValueError:
            return None
    return due


def _from_time(match, now):
    day_offset = _OFFSET_DAYS.get(match.group(1) or "", 0)
    hour = int(match.group(2))
    minute = _minute_of(match.group(3), match.group(4))
    if not 0 <= hour <= 23 or minute is None:
        return None
    base = (now + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if day_offset == 0 and base <= now:
        base += timedelta(days=1)  # 今天这个时刻已过 → 明天同一时刻
    return base


def parse_when(text, now=None):
    """从 text 里解析时间。

    返回 (到期 unix 时间戳或 None, 剥掉时间后的剩余文本)。
    时间词可以出现在内容的任意位置。
    """
    norm = " ".join(unicodedata.normalize("NFKC", str(text or "")).split())
    if not norm:
        return None, ""
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    for pattern, builder in (
        (_REL_RE, _from_rel),
        (_DATE_RE, _from_date),
        (_TIME_RE, _from_time),
    ):
        match = pattern.search(norm)
        if match is None:
            continue
        due = builder(match, now)
        if due is None:
            continue
        rest = (norm[: match.start()] + " " + norm[match.end():]).strip()
        return int(due.timestamp()), " ".join(rest.split())
    return None, norm
