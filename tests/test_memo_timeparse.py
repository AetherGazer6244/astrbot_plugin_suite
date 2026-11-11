"""memo/timeparse.parse_when 解析矩阵：真实说法全覆盖 + 误伤防线。

红线（来自设计决策）：
- 只有「点/冒号/…后/N月N日」这些时间词才算时间，「买 3 斤」「3-4 个人」不误伤
- 命中后剥掉时间词，剩余即内容；无时间 → (None, 原文)
- 今天时刻已过 → 明天；N月N日已过 → 明年
"""

from datetime import datetime, timedelta

from astrbot_plugin_suite.modules.memo.timeparse import parse_when

NOW = datetime(2026, 11, 11, 10, 0, 0).astimezone()


def _ts(dt):
    return int(dt.timestamp())


def test_rel_minutes():
    due, rest = parse_when("30分钟后 买酱油", NOW)
    assert rest == "买酱油"
    assert due == _ts(NOW + timedelta(minutes=30))


def test_rel_hours_and_days_and_seconds():
    due, rest = parse_when("2小时后 起锅", NOW)
    assert due == _ts(NOW + timedelta(hours=2)) and rest == "起锅"
    due, rest = parse_when("3天之后 复查", NOW)
    assert due == _ts(NOW + timedelta(days=3)) and rest == "复查"
    due, rest = parse_when("90秒后 出锅", NOW)
    assert due == _ts(NOW + timedelta(seconds=90)) and rest == "出锅"


def test_today_time_passed_goes_tomorrow():
    due, rest = parse_when("今天 8:00 交作业", NOW)  # now = 10:00
    expected = NOW.replace(hour=8, minute=0) + timedelta(days=1)
    assert due == _ts(expected) and rest == "交作业"


def test_today_time_future_stays_today():
    due, rest = parse_when("21:30 吃药", NOW)
    assert due == _ts(NOW.replace(hour=21, minute=30)) and rest == "吃药"


def test_tomorrow_explicit_not_bumped():
    due, rest = parse_when("明天 7:30 跑步", NOW)
    expected = NOW.replace(hour=7, minute=30) + timedelta(days=1)
    assert due == _ts(expected) and rest == "跑步"


def test_day_after_and_three_days():
    due, rest = parse_when("后天 9点 值日", NOW)
    assert due == _ts(NOW.replace(hour=9) + timedelta(days=2)) and rest == "值日"
    due, rest = parse_when("大后天 9点 值日", NOW)
    assert due == _ts(NOW.replace(hour=9) + timedelta(days=3)) and rest == "值日"


def test_half_and_minute_forms():
    due, rest = parse_when("8点半 开会", NOW)  # 已过 → 明天
    expected = NOW.replace(hour=8, minute=30) + timedelta(days=1)
    assert due == _ts(expected) and rest == "开会"
    due, rest = parse_when("8点30 扫除", NOW.replace(hour=8, minute=10))
    assert due == _ts(NOW.replace(hour=8, minute=10).replace(hour=8, minute=30))
    assert rest == "扫除"


def test_fullwidth_digits_nfkc():
    due, rest = parse_when("３０分钟后 买酱油", NOW)
    assert due == _ts(NOW + timedelta(minutes=30)) and rest == "买酱油"


def test_invalid_hour_and_minute_fall_through_to_plain_todo():
    for text in ("25点 起床", "8点61分 睡觉"):
        due, rest = parse_when(text, NOW)
        assert due is None
        assert rest == text, "无效时间必须原文返回当纯待办"


def test_invalid_calendar_date_falls_through():
    due, rest = parse_when("2月30日 检查", NOW)  # 不存在的日期
    assert due is None and rest == "2月30日 检查"


def test_date_passed_this_year_goes_next_year():
    due, rest = parse_when("10月1日 交报告", NOW)
    expected = NOW.replace(year=2027, month=10, day=1, hour=0, minute=0)
    assert due == _ts(expected) and rest == "交报告"


def test_date_with_time():
    due, rest = parse_when("10月1日 9:00 交报告", NOW)
    expected = NOW.replace(year=2027, month=10, day=1, hour=9, minute=0)
    assert due == _ts(expected) and rest == "交报告"


def test_no_time_returns_original():
    due, rest = parse_when("买 3 斤苹果", NOW)
    assert due is None and rest == "买 3 斤苹果"
    due, rest = parse_when("找 3-4 个人搬桌子", NOW)
    assert due is None and rest == "找 3-4 个人搬桌子"


def test_strip_position_independent():
    a = parse_when("明天 8:00 交作业", NOW)
    b = parse_when("交作业 明天 8:00", NOW)
    assert a == b and a[1] == "交作业"


def test_empty_text():
    assert parse_when("", NOW) == (None, "")
    assert parse_when("   ", NOW) == (None, "")


def test_zero_duration_is_not_a_time():
    due, rest = parse_when("0分钟后 再看", NOW)
    assert due is None and rest == "0分钟后 再看"
