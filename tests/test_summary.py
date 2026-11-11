"""v1.1.0 AI 摘要：后台预生成、前台秒取、推送也总结。

架构红线（用户拍板）：
- 前台 /游戏 与推送**只读缓存**，正常路径绝不前台等 LLM 写完
- 缓存按「最新公告 id 元组」判版本，公告列表变了自动作废
- 生成失败**不缓存**，下一轮轮询自动重试
- 推送摘要基于全量最新列表，与查询缓存共用，不为推送单独多调 LLM
"""

import asyncio
import time

import pytest
from astrbot_plugin_suite.modules.games import service as M
from astrbot_plugin_suite.modules.games.providers import NewsItem
from astrbot_plugin_suite.modules.games.storage import Store
from astrbot_plugin_suite.modules.games.summarizer import wash
from conftest import PRIVATE_UMO, FakeContext, FakeProvider, FakeSource, make_games_plugin


def _news(item_id, title, ts=None, summary="", content="", game="re1999"):
    return NewsItem(
        game=game,
        item_id=item_id,
        title=title,
        category="公告",
        published=ts if ts is not None else time.time() - 60,
        url=f"https://example.com/{item_id}",
        summary=summary or "摘要内容",
        content=content or summary or "摘要内容",
    )


def _plugin(tmp_path, items=None, provider=None, config=None, ctx=None, sources=None):
    src = FakeSource(items if items is not None else [_news("1", "公告A")])
    cfg = {"ai_summary_enabled": True}
    cfg.update(config or {})
    p = make_games_plugin(
        config=cfg,
        store=Store(tmp_path / "s.json"),
        sources=sources if sources is not None else {"re1999": src, "arknights": src},
        context=ctx or FakeContext(provider=provider),
    )
    p._src = src
    return p


def _prewarm(p, key="re1999"):
    """直接跑完一次生成，把缓存填上（模拟后台已完成）。"""
    items = asyncio.run(p._latest(key, 8))
    p._digest_cache[key] = (M.cache_key(items[: p._digest_max_items()]), "- 已缓存要点")


# ==================== summarizer 纯函数 ====================


def test_build_digest_prompt_strips_urls_and_truncates():
    long = "很长的正文。" * 200
    items = [_news("1", "维护公告", content=f"详情 https://example.com/a\n{long}")]
    prompt = M.build_digest_prompt("重返未来：1999", items)
    assert "https://" not in prompt
    # 单条公告被截断，prompt 不会无限膨胀
    assert len(prompt) < 3000 + 500
    assert "维护公告" in prompt
    assert "重返未来：1999" in prompt
    assert "不要" in prompt  # 反编造指令在场


def test_wash_strips_urls_and_blank_lines():
    assert wash("看 https://a.b/c 这一页") == "看 这一页"
    assert "\n\n" not in wash("a\n\n\nb")
    assert wash("") == ""


def test_summarize_empty_text_raises():
    provider = FakeProvider(text="   ")
    with pytest.raises(ValueError):
        asyncio.run(M.summarize(provider, "prompt"))


def test_summarize_timeout():
    provider = FakeProvider(text="慢", delay=0.2)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(M.summarize(provider, "prompt", timeout=0.01))


def test_summarize_returns_completion_text():
    provider = FakeProvider(text="- 要点")
    assert asyncio.run(M.summarize(provider, "p")) == "- 要点"


# ==================== 生成与缓存 ====================


def test_generate_without_provider_is_noop(tmp_path):
    p = _plugin(tmp_path, provider=None)
    asyncio.run(p._generate_digest("re1999", [_news("1", "公告A")]))
    assert "re1999" not in p._digest_cache


def test_generate_failure_not_cached(tmp_path):
    provider = FakeProvider(error=RuntimeError("模型挂了"))
    p = _plugin(tmp_path, provider=provider)
    asyncio.run(p._generate_digest("re1999", [_news("1", "公告A")]))
    assert "re1999" not in p._digest_cache
    # 失败不缓存 → 再来一次还会重试（下一轮轮询的语义）
    asyncio.run(p._generate_digest("re1999", [_news("1", "公告A")]))
    assert len(provider.calls) == 2


def test_cache_hit_and_invalidation(tmp_path):
    provider = FakeProvider()
    p = _plugin(tmp_path, provider=provider)
    items = [_news("1", "公告A"), _news("2", "公告B")]
    asyncio.run(p._generate_digest("re1999", items))
    assert p._digest_of("re1999", items) is not None
    assert len(provider.calls) == 1

    # 同一批条目：命中缓存（_ensure_digest 直接返回，不再调 LLM）
    out = asyncio.run(p._ensure_digest("re1999", items, 5))
    assert out is not None
    assert len(provider.calls) == 1

    # 公告列表变了：缓存作废，重新生成
    items.append(_news("3", "公告C"))
    assert p._digest_of("re1999", items) is None
    asyncio.run(p._generate_digest("re1999", items))
    assert len(provider.calls) == 2


def test_inflight_task_shared_not_duplicated(tmp_path):
    """查询/推送/轮询三方共用一个在途任务，绝不双调 LLM。"""
    provider = FakeProvider(text="- 慢慢写", delay=0.05)
    p = _plugin(tmp_path, provider=provider)
    items = [_news("1", "公告A")]

    async def go():
        t1 = asyncio.ensure_future(p._ensure_digest("re1999", items, 5))
        await asyncio.sleep(0.001)  # 让第一个 spawn 先建任务
        t2 = asyncio.ensure_future(p._ensure_digest("re1999", items, 5))
        r1, r2 = await asyncio.gather(t1, t2)
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1 == r2 == "- 慢慢写"
    assert len(provider.calls) == 1


def test_ensure_digest_timeout_keeps_generation_running(tmp_path):
    """前台等不到就先走兜底，但生成任务不被取消——稍后再问就有缓存。"""
    provider = FakeProvider(text="- 最终要点", delay=0.1)
    p = _plugin(tmp_path, provider=provider)
    items = [_news("1", "公告A")]

    async def go():
        first = await p._ensure_digest("re1999", items, 0.01)  # 超时
        await asyncio.sleep(0.2)  # 让后台任务跑完
        return first, p._digest_of("re1999", items)

    first, cached = asyncio.run(go())
    assert first is None
    assert cached == "- 最终要点"


def test_digest_disabled_by_config(tmp_path):
    provider = FakeProvider()
    p = _plugin(tmp_path, provider=provider, config={"ai_summary_enabled": False})
    out = asyncio.run(p._ensure_digest("re1999", [_news("1", "公告A")], 1))
    assert out is None
    assert provider.calls == []


def test_max_items_clamped(tmp_path):
    p = _plugin(tmp_path, config={"ai_summary_max_items": 999})
    assert p._digest_max_items() == 20
    p.config["ai_summary_max_items"] = 0
    assert p._digest_max_items() == 3


# ==================== /游戏 视图 ====================


class FakeEvent:
    def __init__(self, text="", umo="aiocqhttp:FriendMessage:10000"):
        self.message_str = text
        self.unified_msg_origin = umo
        self.result = None

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        self.result = text
        return text


def _run_cmd(p, event):
    async def go():
        async for _ in p.cmd_games(event):
            pass
        return event.result

    return asyncio.run(go())


def test_default_view_uses_cached_digest(tmp_path):
    provider = FakeProvider()
    p = _plugin(tmp_path, provider=provider)
    _prewarm(p)  # 只预热了 re1999
    out = _run_cmd(p, FakeEvent("游戏 1999"))
    assert "资讯要点" in out
    assert "- 已缓存要点" in out
    assert "最新资讯" not in out  # 命中摘要时不该再附原始列表
    assert len(provider.calls) == 0  # 前台零 LLM 调用


def test_default_view_falls_back_to_list_with_hint(tmp_path):
    p = _plugin(tmp_path, provider=None)
    out = _run_cmd(p, FakeEvent("游戏"))
    assert "最新资讯" in out
    assert "AI 摘要" in out  # 告知为什么现在是列表
    assert "https://" not in out


def test_list_route_shows_raw_without_hint(tmp_path):
    p = _plugin(tmp_path, provider=None)
    out = _run_cmd(p, FakeEvent("游戏 列表"))
    assert "最新资讯" in out
    assert "AI 摘要" not in out  # 显式要列表就不要再说摘要的事


def test_list_route_is_always_raw_even_with_cache(tmp_path):
    """列表是逃生口：缓存里有摘要也照给原始列表，别替用户做主。"""
    p = _plugin(tmp_path, provider=None)
    _prewarm(p)
    out = _run_cmd(p, FakeEvent("游戏 列表"))
    assert "最新资讯" in out
    assert "资讯要点" not in out
    assert "AI 摘要" not in out  # 显式要列表就不要再推销摘要


def test_summary_route_alias(tmp_path):
    p = _plugin(tmp_path, provider=None)
    _prewarm(p)
    assert "资讯要点" in _run_cmd(p, FakeEvent("游戏 总结 1999"))
    assert "资讯要点" in _run_cmd(p, FakeEvent("游戏 摘要"))


def test_view_fetches_from_sources_on_cache_miss(tmp_path):
    """缓存没有 → 生成 → 后续查询命中。前台指令路径端到端。"""
    provider = FakeProvider(text="- 现生成的要点")
    p = _plugin(tmp_path, provider=provider)
    out = _run_cmd(p, FakeEvent("游戏 1999"))
    # 首次查询：20s 内等到了生成结果
    assert "- 现生成的要点" in out
    assert len(provider.calls) == 1
    _run_cmd(p, FakeEvent("游戏 1999"))
    assert len(provider.calls) == 1  # 第二次纯缓存


# ==================== 推送 ====================


def test_push_news_uses_digest(tmp_path):
    provider = FakeProvider(text="- 推送要点")
    ctx = FakeContext(provider=provider)
    p = _plugin(tmp_path, items=[_news("1", "新公告")], provider=provider, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    items = asyncio.run(p._latest("re1999", 15))
    asyncio.run(p._push_news("re1999", items[:1], items))
    assert len(ctx.sent) == 1
    assert "资讯要点" in ctx.sent[0][1]
    assert "- 推送要点" in ctx.sent[0][1]
    assert p.store.seen_ids("news:re1999") == {"1"}


def test_push_news_falls_back_without_digest(tmp_path):
    ctx = FakeContext()
    p = _plugin(tmp_path, items=[_news("1", "新公告")], provider=None, ctx=ctx)
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    items = asyncio.run(p._latest("re1999", 15))
    asyncio.run(p._push_news("re1999", items[:1], items))
    assert "有新公告" in ctx.sent[0][1]
    assert "新公告" in ctx.sent[0][1]


# ==================== 轮询 ====================


def test_poll_absorbs_codes_without_subscribers(tmp_path):
    """无订阅者也要扫码入库——/游戏 兑换 的公告码源全靠轮询，与推送无关。"""
    item = _news("1", "前瞻", content="<p>兑换码：NOSUB1234 有效期至2030年1月1日</p>")
    p = _plugin(tmp_path, items=[item], provider=None, config={"push_enabled": False})
    asyncio.run(p._poll_news())
    assert [c["code"] for c in p.store.codes("re1999")] == ["NOSUB1234"]


def test_poll_spawns_digest_and_push_falls_back(tmp_path):
    """轮询触发生成（后台）；同轮推送等不到就走兜底，下轮才有摘要。"""
    provider = FakeProvider(text="- 轮询要点", delay=0.05)
    ctx = FakeContext(provider=provider)
    p = _plugin(
        tmp_path,
        items=[_news("1", "新公告"), _news("2", "旧公告", ts=time.time() - 9999)],
        provider=provider,
        ctx=ctx,
    )
    p.store.set_subscription(PRIVATE_UMO, ["re1999"], True)
    p.store.mark_seen("news:re1999", ["2"])
    asyncio.run(p._poll_news())
    # 推送正文：等了 45s 上限内生成完成（0.05s 假延迟），拿到摘要
    assert "资讯要点" in ctx.sent[0][1]
    assert "新公告" not in ctx.sent[0][1]  # 摘要模式下不附原始列表
    # setup 时已记 '2' 已读，推送成功补记 '1'
    assert p.store.seen_ids("news:re1999") == {"1", "2"}


def test_view_and_poll_share_one_llm_call_despite_different_limits(tmp_path):
    """回归：轮询取 15 条、视图取 max_push 条，cache_key 必须仍能对上。

    真跑渲染抓到的 bug：两边公告窗口不同 → key 永远 miss → LLM 被调双倍。
    FakeSource 忽略 limit 测不出来，这里用一个真按 limit 截断的源。
    """

    class LimitSource:
        def __init__(self, items):
            self.items = items
            self.calls = 0

        async def latest(self, limit=10):
            self.calls += 1
            return list(self.items[:limit])

        async def by_category(self, category, limit=20):
            return list(self.items[:limit])

        async def close(self):
            pass

    items = [
        _news(str(i), f"公告{i}", ts=time.time() - i * 3600) for i in range(15)
    ]
    src = LimitSource(items)
    provider = FakeProvider(text="- 唯一一次要点")
    p = _plugin(tmp_path, provider=provider, sources={"re1999": src, "arknights": src})

    async def go():
        await p._poll_news()  # 轮询取 15 条，spawn 生成任务
        await asyncio.gather(*p._digest_tasks.values())  # 等后台任务写完缓存
        before = len(provider.calls)
        ev = FakeEvent("游戏")
        async for _ in p.cmd_games(ev):  # 视图只取 5 条
            pass
        return before, ev.result

    before, out = asyncio.run(go())
    assert "资讯要点" in out
    assert before == 2  # 两游戏各生成一次
    assert len(provider.calls) == 2  # 视图没加重调


# ==================== 生命周期 ====================


class FakeScheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, fn, trigger, **kw):
        self.jobs.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.started = False


def test_initialize_registers_jobs_and_warms_up_even_without_push(tmp_path, monkeypatch):
    """修 v1.0.0 遗留 bug：关推送也要起轮询（摘要预热 + 扫码入库靠它）。"""
    from astrbot_plugin_suite.modules.games import webui as W

    sched = FakeScheduler()
    monkeypatch.setattr(M, "AsyncIOScheduler", lambda: sched)
    monkeypatch.setattr(M, "get_astrbot_data_path", lambda: str(tmp_path))

    class WebCtx(FakeContext):
        def register_web_api(self, *a, **k):
            pass

    src = FakeSource([_news("1", "公告A")])
    p = make_games_plugin(
        config={"push_enabled": False},
        store=Store(tmp_path / "s.json"),
        sources={"re1999": src, "arknights": src},
        context=WebCtx(provider=FakeProvider(text="- 预热要点")),
    )
    monkeypatch.setattr(M.GamesCore, "_build_sources", lambda self: None)
    monkeypatch.setattr(W, "register", lambda plugin: None)

    async def go():
        await p.initialize()
        await asyncio.sleep(0.05)  # 给开机预热任务跑完的机会
        warmed = dict(p._digest_cache)  # terminate 会清缓存，先留快照
        await p.terminate()
        return warmed

    warmed = asyncio.run(go())
    assert sorted(sched.jobs) == ["_poll_dynamics", "_poll_news"]
    # 预热完成：摘要已入缓存
    assert "re1999" in warmed
    # terminate 清空在途任务与缓存
    assert p._digest_tasks == {}
    assert p._digest_cache == {}


def test_terminate_cancels_pending_digest_task(tmp_path):
    provider = FakeProvider(text="- 慢", delay=5)
    p = _plugin(tmp_path, provider=provider)
    items = [_news("1", "公告A")]

    async def go():
        p._spawn_digest("re1999", items)
        await asyncio.sleep(0.01)  # 任务开跑
        await p.terminate()

    asyncio.run(go())
    assert p._digest_tasks == {}
    assert "re1999" not in p._digest_cache
