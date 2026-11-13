"""查询缓存与兑换码落盘（v1.1.2）的行为规格。

背景：1999 官网挂着 EdgeOne 风控（curl 的 TLS 指纹直接被拦 code=5001），
用户连问三次 /游戏 就是三次官网请求。缓存让 60 秒内的重复提问不再打官网；
轮询每轮刷新缓存，命中即最新不超过一个轮询周期。

红线回归：v1.1.1 重写 _broadcast_codes 时把 store.save() 弄丢了——
兑换码入库后只有「有订阅者且有新公告」才落盘，无订阅者时重启即丢。
现在入库的事实点（_absorb_*）自己负责落盘，测试锁死。
"""

import asyncio
import time

from astrbot_plugin_suite.modules.games.cache import TTLCache
from astrbot_plugin_suite.modules.games.providers import SourceError
from astrbot_plugin_suite.modules.games.providers.bilibili import DynamicItem
from astrbot_plugin_suite.modules.games.storage import Store
from conftest import PRIVATE_UMO, FakeBili, FakeContext, FakeSource, make_games_plugin
from test_integration import _news, _plugin

# ==================== TTLCache 单元 ====================


def test_ttl_cache_hit_and_expiry():
    c = TTLCache(ttl_seconds=60)
    c.put("k", ["a", "b"])
    assert c.get("k") == ["a", "b"]
    # 过期：把写入时间拨回 61 秒前
    c._data["k"] = (time.time() - 61, ["a", "b"])
    assert c.get("k") is None
    assert "k" not in c._data  # 过期项顺手清掉


def test_ttl_cache_min_items_semantics():
    c = TTLCache(ttl_seconds=60)
    c.put("k", ["a"])
    # 要的条数比缓存长 → 不算命中，让调用方重拉
    assert c.get("k", min_items=5) is None
    assert c.get("k", min_items=1) == ["a"]
    assert c.get("k", min_items=0) == ["a"]


def test_ttl_cache_disabled_when_ttl_zero():
    c = TTLCache(ttl_seconds=0)
    c.put("k", ["a"])
    assert c.get("k") is None  # put 也直接丢弃
    assert not c._data


def test_ttl_cache_evicts_oldest_when_full():
    c = TTLCache(ttl_seconds=60, max_entries=2)
    c.put("a", [1])
    time.sleep(0.01)
    c.put("b", [2])
    time.sleep(0.01)
    c.put("c", [3])
    assert c.get("a") is None  # 最老的被淘汰
    assert c.get("b") == [2]
    assert c.get("c") == [3]


# ==================== 查询路径吃缓存 ====================


def test_view_within_ttl_hits_cache_not_source(tmp_path):
    items = [_news(str(i), f"公告{i}") for i in range(15)]
    ctx = FakeContext()
    p = _plugin(tmp_path, items=items, ctx=ctx)
    asyncio.run(p._news_view(""))
    first = len(p._src.calls)
    assert first >= 1
    asyncio.run(p._news_view(""))
    # 第二次完全吃缓存，re1999 源一次都没多打
    assert len(p._src.calls) == first


def test_poll_refreshes_cache_so_view_needs_no_fetch(tmp_path):
    items = [_news(str(i), f"公告{i}") for i in range(15)]
    ctx = FakeContext()
    p = _plugin(tmp_path, config={"push_enabled": True}, items=items, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    asyncio.run(p._poll_news())
    poll_calls = len(p._src.calls)
    asyncio.run(p._news_view(""))
    # 轮询刚拉过 → 视图直接吃缓存，不打源（轮询失败被退避接管，与缓存无关）
    assert len(p._src.calls) == poll_calls


def test_cache_disabled_by_zero_config(tmp_path):
    items = [_news("1", "公告")]
    ctx = FakeContext()
    p = _plugin(tmp_path, config={"query_cache_seconds": 0}, items=items, ctx=ctx)
    asyncio.run(p._news_view(""))
    first = len(p._src.calls)
    asyncio.run(p._news_view(""))
    assert len(p._src.calls) > first  # 关缓存 = 每次真打官网


def test_failed_fetch_is_not_cached(tmp_path):
    ctx = FakeContext()
    p = _plugin(tmp_path, items=[], error=SourceError("官网 500"), ctx=ctx)
    out1 = asyncio.run(p._news_view(""))
    assert "暂时拉不到" in out1
    # 源恢复后必须真的重拉，而不是把失败冻结成缓存答案
    # （清掉退避：_safe_call 在退避期内根本不打源，那不是缓存要防的事）
    p._failure_backoff.clear()
    p._src.error = None
    p._src.items = [_news("1", "好了")]
    p.sources["arknights"].error = None
    p.sources["arknights"].items = [_news("1", "好了")]
    out2 = asyncio.run(p._news_view(""))
    assert "暂时拉不到" not in out2


def test_category_cache_shared_between_gacha_and_tool(tmp_path):
    # 32 条：gacha/工具都按 limit=30 拉，缓存列表必须够长才判定命中
    items = [_news(str(i), f"公告{i}") for i in range(32)]
    ctx = FakeContext()
    p = _plugin(tmp_path, items=items, ctx=ctx)
    asyncio.run(p._gacha_view("1999"))
    first = len(p._src.calls)
    from conftest import FakeEvent

    asyncio.run(p.tool_games_news(FakeEvent(), "1999"))
    # 同一个分类（最新）第二次走缓存
    assert len(p._src.calls) == first


# ==================== 兑换码入库即落盘（v1.1.1 回归锁） ====================


def test_codes_persist_even_without_subscribers(tmp_path):
    """无订阅者时轮询扫到的码也必须落盘——旧回归：只有广播路径才 save。"""
    item = _news("1", "前瞻公告", content="<p>兑换码：NEWCOD1234 有效期至2030年1月1日</p>")
    p = _plugin(tmp_path, config={"push_enabled": True}, items=[item], ctx=FakeContext())
    # 刻意不订阅任何会话
    asyncio.run(p._poll_news())
    reloaded = Store(tmp_path / "s.json")
    assert sorted(c["code"] for c in reloaded.codes("re1999")) == ["NEWCOD1234"]
    assert sorted(c["code"] for c in reloaded.codes("arknights")) == ["NEWCOD1234"]


# ==================== 静音状态不被订阅操作冲掉 ====================


def test_set_subscription_preserves_muted_until(tmp_path):
    store = Store(tmp_path / "s.json")
    store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    store.data["subscriptions"][PRIVATE_UMO]["muted_until"] = time.time() + 3600
    # 用户改订阅（加游戏/改动态开关）不应把静音无声清掉
    store.set_subscription(PRIVATE_UMO, ["re1999", "arknights"], False)
    assert store.is_muted(PRIVATE_UMO) is True
    sub = store.subscription(PRIVATE_UMO)
    assert sub["games"] == ["arknights", "re1999"]
    assert sub["dynamics"] is False


def test_dynamics_push_skips_muted_session(tmp_path):
    ctx = FakeContext()
    dyn = DynamicItem("re1999", "9", "PV", "正文", time.time(), "https://t.bilibili.com/9", "视频")
    p = make_games_plugin(
        config={"push_enabled": True},
        store=Store(tmp_path / "s.json"),
        sources={"re1999": FakeSource([]), "arknights": FakeSource([])},
        bili=FakeBili([dyn]),
        context=ctx,
    )
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    p.store.data["subscriptions"][PRIVATE_UMO]["muted_until"] = time.time() + 3600
    asyncio.run(p._poll_dynamics())
    assert ctx.sent == []
