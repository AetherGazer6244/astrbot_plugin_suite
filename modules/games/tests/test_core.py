"""keywords / codes / formatting / storage 的纯逻辑测试。"""

import json
import time

import pytest
from astrbot_plugin_games_monitor import codes as C
from astrbot_plugin_games_monitor import formatting as F
from astrbot_plugin_games_monitor import keywords as K
from astrbot_plugin_games_monitor.providers import NewsItem
from astrbot_plugin_games_monitor.storage import MAX_SEEN_PER_SOURCE, Store
from conftest import load_fixture


def _item(**kw) -> NewsItem:
    base = {
        "game": "re1999",
        "item_id": "1",
        "title": "标题",
        "category": "公告",
        "published": time.time(),
        "url": "https://example.com/1",
    }
    base.update(kw)
    return NewsItem(**base)


# ==================== keywords ====================


def test_gacha_keywords_hit_real_titles():
    """方舟/1999 的卡池公告用词不同但都在同一张表里；普通活动公告不该误命中。"""
    assert K.is_gacha("[明日方舟]10月16日16:00闪断更新公告") is False
    assert K.is_gacha("限时寻访进行中，UP 干员登场") is True
    assert K.is_gacha("新干员「德雷克」上线") is True
    assert K.is_gacha("全新神秘学家加入") is True
    assert K.is_gacha("SideStory「昨日海」限时活动即将开启") is False


def test_version_keywords_hit_real_titles():
    assert K.is_version("3.9「重燃！流金之海」版本更新维护公告")
    assert K.is_version("10月16日16:00闪断更新公告")
    assert K.is_version("《明日方舟》制作组通讯#70期")
    assert K.is_version("2026箱中巡游·特别前瞻节目回顾")


def test_classify_is_case_insensitive():
    assert K.is_gacha("UP池开放") is True
    assert K.is_gacha("up池开放") is True


def test_classify_empty_text():
    assert K.is_gacha("") is False
    assert K.is_version(None) is False


def test_extract_time_range_from_real_ark_brief():
    """方舟活动公告里的真实格式：10月16日 16:00 - 10月30日 03:59。"""
    items = _parse_ark_briefs()
    ranges = K.extract_time_ranges(items[0])
    assert ranges == ["10月16日 16:00 - 10月30日 03:59"]


def test_extract_time_range_handles_awkward_spacing():
    text = "活动时间：09月29日 16:00 - 10月20日 03:59，活动说明如下"
    assert K.extract_time_ranges(text) == ["9月29日 16:00 - 10月20日 03:59"]


def test_extract_time_range_full_width_punctuation():
    text = "活动时间：10月16日 16：00 ~ 10月30日 03：59"
    assert K.extract_time_ranges(text) == ["10月16日 16:00 - 10月30日 03:59"]


def test_extract_time_range_single_date_for_maintenance_notice():
    text = "服务器将于2026年10月16日16:00 ~ 16:10期间进行闪断更新"
    assert K.extract_time_ranges(text) == ["2026年10月16日 16:00 - 16:10"]


def test_extract_time_range_ignores_dates_without_cue_word():
    """「封禁公示：2026年9月17日」不是活动时间，不能被当成卡池时间。"""
    assert K.extract_time_ranges("账号封禁公示：2026年9月17日更新") == []


def test_extract_time_range_can_skip_cue_requirement():
    text = "10月16日 16:00 - 10月30日 03:59"
    assert K.extract_time_ranges(text, require_cue=False) == ["10月16日 16:00 - 10月30日 03:59"]


def test_extract_time_range_falls_back_to_single_point():
    text = "维护时间：2026年10月09日06:00"
    assert K.extract_time_ranges(text) == ["2026年10月9日 06:00"]


def test_extract_time_range_limit_and_dedup():
    text = (
        "活动时间：10月01日 12:00 - 10月02日 12:00；"
        "活动时间：11月01日 12:00 - 11月02日 12:00；"
        "活动时间：10月01日 12:00 - 10月02日 12:00"
    )
    got = K.extract_time_ranges(text, limit=2)
    assert len(got) == 2
    assert got[0] != got[1]


def test_extract_time_range_empty_input():
    assert K.extract_time_ranges("") == []
    assert K.extract_time_ranges(None) == []


def test_maintenance_window_is_not_reported_as_activity_time():
    """真跑抓到的坑：1999 维护公告写「维护时间为2026年9月24日05:00前后」，
    混进 /游戏 X 卡池 会把停服时间当成卡池时间推给用户。"""
    text = "《重返未来：1999》计划将于9月24日5点进行停服更新维护，维护时间为2026年9月24日05:00前后"
    assert K.extract_time_ranges(text, cues=K.MAINTENANCE_CUES)
    assert K.extract_time_ranges(text, cues=K.ACTIVITY_CUES) == []


def test_activity_window_not_picked_up_by_maintenance_cues_only():
    text = "活动时间：10月16日 16:00 - 10月30日 03:59"
    assert K.extract_time_ranges(text, cues=K.ACTIVITY_CUES) == [
        "10月16日 16:00 - 10月30日 03:59"
    ]
    assert K.extract_time_ranges(text, cues=K.MAINTENANCE_CUES) == []


def test_cue_word_groups_are_disjoint_where_it_matters():
    assert "维护" not in K.ACTIVITY_CUES
    assert "闪断" not in K.ACTIVITY_CUES
    assert "活动时间" in K.ACTIVITY_CUES


def test_maintenance_title_regex_catches_real_titles():
    for title in (
        "4.0「应门者」版本更新维护公告",
        "[明日方舟]10月16日16:00闪断更新公告",
        "[明日方舟]10月09日06:00版本更新停机维护公告",
    ):
        assert K.MAINTENANCE_TITLE_RE.search(title), title
    for title in ("SideStory「昨日海」限时活动即将开启", "全新SideStory限时活动开启"):
        assert not K.MAINTENANCE_TITLE_RE.search(title), title


def test_pick_sorts_by_time_desc_and_limits():
    items = [
        _item(item_id="a", published=100, title="卡池开放"),
        _item(item_id="b", published=300, title="卡池预告"),
        _item(item_id="c", published=200, title="无关公告"),
        _item(item_id="d", published=250, title="新干员上线"),
    ]
    got = K.pick(items, K.GACHA_KEYWORDS, limit=2)
    assert [i.item_id for i in got] == ["b", "d"]


def test_pick_prefers_title_matches_over_body_mentions():
    """真跑发现的坑：整篇正文里到处是「开启/联动」，只看全文会误选。"""
    items = [
        _item(
            item_id="broadcast",
            published=300,
            title="1999制作组播报 第二十七期",
            summary="正文里提到某联动活动已经开启",
        ),
        _item(item_id="real", published=100, title="SideStory 限时活动开启"),
    ]
    got = K.pick(items, K.ACTIVITY_KEYWORDS, limit=5)
    assert [i.item_id for i in got] == ["real"]


def test_pick_falls_back_to_body_when_no_title_matches():
    items = [
        _item(item_id="x", published=300, title="某则播报", summary="正文里提到卡池开放了"),
    ]
    got = K.pick(items, K.GACHA_KEYWORDS, limit=5)
    assert [i.item_id for i in got] == ["x"]


def _parse_ark_briefs() -> list[str]:
    payload = load_fixture("ark_activity.json")
    return [str(i.get("brief") or "") for i in payload["data"]["list"]]


# ==================== codes ====================


@pytest.mark.parametrize(
    "text,expect",
    [
        ("兑换码：ABCDEFGH", "ABCDEFGH"),
        ("兑换码 ABCDEFGH", "ABCDEFGH"),
        ("礼包码：XYZ12345", "XYZ12345"),
        ("激活码：QQQQWWWW", "QQQQWWWW"),
        ("码：ABC123456", "ABC123456"),
        ("CDK: ZZZ99999", "ZZZ99999"),
        ("redeem code: TEST1234", "TEST1234"),
    ],
)
def test_code_extraction_matrix(text, expect):
    hits = C.extract_codes(text, "re1999", "bilibili", "https://t.bilibili.com/1")
    assert [h.code for h in hits] == [expect]


def test_code_extraction_handles_multiple_codes_in_one_post():
    text = "兑换码：AAAA1111，兑换码：BBBB2222，礼包码 CCCC3333"
    hits = C.extract_codes(text, "re1999", "bilibili")
    assert [h.code for h in hits] == ["AAAA1111", "BBBB2222", "CCCC3333"]


def test_code_extraction_dedups_case_insensitively():
    hits = C.extract_codes("兑换码：abc12345\n兑换码：ABC12345", "re1999", "bilibili")
    assert len(hits) == 1


def test_code_extraction_respects_limit():
    text = " ".join(f"兑换码：CODE{i:04d}" for i in range(20))
    assert len(C.extract_codes(text, "re1999", "bilibili", limit=5)) == 5


def test_code_extraction_rejects_short_and_long_tokens():
    assert C.extract_codes("兑换码：ABC", "re1999", "b") == []
    assert C.extract_codes("兑换码：" + "A" * 21, "re1999", "b") == []


def test_code_extraction_does_not_match_chinese_words_after_cue():
    assert C.extract_codes("兑换码有效期七天，请尽快兑换", "re1999", "b") == []


def test_code_extraction_filters_known_noise_words():
    hits = C.extract_codes("兑换码：HYPERGRYPH", "re1999", "b")
    assert hits == []


def test_code_extraction_strips_html_when_asked():
    text = "<p>兑换码：<strong>ABCD1234</strong></p>"
    assert C.extract_codes(text, "re1999", "notice", html=True)[0].code == "ABCD1234"


def test_code_expire_hint_from_explicit_date():
    hits = C.extract_codes("兑换码：ABCD1234，有效期至2026年12月31日", "re1999", "b")
    assert hits[0].expire_hint == "2026年12月31日"


def test_code_expire_hint_normalizes_spacing():
    hits = C.extract_codes("兑换码：ABCD1234，截止 11月5日 24:00", "re1999", "b")
    assert hits[0].expire_hint == "11月5日 24:00"
    hits2 = C.extract_codes("兑换码：ABCD1234，截止11月5日24:00", "re1999", "b")
    assert hits2[0].expire_hint == "11月5日 24:00"


def test_code_context_is_captured():
    hits = C.extract_codes("大家注意啦兑换码：ABCD1234 快去兑换", "re1999", "b")
    assert "兑换码" in hits[0].context


def test_code_extraction_empty_text():
    assert C.extract_codes("", "re1999", "b") == []
    assert C.extract_codes(None, "re1999", "b") == []


def test_parse_expire_handles_year_and_month_day_only():
    assert C._parse_expire("2026年12月31日") > time.time()
    assert C._parse_expire("1月5日") > time.time()  # 最近的将来


def test_parse_expire_rolls_over_24h():
    """"24:00" 要归一到次日零点，否则会算成当天 00:00（早了一天过期）。"""
    got = C._parse_expire("2026年12月31日 24:00")
    import datetime as dt

    expect = dt.datetime(2027, 1, 1, 0, 0).timestamp()
    assert got == expect


def test_parse_expire_rejects_garbage():
    assert C._parse_expire("不是日期") == 0.0
    assert C._parse_expire("") == 0.0
    assert C._parse_expire("13月99日") == 0.0


def test_merge_into_adds_new_and_keeps_found_time_of_known():
    now = time.time()
    first = C.extract_codes("兑换码：AAAA1111", "re1999", "bilibili", "u1")
    lib = C.merge_into([], first, now, 24)
    assert len(lib) == 1
    assert lib[0]["code"] == "AAAA1111"

    again = C.merge_into(lib, C.extract_codes("兑换码：AAAA1111", "re1999", "notice", "u2"), now, 24)
    assert len(again) == 1
    assert again[0]["found_at"] == now
    assert set(again[0]["sources"]) == {"bilibili", "notice"}


def test_merge_into_keeps_only_unexpired():
    now = time.time()
    lib = [{"code": "OLD11111", "expire_at": now - 10, "found_at": now - 100}]
    fresh = C.merge_into(lib, C.extract_codes("兑换码：NEW22222", "re1999", "b"), now, 24)
    assert [r["code"] for r in fresh] == ["NEW22222"]


def test_merge_into_uses_ttl_fallback_when_no_expiry_hint():
    now = time.time()
    lib = C.merge_into([], C.extract_codes("兑换码：AAAA1111", "re1999", "b"), now, 6)
    assert now + 6 * 3600 - 5 <= lib[0]["expire_at"] <= now + 6 * 3600 + 5


def test_merge_into_sorts_newest_first():
    now = time.time()
    lib = [{"code": "OLD11111", "expire_at": now + 9999, "found_at": now - 500}]
    merged = C.merge_into(lib, C.extract_codes("兑换码：NEW22222", "re1999", "b"), now, 24)
    assert [r["code"] for r in merged] == ["NEW22222", "OLD11111"]


def test_merge_into_upgrades_expire_hint_when_first_missed_it():
    now = time.time()
    lib = [{"code": "AAAA1111", "expire_hint": "", "expire_at": now + 100, "found_at": now, "source": "bilibili", "sources": ["bilibili"]}]
    hits = C.extract_codes("兑换码：AAAA1111，有效期至2027年1月1日", "re1999", "notice", "u")
    merged = C.merge_into(lib, hits, now, 24)
    assert merged[0]["expire_hint"] == "2027年1月1日"


def test_merge_into_drops_malformed_records():
    lib = [{"code": ""}, {"nope": 1}, "字符串"]
    merged = C.merge_into(lib, [], time.time(), 24)
    assert merged == []


def test_merge_into_tolerates_bad_expire_field():
    lib = [{"code": "AAAA1111", "expire_at": "不是数字", "found_at": time.time()}]
    assert len(C.merge_into(lib, [], time.time(), 24)) == 1


def test_expiry_note_variants():
    now = time.time()
    assert "已过期" in C.expiry_note({"expire_at": now - 1})
    assert "不到 1 小时" in C.expiry_note({"expire_at": now + 600})
    assert "小时后失效" in C.expiry_note({"expire_at": now + 7200})
    assert "2027年" in C.expiry_note({"expire_at": now + 86400 * 60, "expire_hint": "2027年1月1日"})
    assert C.expiry_note({}) == C.NO_DATE_HINT
    assert C.expiry_note({"expire_at": "坏"}) == C.NO_DATE_HINT


def test_scan_item_strips_1999_html_content():
    payload = load_fixture("re1999_latest.json")
    raw = payload["data"]["pageData"][0]
    item = NewsItem(
        game="re1999",
        item_id=raw["id"],
        title=raw["title"],
        category="公告",
        published=0,
        url="u",
        summary="",
        content="<p>兑换码：<b>HTML1234</b> 有效期至2027年1月1日</p>",
    )
    hits = C.scan_item(item)
    assert [h.code for h in hits] == ["HTML1234"]
    assert hits[0].expire_hint == "2027年1月1日"


def test_scan_dynamics_uses_title_and_text():
    class Dyn:
        game = "re1999"
        title = "直播回放"
        text = "兑换码：DYN12345"
        url = "https://t.bilibili.com/9"

    assert [h.code for h in C.scan_dynamics(Dyn())] == ["DYN12345"]


# ==================== formatting ====================


def test_fmt_date_and_ago():
    ts = time.time() - 3600
    assert F.fmt_date(ts) != ""
    assert F.fmt_ago(ts) == "1 小时前"
    assert F.fmt_ago(time.time() - 300) == "5 分钟前"
    assert F.fmt_ago(time.time() - 86400 * 3) == "3 天前"
    assert F.fmt_ago(time.time() - 86400 * 30) != "30 天前"  # 超一周改给日期


def test_fmt_date_handles_bad_input():
    assert F.fmt_date("坏") == ""
    assert F.fmt_date(0) == ""
    assert F.fmt_ago("坏") == ""


def test_one_line_collapses_and_truncates():
    assert F.one_line("a\n\n  b\t c") == "a b c"
    assert F.one_line("字" * 100, limit=10).endswith("…")


def test_empty_code_text_is_fixed_phrase():
    """用户决策：空码固定说「没有兑换码QWQ」，不能变成报错口吻。"""
    assert F.EMPTY_CODE_TEXT == "没有兑换码QWQ"
    assert "没有兑换码QWQ" in F.codes_text([], "重返未来：1999")


def test_codes_text_renders_code_source_and_expiry():
    now = time.time()
    recs = [{"code": "AAAA1111", "sources": ["bilibili"], "url": "u", "expire_at": now + 7200}]
    text = F.codes_text(recs, "重返未来：1999")
    assert "AAAA1111" in text
    assert "bilibili" in text
    assert "小时后失效" in text


def test_news_text_contains_category_date_and_link():
    item = _item(title="4.0 版本更新维护公告", category="公告", url="https://x/1")
    text = F.news_text([item], "🎮 测试")
    assert "📢" in text
    assert "4.0 版本更新维护公告" in text
    assert "https://x/1" in text


def test_news_text_empty_state():
    assert F.EMPTY_NEWS_TEXT in F.news_text([], "🎮 测试")


def test_news_text_can_skip_summary():
    item = _item(summary="一段很长的摘要")
    assert "一段很长的摘要" not in F.news_text([item], "H", with_summary=False)


def test_gacha_lines_include_time_range_when_known():
    item = _item(item_id="7", title="恒远津梁限时活动")
    lines = F.gacha_lines([item], {"7": ["10月16日 16:00 - 10月30日 03:59"]})
    assert any("🕒 10月16日 16:00 - 10月30日 03:59" in ln for ln in lines)


def test_gacha_lines_omit_time_when_unknown():
    """抽不到时间就不硬凑——给错时间比不给更糟。"""
    item = _item(item_id="7", title="卡池")
    lines = F.gacha_lines([item], {})
    assert not any("🕒" in ln for ln in lines)


def test_dynamics_text_empty_state_for_silent_official_account():
    """方舟官方号动态列表当前为空——文案要说「没有」而不是「出错」。"""
    assert F.EMPTY_DYNAMIC_TEXT in F.dynamics_text([], "明日方舟")


def test_dynamics_text_renders_kind_title_and_link():
    class Dyn:
        game = "re1999"
        item_id = "1"
        title = "新版本 PV"
        text = "正文内容"
        published = time.time() - 600
        url = "https://t.bilibili.com/1"
        kind = "视频"

    text = F.dynamics_text([Dyn()], "重返未来：1999")
    assert "[视频]" in text
    assert "新版本 PV" in text
    assert "https://t.bilibili.com/1" in text


def test_summary_of_strips_html_for_llm():
    item = _item(title="标题", content="<p>正文里有<strong>标签</strong></p>")
    out = F.summary_of(item)
    assert "<" not in out
    assert "正文里有标签" in out


def test_help_text_reflects_config():
    text = F.help_text(["re1999", "arknights"], 7, 13, True)
    assert "7 分钟" in text
    assert "13 分钟" in text
    assert "已开启" in text
    assert "重返未来：1999" in text
    assert "明日方舟" in text


def test_help_text_when_no_source_enabled():
    assert "未启用任何数据源" in F.help_text([], 10, 15, False)
    assert "未开启" in F.help_text([], 10, 15, False)


def test_scope_text_no_subscription():
    assert "未订阅" in F.scope_text({})


def test_scope_text_shows_games_and_dynamics():
    text = F.scope_text({"games": ["re1999"], "dynamics": True})
    assert "重返未来：1999" in text
    assert "开" in text


def test_scope_text_no_games_but_present():
    assert "（无）" in F.scope_text({"games": [], "dynamics": False})


def test_push_text_is_compact():
    item = _item(summary="不该出现在推送里")
    text = F.push_text([item])
    assert "标题" in text
    assert "不该出现在推送里" not in text


# ==================== storage ====================


def test_store_roundtrip(tmp_path):
    path = tmp_path / "data.json"
    s = Store(path)
    s.mark_seen("arknights", ["1", "2"])
    s.set_subscription("u1", ["re1999"], True)
    s.set_codes("re1999", [{"code": "A", "found_at": 1}])
    s.save(force=True)

    s2 = Store(path)
    assert s2.seen_ids("arknights") == {"1", "2"}
    assert s2.subscription("u1")["games"] == ["re1999"]
    assert s2.codes("re1999")[0]["code"] == "A"


def test_store_marks_new_only(tmp_path):
    s = Store(tmp_path / "d.json")
    assert s.mark_seen("x", ["a"]) is True
    assert s.mark_seen("x", ["a"]) is False
    assert s.mark_seen("x", ["b"]) is True


def test_store_seen_is_capped(tmp_path):
    s = Store(tmp_path / "d.json")
    s.mark_seen("x", [str(i) for i in range(MAX_SEEN_PER_SOURCE + 50)])
    assert len(s.data["seen"]["x"]) == MAX_SEEN_PER_SOURCE
    # 保留的是最新的那些
    assert s.data["seen"]["x"][-1] == str(MAX_SEEN_PER_SOURCE + 49)


def test_store_forget_seen_allows_resend(tmp_path):
    s = Store(tmp_path / "d.json")
    s.mark_seen("x", ["a", "b"])
    s.forget_seen("x", ["a"])
    assert s.seen_ids("x") == {"b"}


def test_store_subscribers_filters_by_game(tmp_path):
    s = Store(tmp_path / "d.json")
    s.set_subscription("u1", ["re1999"], False)
    s.set_subscription("u2", ["arknights"], False)
    s.set_subscription("u3", ["re1999", "arknights"], True)
    assert [u for u, _ in s.subscribers("re1999")] == ["u1", "u3"]
    assert len(s.subscribers()) == 3


def test_store_del_subscription(tmp_path):
    s = Store(tmp_path / "d.json")
    s.set_subscription("u1", ["re1999"], False)
    assert s.del_subscription("u1") is True
    assert s.del_subscription("u1") is False


def test_store_muted_flag(tmp_path):
    s = Store(tmp_path / "d.json")
    s.set_subscription("u1", ["re1999"], False)
    assert s.is_muted("u1") is False
    s.data["subscriptions"]["u1"]["muted_until"] = time.time() + 600
    assert s.is_muted("u1") is True


def test_store_prune_drops_expired_codes_and_mutes(tmp_path):
    s = Store(tmp_path / "d.json")
    now = time.time()
    s.set_codes("re1999", [
        {"code": "OLD", "expire_at": now - 1},
        {"code": "NEW", "expire_at": now + 999},
    ])
    s.set_subscription("u1", ["re1999"], False)
    s.data["subscriptions"]["u1"]["muted_until"] = now - 5
    assert s.prune() == 2
    assert [r["code"] for r in s.codes("re1999")] == ["NEW"]
    assert "muted_until" not in s.subscription("u1")


def test_store_save_debounce(tmp_path):
    s = Store(tmp_path / "d.json")
    s.set_subscription("u1", ["re1999"], False)
    s.save()  # 首写：_last_saved_at=0，会真的落盘
    s.del_subscription("u1")
    s.save()  # 去抖窗口内：不落盘
    assert s.flush() is True
    assert Store(tmp_path / "d.json").subscription("u1") == {}


def test_store_quarantines_corrupt_file(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    s = Store(path)
    assert s.data["seen"] == {}
    assert list(tmp_path.glob("d.json.corrupt-*"))


def test_store_rejects_wrong_shape(tmp_path):
    path = tmp_path / "d.json"
    path.write_text('["列表"]', encoding="utf-8")
    assert Store(path).data["seen"] == {}


def test_store_accepts_legacy_missing_keys(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"seen": {"x": ["1"]}}), encoding="utf-8")
    s = Store(path)
    assert s.data["subscriptions"] == {}
    assert s.data["codes"] == {}
    assert s.seen_ids("x") == {"1"}


def test_store_codes_sorted_by_found_at_desc(tmp_path):
    s = Store(tmp_path / "d.json")
    s.set_codes("re1999", [{"code": "A", "found_at": 1}, {"code": "B", "found_at": 9}])
    s.set_codes("arknights", [{"code": "C", "found_at": 5}])
    assert [r["code"] for r in s.codes("re1999")] == ["B", "A"]
    assert len(s.codes()) == 3


def test_store_save_leaves_no_tmp_on_failure(tmp_path):
    """写盘失败时不能删掉目标文件以外的任何东西，旧数据必须保住。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "d.json"
    target.write_text('{"seen": {}}', encoding="utf-8")
    s = Store(target)
    s.mark_seen("x", ["1"])
    # 把 tmp 目标占成目录，模拟写临时文件失败
    (sub / "d.tmp").mkdir(exist_ok=True)
    with pytest.raises(OSError):
        s.save(force=True)
    assert target.read_text(encoding="utf-8") == '{"seen": {}}'
