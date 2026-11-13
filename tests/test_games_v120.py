"""v1.2.0 三件事的行为规格：/游戏 静音、pending 队列落盘、AI 摘要落盘。

- 静音：作用面 = 三类推送全体（公告/动态/兑换码），查询不受影响；
  群聊仅管理员（与订阅同款闸门）；时长上限 MAX_MUTE_HOURS 防手滑；
  「解除」随时可逆。
- pending 落盘：广播失败也要扛住重启——否则又是「码在库里但永远
  不再广播」的 v1.1.1 回归。补发前过滤已过期的码。
- 摘要落盘：重启后缓存不再清零；公告列表没变直接命中，变了自然 miss
  （key 一致性由 cache_key 保证，落盘只是把 (key, text) 搬进 store）。
"""

import asyncio
import time

from astrbot_plugin_suite.modules.games.providers import NewsItem
from astrbot_plugin_suite.modules.games.storage import Store
from conftest import (
    PRIVATE_UMO,
    FakeContext,
    FakeEvent,
    FakeProvider,
    FakeSource,
    make_games_plugin,
)

GROUP_UMO = "aiocqhttp:GroupMessage:555"


def _news(item_id, title, ts=None, game="re1999"):
    return NewsItem(
        game=game,
        item_id=item_id,
        title=title,
        category="公告",
        published=ts if ts is not None else time.time() - 60,
        url=f"https://example.com/{item_id}",
        summary="摘要内容",
        content="摘要内容",
    )


def _plugin(tmp_path, items=None, provider=None, config=None, ctx=None):
    src = FakeSource(items if items is not None else [_news("1", "公告A")])
    cfg = {"ai_summary_enabled": True}
    cfg.update(config or {})
    p = make_games_plugin(
        config=cfg,
        store=Store(tmp_path / "s.json"),
        sources={"re1999": src, "arknights": src},
        context=ctx or FakeContext(provider=provider),
    )
    p._src = src
    return p


# ==================== 静音指令 ====================


def test_mute_hours_then_state_then_clear(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    ev = FakeEvent(text="静音", umo=PRIVATE_UMO)
    # 静音 2 小时
    out1 = asyncio.run(p._handle_mute(ev, "2"))
    assert "已静音 2 小时" in out1 and "至 " in out1
    assert p.store.is_muted(PRIVATE_UMO) is True
    # 无参看状态
    out2 = asyncio.run(p._handle_mute(ev, ""))
    assert "静音至" in out2
    # 解除
    out3 = asyncio.run(p._handle_mute(ev, "解除"))
    assert "已恢复推送" in out3
    assert p.store.is_muted(PRIVATE_UMO) is False
    # 再看状态：未静音
    out4 = asyncio.run(p._handle_mute(ev, ""))
    assert "未静音" in out4


def test_mute_accepts_hour_suffix_and_clamps_max(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    ev = FakeEvent(text="静音")
    out = asyncio.run(p._handle_mute(ev, "2小时"))
    assert "已静音 2 小时" in out
    # 超上限夹取到 168：不报错、不多关
    out2 = asyncio.run(p._handle_mute(ev, "99999"))
    assert "已静音 168 小时" in out2
    from astrbot_plugin_suite.modules.games.service import MAX_MUTE_HOURS

    until = p.store.muted_until(PRIVATE_UMO)
    assert until <= time.time() + (MAX_MUTE_HOURS + 0.01) * 3600


def test_mute_zero_and_off_keywords_clear(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    ev = FakeEvent(text="静音")
    asyncio.run(p._handle_mute(ev, "3"))
    assert p.store.is_muted(PRIVATE_UMO)
    assert "已恢复推送" in asyncio.run(p._handle_mute(ev, "0"))
    asyncio.run(p._handle_mute(ev, "3"))
    assert "已恢复推送" in asyncio.run(p._handle_mute(ev, "off"))
    # 本来就没静音时的解除
    assert "本来就没静音" in asyncio.run(p._handle_mute(ev, "解除"))


def test_mute_invalid_arg_hint(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    ev = FakeEvent(text="静音")
    out = asyncio.run(p._handle_mute(ev, "明天"))
    assert "没认出" in out and "用法" in out
    assert not p.store.is_muted(PRIVATE_UMO)


def test_mute_requires_admin_in_group(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    pleb = FakeEvent(text="静音", umo=GROUP_UMO, private=False, admin=False)
    out = asyncio.run(p._handle_mute(pleb, "5"))
    assert "只有管理员" in out
    assert not p.store.is_muted(GROUP_UMO)
    # 状态查看不设闸门：普通群员也能看
    out2 = asyncio.run(p._handle_mute(pleb, ""))
    assert "未静音" in out2
    # 管理员可以
    boss = FakeEvent(text="静音", umo=GROUP_UMO, private=False, admin=True)
    out3 = asyncio.run(p._handle_mute(boss, "5"))
    assert "已静音" in out3
    assert p.store.is_muted(GROUP_UMO)


def test_mute_without_subscription_still_works(tmp_path):
    """没订阅也能静音：建空订阅条目（games=[]），不影响广播匹配。"""
    p = _plugin(tmp_path, ctx=FakeContext())
    ev = FakeEvent(text="静音")
    out = asyncio.run(p._handle_mute(ev, "4"))
    assert "已静音" in out
    sub = p.store.subscription(PRIVATE_UMO)
    assert sub["games"] == []
    assert p.store.subscribers("re1999") == []  # 空订阅不算订阅者
    # 之后订阅，静音保留（v1.1.2 语义）
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    assert p.store.is_muted(PRIVATE_UMO) is True


def test_muted_session_gets_no_news_push(tmp_path):
    ctx = FakeContext()
    items = [_news(str(i), f"公告{i}") for i in range(6)]
    p = _plugin(tmp_path, items=items, config={"push_enabled": True}, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    p.store.set_muted(PRIVATE_UMO, 5)
    asyncio.run(p._push_news("re1999", items, items))
    assert ctx.sent == []
    # 静音期间的公告照常记已读（解除后不补发——「别打扰」不是「替你攒着」）
    assert "1" in p.store.seen_ids("news:re1999")


def test_help_text_mentions_mute(tmp_path):
    p = _plugin(tmp_path, ctx=FakeContext())
    assert "静音" in p._help_text()


# ==================== pending 队列落盘 ====================


def _seed_pending(store, key="re1999", code="PEND123", expire_in=3600):
    rec = {
        "code": code,
        "sources": ["bilibili"],
        "url": "u",
        "expire_at": time.time() + expire_in,
        "found_at": time.time(),
    }
    store.set_pending_codes(key, [rec])
    return rec


def test_pending_survives_store_reload(tmp_path):
    """广播失败进 pending → 落盘 → 重建 Store（模拟重启）→ 队列还在。"""
    store = Store(tmp_path / "s.json")
    _seed_pending(store)
    store.save(force=True)
    reloaded = Store(tmp_path / "s.json")
    assert [r["code"] for r in reloaded.pending_codes("re1999")] == ["PEND123"]


def test_pending_expired_codes_filtered_before_retry(tmp_path):
    """补发前过滤已过期码：过期了再推就是骚扰。"""
    ctx = FakeContext()
    p = _plugin(tmp_path, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    _seed_pending(p.store, code="DEAD123", expire_in=-60)  # 已过期
    _seed_pending(p.store, code="LIVE1234", expire_in=3600)
    # 注意第二次 seed 覆盖了第一次（set 语义），单独再种一条进 re1999 的队列
    recs = p.store.pending_codes("re1999") + [
        {"code": "DEAD123", "sources": ["bilibili"], "url": "u",
         "expire_at": time.time() - 60, "found_at": time.time()}
    ]
    p.store.set_pending_codes("re1999", recs)
    asyncio.run(p._push_codes("re1999", []))
    sent_codes = [t for _u, t in ctx.sent if "LIVE1234" in t]
    assert sent_codes and "DEAD123" not in sent_codes[0]
    # 过期码被清出队列，活码送达也不再留
    assert p.store.pending_codes("re1999") == []


def test_pending_broadcast_failure_persists_via_force_save(tmp_path):
    """广播失败 → pending 落盘走 force：60s 去抖窗口不能吞掉它（v1.1.2 同款坑）。"""
    ctx = FakeContext(ok=False)
    p = _plugin(tmp_path, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    code = {"code": "NEWCOD11", "sources": ["bilibili"], "url": "u",
            "expire_at": time.time() + 3600, "found_at": time.time()}
    asyncio.run(p._push_codes("re1999", [code]))
    reloaded = Store(tmp_path / "s.json")  # 直接重开文件，不走内存对象
    assert [r["code"] for r in reloaded.pending_codes("re1999")] == ["NEWCOD11"]


# ==================== AI 摘要落盘 ====================


def test_digest_persisted_and_restored_across_restart(tmp_path):
    """生成成功 → 落盘；重建 Core + _restore_digests → 缓存命中不再打 LLM。"""
    items = [_news(str(i), f"公告{i}") for i in range(6)]
    p = _plugin(tmp_path, items=items, provider=FakeProvider(text="- 要点"))
    asyncio.run(p._generate_digest("re1999", items))
    # 落盘了
    saved = p.store.all_digests()
    assert "re1999" in saved and saved["re1999"][1] == "- 要点"
    # 模拟重启：新 Core 实例（同 store 文件），内存缓存为空
    p2 = _plugin(tmp_path, items=items, provider=FakeProvider(text="不应该再生成"))
    assert p2._digest_cache == {}
    p2._restore_digests()
    assert "re1999" in p2._digest_cache
    # 恢复后 _ensure_digest 直接命中，不 spawn 新任务
    out = asyncio.run(p2._ensure_digest("re1999", items, wait_seconds=0.05))
    assert out == "- 要点"
    assert p2._digest_tasks == {}


def test_digest_key_mismatch_after_content_change(tmp_path):
    """公告列表变了 → 落盘的 key 对不上 → miss → 正常触发重新生成。"""
    items_a = [_news("1", "公告A"), _news("2", "公告B")]
    p = _plugin(tmp_path, items=items_a, provider=FakeProvider(text="- A"))
    asyncio.run(p._generate_digest("re1999", items_a))
    p2 = _plugin(tmp_path, provider=FakeProvider(text="- 新要点"))
    p2._restore_digests()
    items_b = [_news("1", "公告A"), _news("3", "新公告C")]
    assert p2._digest_of("re1999", items_b) is None  # 列表变了不认旧摘要
    assert p2._digest_of("re1999", items_a) == "- A"  # 同列表照常命中


def test_digest_persist_failure_does_not_break_generation(tmp_path):
    """落盘异常只告警，内存缓存照写（生成结果不能因为存储抖动白费）。"""
    items = [_news("1", "公告A")]
    p = _plugin(tmp_path, items=items, provider=FakeProvider(text="- 要点"))

    def boom(*a, **k):
        raise OSError("disk full")

    p.store.set_digest = boom
    asyncio.run(p._generate_digest("re1999", items))
    assert p._digest_cache["re1999"][1] == "- 要点"


def test_store_all_digests_rejects_malformed(tmp_path):
    """落盘条目损坏（缺 ck/text）→ 忽略，不把坏数据装回缓存。"""
    store = Store(tmp_path / "s.json")
    store.data["digests"]["re1999"] = {"ck": ["1"], "text": "好"}
    store.data["digests"]["arknights"] = {"text": "缺ck"}
    store.data["digests"]["junk"] = "not-a-dict"
    out = store.all_digests()
    assert list(out) == ["re1999"]
    assert out["re1999"] == (("1",), "好")
