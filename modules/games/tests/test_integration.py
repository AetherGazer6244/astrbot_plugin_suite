import asyncio
import time

import pytest
from astrbot_plugin_games_monitor import keywords as K
from astrbot_plugin_games_monitor import webui as W
from astrbot_plugin_games_monitor.providers import NewsItem, SourceError
from astrbot_plugin_games_monitor.providers.bilibili import DynamicItem
from astrbot_plugin_games_monitor.storage import Store

# 假数据源统一放 conftest，tools/smoke.py 用的也是同一份
from conftest import (
    GROUP_UMO,
    PRIVATE_UMO,
    FakeBili,
    FakeContext,
    FakeEvent,
    FakeLogin,
    FakeSource,
    make_plugin,
)


def _news(item_id, title, game="re1999", ts=None, summary="", content="", category="公告"):
    return NewsItem(
        game=game,
        item_id=item_id,
        title=title,
        category=category,
        published=ts if ts is not None else time.time(),
        url=f"https://example.com/{item_id}",
        summary=summary,
        content=content or summary,
    )





def _plugin(tmp_path, config=None, items=None, error=None, dyns=None, ctx=None, store=None):
    # 两个游戏各一份假源：v1.1.0 起轮询不再按订阅裁剪，共享实例会让
    # 「退避期内不重试」这类按游戏计数的断言互相污染
    src = FakeSource(items, error)
    src2 = FakeSource(items, error)
    cfg = {"ai_summary_enabled": False}  # 摘要测试专归 test_summary.py，这里默认关掉
    cfg.update(config or {})
    plugin = make_plugin(
        config=cfg,
        store=store if store is not None else Store(tmp_path / "s.json"),
        sources={"re1999": src, "arknights": src2},
        bili=FakeBili(dyns),
        context=ctx or FakeContext(),
    )
    plugin._src = src
    return plugin


def _run_cmd(plugin, event):
    """跑完命令生成器，返回用户看到的那段文本。"""

    async def go():
        async for _ in plugin.cmd_games(event):
            pass
        return event.result

    return asyncio.run(go())


# ==================== 指令路由（纯函数） ====================


@pytest.mark.parametrize(
    "raw,sub,rest",
    [
        ("", "", ""),
        ("1999", "", "1999"),
        ("方舟", "", "方舟"),
        ("1999 卡池", "卡池", "1999"),
        ("方舟 版本", "版本", "方舟"),
        ("1999 兑换", "兑换", "1999"),
        ("1999 兑换码", "兑换", "1999"),
        ("1999 码", "兑换", "1999"),
        ("方舟 兑换码", "兑换", "方舟"),
        ("卡池", "卡池", ""),
        ("帮助", "帮助", ""),
        ("help", "帮助", ""),
        ("订阅", "订阅", ""),
        ("订阅 全部", "订阅", "全部"),
        ("订阅 关闭", "订阅", "关闭"),
        ("订阅 1999", "订阅", "1999"),
        ("总结", "总结", ""),
        ("总结 1999", "总结", "1999"),
        ("摘要", "总结", ""),
        ("方舟 列表", "列表", "方舟"),
        ("列表", "列表", ""),
        ("版本更新", "版本", ""),
        ("方舟版本", "版本", "方舟"),
        ("  1999   卡池  ", "卡池", "1999"),
        ("RE1999 卡池", "卡池", "re1999"),
    ],
)
def test_parse_route(raw, sub, rest):
    assert K.parse_route(raw) == (sub, rest)


def test_parse_route_ignores_unrelated_text():
    assert K.parse_route("原神") == ("", "原神")


def test_subscribe_arg_helpers():
    assert K.is_subscribe_all("全部") and K.is_subscribe_all("all")
    assert K.is_subscribe_off("关闭") and K.is_subscribe_off("退订")
    assert not K.is_subscribe_off("全部")
    assert not K.is_subscribe_all("")


# ==================== /游戏 ====================


def test_cmd_news_without_args_returns_both_games(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "公告A")])
    out = _run_cmd(p, FakeEvent("游戏"))
    assert "重返未来：1999" in out
    assert "明日方舟" in out
    assert "公告A" in out


def test_cmd_news_with_game_arg_filters_to_one(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "公告A", game="re1999")])
    out = _run_cmd(p, FakeEvent("游戏 1999"))
    assert "重返未来：1999" in out
    assert "明日方舟" not in out


def test_cmd_news_respects_wake_prefix_already_stripped(tmp_path):
    """宿主已剥掉唤醒前缀，event 里只剩「游戏 …」。"""
    p = _plugin(tmp_path, items=[_news("1", "公告A")])
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "卡池" in out


def test_cmd_news_unknown_game_shows_hint_and_help(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "A")])
    out = _run_cmd(p, FakeEvent("游戏 原神"))
    assert "没认出是哪个游戏" in out
    assert "重返未来" in out


def test_cmd_news_disabled_source_is_explained(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "A")], config={"arknights_enabled": False})
    out = _run_cmd(p, FakeEvent("游戏 方舟"))
    assert "数据源当前是关闭的" in out


def test_cmd_news_partial_failure_reports_only_broken_source(tmp_path):
    good = FakeSource([_news("1", "好的公告")])
    bad = FakeSource(error=SourceError("官网 500"))
    p = make_plugin(
        config={},
        store=Store(tmp_path / "s.json"),
        sources={"re1999": good, "arknights": bad},
        context=FakeContext(),
    )
    out = _run_cmd(p, FakeEvent("游戏"))
    assert "好的公告" in out
    assert "明日方舟 暂时拉不到" in out


def test_cmd_news_respects_max_items_per_push(tmp_path):
    items = [_news(str(i), f"公告{i}", ts=time.time() - i) for i in range(10)]
    p = _plugin(tmp_path, items=items, config={"max_items_per_push": 3})
    out = _run_cmd(p, FakeEvent("游戏 1999"))
    assert [f"公告{i}" for i in range(3) if f"公告{i}" in out] == [
        f"公告{i}" for i in range(3)
    ]
    assert "公告3" not in out


def test_cmd_help_lists_usage(tmp_path):
    p = _plugin(tmp_path, items=[])
    out = _run_cmd(p, FakeEvent("游戏 帮助"))
    assert "/游戏" in out
    assert "卡池" in out
    assert "订阅" in out


def test_cmd_help_reflects_poll_interval(tmp_path):
    p = _plugin(tmp_path, items=[], config={"poll_interval_minutes": 7})
    assert "7 分钟" in _run_cmd(p, FakeEvent("游戏 help"))


# ==================== /游戏 <游戏> 卡池 / 版本 / 兑换 ====================


def test_cmd_gacha_shows_time_range(tmp_path):
    item = _news(
        "7",
        "限时寻访开启，新干员登场",
        summary="活动时间：10月16日 16:00 - 10月30日 03:59活动说明",
    )
    p = _plugin(tmp_path, items=[item])
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "🕒 10月16日 16:00 - 10月30日 03:59" in out


def test_cmd_gacha_without_gacha_match_falls_back_to_recent_activities(tmp_path):
    """卡池公告稀疏，关键词没命中时退给最近的活动，而不是干巴巴一句「没有」。"""
    p = _plugin(
        tmp_path,
        items=[_news("1", "SideStory「昨日海」限时活动即将开启")],
    )
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "昨日海" in out
    assert "没看到卡池相关的公告" in out


def test_cmd_gacha_reports_when_nothing_at_all_matches(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "账号封禁公示")])
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "既没有卡池公告，也没有限时活动公告" in out


def test_cmd_gacha_excludes_maintenance_notices_from_activity_list(tmp_path):
    """真跑抓到的坑：维护公告时间最新，会把真正的活动挤掉。"""
    items = [
        _news("1", "4.0 版本更新维护公告", summary="维护时间：2026年9月24日05:00", ts=time.time()),
        _news("2", "SideStory 限时活动开启", summary="活动时间：10月16日 16:00 - 10月30日 03:59",
              ts=time.time() - 100),
    ]
    p = _plugin(tmp_path, items=items)
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "SideStory" in out
    assert "版本更新维护公告" not in out


def test_cmd_gacha_never_shows_maintenance_window_as_activity_time(tmp_path):
    items = [
        _news("1", "4.0 版本更新维护公告",
              summary="维护时间为2026年9月24日05:00前后", ts=time.time()),
        _news("2", "限时活动开启", summary="活动时间：10月16日 16:00 - 10月30日 03:59",
              ts=time.time() - 100),
    ]
    p = _plugin(tmp_path, items=items)
    out = _run_cmd(p, FakeEvent("游戏 1999 卡池"))
    assert "🕒 10月16日 16:00 - 10月30日 03:59" in out
    assert "🕒 2026年9月24日" not in out


def test_cmd_version_shows_maintenance_window(tmp_path):
    """版本视图反之：维护时间正是用户来这儿要的东西。"""
    items = [
        _news("1", "10月16日16:00闪断更新公告",
              summary="计划将于2026年10月16日16:00 ~ 16:10期间进行服务器闪断更新", ts=time.time())
    ]
    p = _plugin(tmp_path, items=items)
    out = _run_cmd(p, FakeEvent("游戏 方舟 版本"))
    assert "🕒 2026年10月16日 16:00 - 16:10" in out


def test_cmd_gacha_merges_both_games_when_no_game_named(tmp_path):
    items = [_news("1", "寻访开启", game="re1999"), _news("2", "新干员上线", game="arknights")]
    p = _plugin(tmp_path, items=items)
    out = _run_cmd(p, FakeEvent("游戏 卡池"))
    assert "重返未来：1999" in out and "明日方舟" in out


def test_cmd_version_filters_by_keywords(tmp_path):
    items = [
        _news("1", "10月16日16:00闪断更新公告", ts=time.time()),
        _news("2", "账号封禁公示", ts=time.time() - 100),
    ]
    p = _plugin(tmp_path, items=items)
    out = _run_cmd(p, FakeEvent("游戏 方舟 版本"))
    assert "闪断更新公告" in out
    assert "账号封禁公示" not in out


def test_cmd_codes_empty_state_is_fixed_phrase(tmp_path):
    p = _plugin(tmp_path, items=[])
    out = _run_cmd(p, FakeEvent("游戏 1999 兑换"))
    assert "没有兑换码QWQ" in out


def test_cmd_codes_lists_stored_codes(tmp_path):
    store = Store(None) if False else Store(_tmp(tmp_path))
    store.set_codes(
        "re1999",
        [{"code": "ABC12345", "sources": ["bilibili"], "url": "u", "expire_at": time.time() + 7200}],
    )
    p = _plugin(tmp_path, items=[], store=store)
    out = _run_cmd(p, FakeEvent("游戏 1999 兑换"))
    assert "ABC12345" in out
    assert "bilibili" in out


def test_cmd_codes_arknights_empty_is_data_reality_not_error(tmp_path):
    p = _plugin(tmp_path, items=[])
    out = _run_cmd(p, FakeEvent("游戏 方舟 码"))
    assert "没有兑换码QWQ" in out
    assert "错误" not in out and "失败" not in out


def _tmp(tmp_path):
    return tmp_path / "codes.json"


# ==================== 订阅 ====================


def test_subscribe_all_in_private(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 全部", umo=PRIVATE_UMO))
    assert "已订阅全部" in out
    assert p.store.subscription(PRIVATE_UMO)["games"]


def test_subscribe_blocked_for_non_admin_in_group(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 全部", umo=GROUP_UMO, private=False, admin=False))
    assert "只有管理员" in out
    assert p.store.subscription(GROUP_UMO) == {}


def test_subscribe_allowed_for_admin_in_group(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 全部", umo=GROUP_UMO, private=False, admin=True))
    assert "已订阅全部" in out


def test_subscribe_refused_when_push_disabled(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": False})
    out = _run_cmd(p, FakeEvent("游戏 订阅 全部"))
    assert "关闭" in out
    assert p.store.subscription(PRIVATE_UMO) == {}


def test_subscribe_single_game(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 1999"))
    assert "已订阅 重返未来：1999" in out
    assert p.store.subscription(PRIVATE_UMO)["games"] == ["re1999"]


def test_subscribe_single_game_is_idempotent(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    _run_cmd(p, FakeEvent("游戏 订阅 1999"))
    _run_cmd(p, FakeEvent("游戏 订阅 九九"))
    assert p.store.subscription(PRIVATE_UMO)["games"] == ["re1999"]


def test_subscribe_off_removes_entry(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    _run_cmd(p, FakeEvent("游戏 订阅 全部"))
    out = _run_cmd(p, FakeEvent("游戏 订阅 关闭"))
    assert "已关闭" in out
    assert p.store.subscription(PRIVATE_UMO) == {}


def test_subscribe_off_when_not_subscribed(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 关闭"))
    assert "本来就没订阅" in out


def test_subscribe_without_args_shows_scope(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅"))
    assert "未订阅" in out


def test_subscribe_unknown_game(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, FakeEvent("游戏 订阅 原神"))
    assert "没认出" in out


def test_group_admin_fallback_passes_when_no_is_admin_attr(tmp_path):
    """宿主没挂 is_admin 时放行（拿不到判定接口，宁松勿紧）。"""

    class NoAdmin:
        def __init__(self, text, umo):
            self.message_str = text
            self.unified_msg_origin = umo
            self.result = None

        def get_message_str(self):
            return self.message_str

        def is_private_chat(self):
            return False

        def plain_result(self, text):
            self.result = text
            return text

    p = _plugin(tmp_path, config={"push_enabled": True})
    out = _run_cmd(p, NoAdmin("游戏 订阅 全部", GROUP_UMO))
    assert "已订阅全部" in out


# ==================== AI 工具 ====================


def test_ai_tool_news_returns_text_for_llm(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "4.0 版本更新维护公告", summary="正文")])
    out = asyncio.run(p.tool_games_news(FakeEvent(), "1999", "", ""))
    assert "重返未来：1999" in out
    assert "4.0 版本更新维护公告" in out


def test_ai_tool_news_category_validation(tmp_path):
    p = _plugin(tmp_path, items=[])
    out = asyncio.run(p.tool_games_news(FakeEvent(), "1999", "乱写", ""))
    assert "分类只能是" in out


def test_ai_tool_news_keyword_filter(tmp_path):
    items = [_news("1", "卡池开启"), _news("2", "账号封禁")]
    p = _plugin(tmp_path, items=items)
    out = asyncio.run(p.tool_games_news(FakeEvent(), "1999", "", "卡池"))
    assert "卡池开启" in out
    assert "账号封禁" not in out


def test_ai_tool_codes_empty_state(tmp_path):
    p = _plugin(tmp_path, items=[])
    out = asyncio.run(p.tool_games_codes(FakeEvent(), "方舟"))
    assert "没有兑换码QWQ" in out


def test_ai_tool_dynamics_empty_is_data_reality(tmp_path):
    p = _plugin(tmp_path, items=[], dyns=[])
    out = asyncio.run(p.tool_games_dynamics(FakeEvent(), "方舟"))
    assert "暂时没有可读取的动态" in out


def test_ai_tool_dynamics_renders_items(tmp_path):
    dyn = DynamicItem(
        game="re1999",
        item_id="1",
        title="新版本 PV",
        text="正文",
        published=time.time(),
        url="https://t.bilibili.com/1",
        kind="视频",
    )
    p = _plugin(tmp_path, items=[], dyns=[dyn])
    out = asyncio.run(p.tool_games_dynamics(FakeEvent(), "1999"))
    assert "[视频]" in out
    assert "新版本 PV" in out


def test_ai_tools_disabled_by_config(tmp_path):
    p = _plugin(tmp_path, items=[], config={"ai_tools_enabled": False})
    for coro in (
        p.tool_games_news(FakeEvent()),
        p.tool_games_codes(FakeEvent()),
        p.tool_games_dynamics(FakeEvent()),
    ):
        assert "已被插件配置关闭" in asyncio.run(coro)


def test_ai_tool_unknown_game_falls_back_to_both(tmp_path):
    p = _plugin(tmp_path, items=[_news("1", "公告")])
    out = asyncio.run(p.tool_games_news(FakeEvent(), "原神", "", ""))
    assert "重返未来：1999" in out and "明日方舟" in out


# ==================== 幕后提示 ====================


class Req:
    def __init__(self, prompt="", contexts=None):
        self.prompt = prompt
        self.system_prompt = prompt
        self.contexts = contexts or []


def test_hint_injected_on_topic_hit(tmp_path):
    p = _plugin(tmp_path, items=[])
    req = Req(prompt="系统提示")
    asyncio.run(p.inject_games_hint(FakeEvent("1999 最近有卡池吗"), req))
    assert "games_news" in req.system_prompt
    assert req.system_prompt.startswith("系统提示")


def test_hint_not_injected_without_topic(tmp_path):
    p = _plugin(tmp_path, items=[])
    req = Req(prompt="系统提示")
    asyncio.run(p.inject_games_hint(FakeEvent("今天天气怎么样"), req))
    assert req.system_prompt == "系统提示"


def test_hint_is_idempotent(tmp_path):
    p = _plugin(tmp_path, items=[])
    req = Req(prompt="p")
    asyncio.run(p.inject_games_hint(FakeEvent("方舟兑换码"), req))
    once = req.system_prompt
    asyncio.run(p.inject_games_hint(FakeEvent("方舟兑换码"), req))
    assert req.system_prompt == once


def test_hint_reads_context_history(tmp_path):
    p = _plugin(tmp_path, items=[])
    req = Req(prompt="p", contexts=[{"content": "前几天聊的明日方舟维护"}])
    asyncio.run(p.inject_games_hint(FakeEvent("嗯嗯"), req))
    assert "games_news" in req.system_prompt


def test_hint_reads_multimodal_context(tmp_path):
    p = _plugin(tmp_path, items=[])
    req = Req(prompt="p", contexts=[{"content": [{"type": "text", "text": "1999 卡池"}]}])
    asyncio.run(p.inject_games_hint(FakeEvent("嗯嗯"), req))
    assert "games_news" in req.system_prompt


def test_hint_disabled_by_config(tmp_path):
    p = _plugin(tmp_path, items=[], config={"ai_prompt_hint": False})
    req = Req(prompt="p")
    asyncio.run(p.inject_games_hint(FakeEvent("方舟卡池"), req))
    assert req.system_prompt == "p"


def test_hint_swallows_exceptions(tmp_path):
    p = _plugin(tmp_path, items=[])

    class Boom:
        @property
        def prompt(self):
            raise RuntimeError("坏")

    asyncio.run(p.inject_games_hint(FakeEvent("方舟"), Boom()))
    assert True  # 不抛即通过


# ==================== 轮询推送 ====================


def test_poll_pushes_new_items_to_subscribers(tmp_path):
    ctx = FakeContext()
    p = _plugin(
        tmp_path,
        config={"push_enabled": True},
        items=[_news("1", "新公告"), _news("2", "旧公告", ts=time.time() - 999)],
        ctx=ctx,
    )
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    p.store.mark_seen("news:re1999", ["2"])
    asyncio.run(p._poll_news())
    assert len(ctx.sent) == 1
    assert "新公告" in ctx.sent[0][1]
    assert "旧公告" not in ctx.sent[0][1]


def test_poll_marks_seen_only_after_success(tmp_path):
    ctx = FakeContext(ok=True)
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[_news("1", "新公告")], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_news())
    assert p.store.seen_ids("news:re1999") == {"1"}

    ctx2 = FakeContext(ok=False)
    p2 = _plugin(tmp_path / "b", config={"push_enabled": True}, items=[_news("1", "新公告")], ctx=ctx2)
    p2.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p2._poll_news())
    # 发送失败不该记账：电费插件的教训——失败不写 seen，下轮好重发
    assert p2.store.seen_ids("news:re1999") == set()


def test_poll_skips_game_without_subscribers(tmp_path):
    ctx = FakeContext()
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[_news("1", "新公告")], ctx=ctx)
    asyncio.run(p._poll_news())
    assert ctx.sent == []


def test_poll_respects_mute(tmp_path):
    ctx = FakeContext()
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[_news("1", "新公告")], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    p.store.data["subscriptions"][PRIVATE_UMO]["muted_until"] = time.time() + 600
    asyncio.run(p._poll_news())
    assert ctx.sent == []


def test_poll_absorbs_codes_from_notice_bodies(tmp_path):
    ctx = FakeContext()
    item = _news("1", "前瞻公告", content="<p>兑换码：NEWCOD1234 有效期至2030年1月1日</p>")
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[item], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_news())
    codes = p.store.codes("re1999")
    assert [c["code"] for c in codes] == ["NEWCOD1234"]
    assert any("新兑换码" in text for _u, text in ctx.sent)


def test_poll_backs_off_after_failure(tmp_path):
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[], error=SourceError("挂了"))
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_news())
    assert p._should_skip("news:re1999") is True
    asyncio.run(p._poll_news())  # 退避期内直接跳过，不再打数据源
    assert len(p._src.calls) == 1


def test_poll_backoff_resets_on_success(tmp_path):
    src = FakeSource([], error=SourceError("挂了"))
    p = make_plugin(config={"push_enabled": True}, store=Store(tmp_path / "s.json"),
                     sources={"re1999": src}, context=FakeContext())
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_news())
    assert p._should_skip("news:re1999") is True
    src.error = None
    src.items = [_news("1", "好了")]
    p._failure_backoff["news:re1999"]["until"] = 0
    asyncio.run(p._poll_news())
    assert p._should_skip("news:re1999") is False


def test_poll_dynamics_pushes_and_marks_seen(tmp_path):
    ctx = FakeContext()
    dyn = DynamicItem("re1999", "9", "PV", "正文", time.time(), "https://t.bilibili.com/9", "视频")
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[], dyns=[dyn], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_dynamics())
    assert "PV" in ctx.sent[0][1]
    assert p.store.seen_ids("bili:re1999") == {"9"}


def test_poll_dynamics_empty_feed_is_not_marked_seen(tmp_path):
    """方舟官方号动态列表为空是数据源现实，不能因此误判。"""
    ctx = FakeContext()
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[], dyns=[], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["arknights"], True)
    asyncio.run(p._poll_dynamics())
    assert ctx.sent == []
    assert p.store.seen_ids("bili:arknights") == set()


def test_poll_dynamics_backs_off_on_risk_control(tmp_path):
    p = _plugin(
        tmp_path,
        config={"push_enabled": True},
        items=[],
        dyns=[],
        ctx=FakeContext(),
    )
    p.bili.dynamics.error = SourceError("B站风控拦截（-352）")
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_dynamics())
    assert p._should_skip("bili:re1999") is True


def test_poll_dynamics_respects_dynamics_switch(tmp_path):
    ctx = FakeContext()
    dyn = DynamicItem("re1999", "9", "PV", "x", time.time(), "u", "视频")
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[], dyns=[dyn], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], False)
    asyncio.run(p._poll_dynamics())
    assert ctx.sent == []


def test_poll_dynamics_absorbs_codes(tmp_path):
    ctx = FakeContext()
    dyn = DynamicItem("re1999", "9", "直播回放", "兑换码：BILICODE1", time.time(), "u", "视频")
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[], dyns=[dyn], ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_dynamics())
    assert [c["code"] for c in p.store.codes("re1999")] == ["BILICODE1"]


# ==================== WebUI 扫码登录 ====================


def test_webui_registers_both_endpoints(tmp_path):
    calls = []

    class Ctx:
        def register_web_api(self, route, handler, methods, desc):
            calls.append((route, methods, desc))

    p = _plugin(tmp_path)
    p.context = Ctx()
    W.register(p)
    assert [c[0] for c in calls] == [
        "/astrbot_plugin_games_monitor/login/generate",
        "/astrbot_plugin_games_monitor/login/poll",
    ]
    assert all("astrbot_plugin_games_monitor" in c[0] for c in calls)


def test_webui_skips_when_host_unsupported(tmp_path):
    p = _plugin(tmp_path)
    p.context = object()
    W.register(p)  # 不抛即通过


def test_webui_generate_returns_key_and_url(tmp_path):
    p = _plugin(tmp_path)
    p.bili.login = FakeLogin(gen=("KEY1", "https://bilibili.com/qr/KEY1"))
    out = asyncio.run(p._web_generate())
    assert out["status"] == "ok"
    assert out["data"]["key"] == "KEY1"
    assert out["data"]["url"].startswith("https://")
    assert out["data"]["ttl"] == W.QRCODE_TTL_SECONDS


def test_webui_generate_error_shape(tmp_path):
    p = _plugin(tmp_path)
    p.bili.login = FakeLogin(gen=SourceError("B站挂了"))
    out = asyncio.run(p._web_generate())
    assert out["status"] == "error"
    assert "message" in out and "data" not in out


def test_webui_generate_without_client(tmp_path):
    p = _plugin(tmp_path)
    p.bili = None
    assert asyncio.run(p._web_generate())["status"] == "error"


def test_webui_poll_states(tmp_path):
    p = _plugin(tmp_path)
    for code, expect_login in ((86101, False), (86090, False), (86038, False)):
        p.bili.login = FakeLogin(poll_result=(code, ""))
        out = asyncio.run(W.poll(p, key="k")) if False else None
        # _web_poll 从 query 取 key，这里直接打 W.poll 的核心逻辑
        out = asyncio.run(_poll_with_key(p, code, ""))
        assert out["status"] == "ok"
        assert out["data"]["login"] is expect_login
        assert out["data"]["text"]


def test_webui_poll_success_saves_sessdata(tmp_path):
    p = _plugin(tmp_path)
    p.bili.login = FakeLogin(poll_result=(0, "SECRETVALUE"))
    out = asyncio.run(_poll_with_key(p, 0, "SECRETVALUE"))
    assert out["data"]["login"] is True
    assert p.config["bilibili_sessdata"] == "SECRETVALUE"
    assert p.bili.sessdata == "SECRETVALUE"
    # 🔒 响应里绝不能出现 SESSDATA 本身
    assert "SECRETVALUE" not in str(out)


def test_webui_poll_success_without_sessdata_is_not_claimed_as_login(tmp_path):
    p = _plugin(tmp_path)
    out = asyncio.run(_poll_with_key(p, 0, ""))
    assert out["data"]["login"] is False
    assert p.config.get("bilibili_sessdata", "") == ""


def test_webui_poll_local_expiry_shortcut(tmp_path):
    p = _plugin(tmp_path)
    p._qr_at = time.time() - (W.QRCODE_TTL_SECONDS + 10)
    p.bili.login = FakeLogin()  # 调 generate 会自增计数，用来验证短路
    out = asyncio.run(_poll_with_key(p, 86101, ""))
    assert out["data"]["code"] == 86038
    assert p.bili.login.gen_calls == 0


def test_webui_poll_missing_key(tmp_path, monkeypatch):
    p = _plugin(tmp_path)
    monkeypatch.setattr(W, "_read_query", lambda name, default="": default)
    out = asyncio.run(p._web_poll())
    assert out["status"] == "error"
    assert "key" in out["message"]


def test_webui_poll_backend_failure(tmp_path, monkeypatch):
    p = _plugin(tmp_path)
    monkeypatch.setattr(W, "_read_query", lambda name, default="": "k")
    p.bili.login = FakeLogin(poll_result=SourceError("B站挂了"))
    out = asyncio.run(p._web_poll())
    assert out["status"] == "error"


async def _poll_with_key(plugin, code, sessdata):
    """绕过 query 读取，直接驱动 poll 的核心逻辑。"""
    plugin.bili.login = FakeLogin(poll_result=(code, sessdata))
    old = W._read_query
    W._read_query = lambda name, default="": "K"
    try:
        return await W.poll(plugin)
    finally:
        W._read_query = old
