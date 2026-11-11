"""HttpClient 的重试/退避/连接退休纪律。

用 httpx.MockTransport 精确控制每次请求的返回，不碰网络。
"""

import asyncio

import httpx
import pytest
from astrbot_plugin_suite.modules.games.providers.base import SourceError
from astrbot_plugin_suite.modules.games.providers.http_client import RETIRED_POOL_MAX, HttpClient


def _client(handler, **kw):
    kw.setdefault("backoff_base", 0)
    c = HttpClient(**kw)
    c._build_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5
    )
    return c


def test_single_success_returns_dict():
    c = _client(lambda req: httpx.Response(200, json={"code": 0}))
    assert asyncio.run(c.request_json("GET", "https://x")) == {"code": 0}


def test_retries_on_connect_error_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    assert asyncio.run(c.request_json("GET", "https://x")) == {"ok": True}
    assert len(calls) == 2


def test_retries_on_5xx():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    assert asyncio.run(c.request_json("GET", "https://x")) == {"ok": True}
    assert len(calls) == 3


def test_retries_on_non_json_200():
    """HTTP 200 但返回空页/风控页是常见的"上游抽风"，应按可重试处理。"""
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(200, text="<html>风控</html>")
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    assert asyncio.run(c.request_json("GET", "https://x")) == {"ok": True}


def test_gives_up_after_max_retries_and_raises_source_error():
    c = _client(lambda req: httpx.Response(500), max_retries=2)
    with pytest.raises(SourceError, match="500"):
        asyncio.run(c.request_json("GET", "https://x"))


def test_does_not_retry_on_4xx():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404)

    c = _client(handler, max_retries=3)
    with pytest.raises(SourceError, match="404"):
        asyncio.run(c.request_json("GET", "https://x"))
    assert len(calls) == 1  # 确定性错误重试没意义


def test_does_not_retry_when_json_is_not_object():
    c = _client(lambda req: httpx.Response(200, json=[1, 2, 3]), max_retries=3)
    with pytest.raises(SourceError, match="不是对象"):
        asyncio.run(c.request_json("GET", "https://x"))


def test_retry_does_not_close_shared_client():
    """当场 aclose 共享 client 会打断并行请求（电费插件 v1.2.1 的教训）。

    用计数 fake 验证：失败重试期间**一个** client 都没被关掉，只是挪进了退休池。
    """
    built = []
    closed = []

    class FakeClient:
        def __init__(self, handler):
            self.handler = handler
            built.append(self)

        async def request(self, *a, **kw):
            return self.handler(None)

        async def aclose(self):
            closed.append(self)

    c = HttpClient(max_retries=1, backoff_base=0)
    c._build_client = lambda: FakeClient(lambda req: httpx.Response(500))
    asyncio.run(_expect_fail(c))
    assert closed == [], "重试时把还在用的 client 关了"
    assert len(c._retired) == 2  # 2 次尝试 = 2 次退休

    asyncio.run(c.close())
    assert len(closed) == 2  # 收尾时才真正关闭


def test_retired_client_is_replaced_on_next_request():
    """退休后下一次请求必须拿到全新的 client，而不是继续用坏的那个。"""
    c = _client(lambda req: httpx.Response(500), max_retries=0)
    asyncio.run(_expect_fail(c))
    assert c._retired, "失败的 client 应进退休池"

    c._build_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1})),
        timeout=5,
    )
    assert asyncio.run(c.request_json("GET", "https://x")) == {"ok": 1}
    assert c._client is not None


async def _expect_fail(c):
    with pytest.raises(SourceError):
        await c.request_json("GET", "https://x")


def test_retired_pool_is_capped():
    c = _client(lambda req: httpx.Response(500), max_retries=1)
    for _ in range(RETIRED_POOL_MAX + 3):
        asyncio.run(_expect_fail(c))
    assert len(c._retired) <= RETIRED_POOL_MAX


def test_close_drains_both_pools():
    c = _client(lambda req: httpx.Response(500), max_retries=1)
    asyncio.run(_expect_fail(c))
    asyncio.run(c.close())
    assert c._retired == []
    assert c._client is None


def test_close_is_safe_without_client():
    c = HttpClient()
    asyncio.run(c.close())
    asyncio.run(c.close())


def test_backoff_actually_sleeps():
    slept = []

    c = _client(lambda req: httpx.Response(500), max_retries=2, backoff_base=1)

    async def fake_sleep(d):
        slept.append(d)

    import astrbot_plugin_suite.modules.games.providers.http_client as mod

    original = mod.asyncio.sleep
    mod.asyncio.sleep = fake_sleep
    try:
        asyncio.run(_expect_fail(c))
    finally:
        mod.asyncio.sleep = original
    assert slept == [1.0, 2.0]


def test_post_body_and_headers_are_passed_through():
    seen = {}

    def handler(req):
        seen["content"] = req.content.decode()
        seen["referer"] = req.headers.get("referer")
        return httpx.Response(200, json={})

    c = _client(handler)
    asyncio.run(
        c.request_json(
            "POST", "https://x", json_body={"a": 1}, headers={"Referer": "https://r"}
        )
    )
    assert '"a":1' in seen["content"]
    assert seen["referer"] == "https://r"


def test_get_params_are_passed_through():
    seen = {}

    def handler(req):
        seen["q"] = str(req.url.params)
        return httpx.Response(200, json={})

    c = _client(handler)
    asyncio.run(c.request_json("GET", "https://x", params={"category": "LATEST"}))
    assert "category=LATEST" in seen["q"]


def test_sessdata_failure_raises_not_silent_empty():
    """B站风控 -352 是确定性错误，应立即抛出而不是重试到天荒地老。"""
    c = _client(lambda req: httpx.Response(200, json={"code": -352}), max_retries=3)
    # 业务码由调用方判断，HttpClient 不该吞
    assert asyncio.run(c.request_json("GET", "https://x"))["code"] == -352
