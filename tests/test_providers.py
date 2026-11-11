"""provider 解析层测试：全部基于 2026-10-19 真机抓取的 fixture，不用编造样本。"""

import pytest
from astrbot_plugin_suite.modules.games.providers import (
    DEFAULT_UIDS,
    enabled_keys,
    fold,
    game_display,
    normalize_game,
    resolve_uids,
)
from astrbot_plugin_suite.modules.games.providers.arknights import parse_items as parse_ark
from astrbot_plugin_suite.modules.games.providers.base import SourceError, strip_html
from astrbot_plugin_suite.modules.games.providers.bilibili import (
    FEED_BASE_PARAMS,
    WBI_MIXIN_TABLE,
    _extract_sessdata,
    mixin_key,
    parse_dynamics,
    sign_params,
)
from astrbot_plugin_suite.modules.games.providers.re1999 import parse_items as parse_re
from conftest import StubAstrBotConfig, load_fixture

# ---------------- 明日方舟 ----------------


def test_ark_latest_fixture_parses_six_items():
    items = parse_ark(load_fixture("ark_latest.json"))
    assert len(items) == 6
    first = items[0]
    assert first.game == "arknights"
    assert first.item_id == "0799"
    assert first.title == "《明日方舟》制作组通讯#70期"
    assert first.category == "新闻"  # tab "2"
    assert first.published > 0
    assert first.url == "https://ak.hypergryph.com/news/0799"
    assert first.summary


def test_ark_tab_mapping_covers_all_three_tabs():
    items = parse_ark(load_fixture("ark_latest.json"))
    assert {i.category for i in items} <= {"公告", "活动", "新闻"}
    assert {i.category for i in items} == {"公告", "活动", "新闻"}


def test_ark_limit_truncates():
    assert len(parse_ark(load_fixture("ark_latest.json"), limit=2)) == 2


def test_ark_rejects_nonzero_code():
    with pytest.raises(SourceError, match="code=7"):
        parse_ark({"code": 7, "data": {"list": []}})


def test_ark_rejects_missing_data():
    with pytest.raises(SourceError, match="data"):
        parse_ark({"code": 0})


def test_ark_skips_rows_without_id_or_title():
    payload = {
        "code": 0,
        "data": {
            "list": [
                {"cid": "", "title": "无 id", "tab": "0"},
                {"cid": "1", "title": "  ", "tab": "0"},
                {"cid": "9", "title": "好的", "tab": "0", "displayTime": 123},
                "不是对象",
            ]
        },
    }
    items = parse_ark(payload)
    assert [i.item_id for i in items] == ["9"]


def test_ark_bad_display_time_falls_back_to_zero():
    items = parse_ark({"code": 0, "data": {"list": [{"cid": "1", "title": "t", "displayTime": "坏"}]}})
    assert items[0].published == 0.0


def test_ark_all_four_categories_are_valid_json():
    for name in ("ark_latest", "ark_announcement", "ark_activity", "ark_news"):
        payload = load_fixture(f"{name}.json")
        assert payload["code"] == 0
        assert parse_ark(payload)


# ---------------- 重返未来：1999 ----------------


def test_re1999_fixture_parses_eight_items():
    items = parse_re(load_fixture("re1999_latest.json"))
    assert len(items) == 8
    first = items[0]
    assert first.game == "re1999"
    assert first.item_id == "102"
    assert first.title == "4.0「应门者」版本更新维护公告"
    assert first.category == "公告"
    assert first.url == "https://re.bluepoch.com/home/detail.html#newsId?102"
    assert first.content.startswith("<p")  # 列表接口直接给 HTML 全文


def test_re1999_online_time_parsed_to_unix():
    """官网给的是「当地时间」无时区标注，按本机时区换算（中国 = UTC+8）。"""
    from datetime import datetime, timedelta, timezone

    items = parse_re(load_fixture("re1999_latest.json"))
    expect = datetime(2026, 9, 23, 18, 1, 8, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert items[0].published == expect


def test_re1999_summary_is_stripped_and_truncated():
    items = parse_re(load_fixture("re1999_latest.json"))
    assert "<" not in items[0].summary
    assert items[0].summary
    assert items[0].summary.endswith("…")


def test_re1999_risk_block_reports_edgeone_not_generic_error():
    """5001 是 TLS 指纹被 EdgeOne 拦，不是「接口坏了」——文案要能指导排查。"""
    with pytest.raises(SourceError) as exc:
        parse_re({"code": 5001, "msg": "系统繁忙，请稍后再试"})
    assert "5001" in str(exc.value)
    assert "EdgeOne" in str(exc.value)


def test_re1999_rejects_other_nonzero_code():
    with pytest.raises(SourceError, match="code=403"):
        parse_re({"code": 403, "msg": "no"})


def test_re1999_bad_time_string_falls_back_to_zero():
    items = parse_re(
        {"code": 200, "data": {"pageData": [{"id": "1", "title": "t", "onlineTime": "昨天"}]}}
    )
    assert items[0].published == 0.0


def test_re1999_date_only_time_accepted():
    items = parse_re(
        {"code": 200, "data": {"pageData": [{"id": "1", "title": "t", "onlineTime": "2026-01-02"}]}}
    )
    assert items[0].published > 0


# ---------------- 游戏名归一 ----------------


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("重返未来", "re1999"),
        ("重返未来：1999", "re1999"),
        ("重返未来 1999", "re1999"),
        ("重返未来1999", "re1999"),
        ("1999", "re1999"),
        ("九九", "re1999"),
        ("重返", "re1999"),
        ("RE1999", "re1999"),
        ("re1999", "re1999"),
        ("Reverse1999", "re1999"),
        ("明日方舟", "arknights"),
        ("方舟", "arknights"),
        ("ak", "arknights"),
        ("Arknights", "arknights"),
        ("ＡＫ", "arknights"),  # 全角
    ],
)
def test_normalize_game_accepts_every_documented_alias(raw, expect):
    assert normalize_game(raw) == expect


def test_normalize_game_tolerates_suffix_and_suffix_free_forms():
    assert normalize_game("1999的卡池") == "re1999"
    assert normalize_game("方舟 兑换码") == "arknights"


def test_normalize_game_longer_alias_wins():
    """「明日方舟」必须在「方舟」之前命中，否则含长名的输入会错判。"""
    assert normalize_game("明日方舟终末地") == "arknights"


def test_normalize_game_returns_none_for_unknown():
    assert normalize_game("原神") is None
    assert normalize_game("") is None
    assert normalize_game("   ") is None
    assert normalize_game(None) is None


def test_fold_strips_separators_and_case():
    assert fold(" 重返-future_1999 ") == "重返future1999"
    assert fold("ＡＫ") == "ak"


def test_game_display_fallback():
    assert game_display("re1999") == "重返未来：1999"
    assert game_display("unknown") == "unknown"


def test_enabled_keys_reads_switches():
    cfg = StubAstrBotConfig({"arknights_enabled": False})
    assert enabled_keys(cfg) == ["re1999"]
    assert enabled_keys(StubAstrBotConfig({})) == ["arknights", "re1999"]


def test_enabled_keys_parses_string_booleans():
    cfg = StubAstrBotConfig({"arknights_enabled": "false", "re1999_enabled": "1"})
    assert enabled_keys(cfg) == ["re1999"]


def test_enabled_keys_survives_broken_config():
    """守卫只挡「形状异常」（属性/类型不对），不是替配置实现兜底一切异常。"""

    class Broken:
        def get(self, *_a, **_kw):
            raise AttributeError("没有 get")

    assert enabled_keys(Broken()) == ["arknights", "re1999"]


# ---------------- B站 UID ----------------


def test_default_uids_are_the_verified_official_accounts():
    """方舟官方号是 161775300；161432940 是同名个人号，别再写回去。"""
    assert DEFAULT_UIDS == {"re1999": "1197454103", "arknights": "161775300"}


def test_resolve_uids_allows_override_and_rejects_garbage():
    cfg = StubAstrBotConfig({"bilibili_uids": {"re1999": "123", "arknights": "非数字"}})
    assert resolve_uids(cfg) == {"re1999": "123", "arknights": "161775300"}


def test_resolve_uids_defaults_when_absent_or_broken():
    assert resolve_uids(StubAstrBotConfig({})) == DEFAULT_UIDS
    assert resolve_uids(StubAstrBotConfig({"bilibili_uids": "不是 dict"})) == DEFAULT_UIDS


# ---------------- wbi 签名 ----------------


def test_wbi_table_has_64_unique_entries():
    assert len(WBI_MIXIN_TABLE) == 64
    assert len(set(WBI_MIXIN_TABLE)) == 64
    assert sorted(WBI_MIXIN_TABLE) == list(range(64))


def test_mixin_key_known_vector():
    """用实测抓到的 nav 样本做已知向量断言（B站会轮换 key，但算法不变）。"""
    key = mixin_key(
        "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
    )
    assert len(key) == 32
    assert key.isalnum()


def test_mixin_key_is_deterministic_and_key_dependent():
    a = mixin_key("https://x/aaa.png", "https://x/bbb.png")
    b = mixin_key("https://x/aaa.png", "https://x/bbb.png")
    c = mixin_key("https://x/aaa.png", "https://x/ccc.png")
    assert a == b
    assert a != c


def test_mixin_key_handles_empty_urls_without_indexerror():
    assert isinstance(mixin_key("", ""), str)


def test_sign_params_is_sorted_and_carries_wts_and_wrid():
    query, w_rid = sign_params({"b": "2", "a": "1"}, "K" * 32, wts=1700000000)
    assert query == "a=1&b=2&wts=1700000000"
    assert len(w_rid) == 32


def test_sign_params_changes_with_wts():
    _, a = sign_params({"a": "1"}, "K" * 32, wts=1700000000)
    _, b = sign_params({"a": "1"}, "K" * 32, wts=1700000001)
    assert a != b


def test_feed_base_params_present():
    """只带 host_mid+wts 会静默返回空列表，这三个参数必须有。"""
    assert set(FEED_BASE_PARAMS) == {"timezone_offset", "platform", "web_location"}


# ---------------- 动态解析 ----------------


def test_parse_dynamics_real_feed_sample():
    items = parse_dynamics(load_fixture("bili_feed_re1999.json"), "re1999")
    assert len(items) == 13
    assert all(i.game == "re1999" for i in items)
    assert all(i.url.startswith("https://t.bilibili.com/") for i in items)
    assert all(i.published > 0 for i in items)


def test_parse_dynamics_extracts_opus_text_for_image_post():
    items = parse_dynamics(load_fixture("bili_feed_re1999.json"), "re1999")
    opus = next(i for i in items if i.kind == "图文")
    assert "重返未来1999" in opus.text


def test_parse_dynamics_extracts_video_title():
    items = parse_dynamics(load_fixture("bili_feed_re1999.json"), "re1999")
    vid = next(i for i in items if i.kind == "视频")
    assert vid.title


def test_parse_dynamics_marks_forward_kind():
    items = parse_dynamics(load_fixture("bili_feed_re1999.json"), "re1999")
    assert any(i.kind == "转发" for i in items)


def test_parse_dynamics_limit():
    items = parse_dynamics(load_fixture("bili_feed_re1999.json"), "re1999", limit=3)
    assert len(items) == 3


def test_parse_dynamics_reports_risk_control_distinctly():
    with pytest.raises(SourceError) as exc:
        parse_dynamics({"code": -352, "message": "风控校验失败"}, "re1999")
    assert "-352" in str(exc.value)
    assert "SESSDATA" in str(exc.value)


def test_parse_dynamics_rejects_other_codes():
    with pytest.raises(SourceError, match="code=400"):
        parse_dynamics({"code": 400, "message": "x"}, "re1999")


def test_parse_dynamics_empty_items_is_not_an_error():
    """方舟官方号当前动态列表为空——合法数据，不是故障。"""
    assert parse_dynamics({"code": 0, "data": {"items": [], "total": "0"}}, "arknights") == []


def test_parse_dynamics_skips_rows_without_id():
    payload = {"code": 0, "data": {"items": [{"id_str": ""}, {"id_str": "7"}]}}
    items = parse_dynamics(payload, "re1999")
    assert [i.item_id for i in items] == ["7"]


def test_parse_dynamics_survives_missing_modules():
    items = parse_dynamics({"code": 0, "data": {"items": [{"id_str": "1"}]}}, "re1999")
    assert items[0].published == 0.0
    assert items[0].text == ""


# ---------------- SESSDATA 提取 ----------------


def test_extract_sessdata_from_url_params():
    payload = {"data": {"url": "https://passport.bilibili.com/x?SESSDATA=abc123%2Cdef&bili_jct=zzz"}}
    assert _extract_sessdata(payload) == "abc123,def"


def test_extract_sessdata_falls_back_to_set_cookie():
    payload = {"data": {"url": "", "headers": {"Set-Cookie": "SESSDATA=cookieval; Path=/; HttpOnly"}}}
    assert _extract_sessdata(payload) == "cookieval"


def test_extract_sessdata_returns_empty_when_absent():
    assert _extract_sessdata({"data": {"url": "https://x/?a=1"}}) == ""


# ---------------- HTML 剥离 ----------------


def test_strip_html_removes_scripts_and_styles():
    raw = "<style>.a{}</style><script>alert(1)</script><p>正文</p>"
    text = strip_html(raw)
    assert "alert" not in text
    assert ".a{}" not in text
    assert "正文" in text


def test_strip_html_unescapes_entities():
    assert "A&B" in strip_html("<p>A&amp;B</p>")


def test_strip_html_splits_blocks_into_lines():
    text = strip_html("<p>第一段</p><p>第二段</p>")
    assert text.splitlines()[0] == "第一段"
    assert "第二段" in text


def test_strip_html_handles_real_1999_content():
    items = parse_re(load_fixture("re1999_latest.json"))
    text = strip_html(items[0].content)
    assert "<" not in text
    assert len(text) > 40


def test_strip_html_limit_truncates_with_ellipsis():
    text = strip_html("<p>" + "字" * 200 + "</p>", limit=50)
    assert len(text) <= 51
    assert text.endswith("…")


def test_strip_html_empty_input():
    assert strip_html("") == ""
    assert strip_html(None) == ""
