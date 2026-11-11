"""共用异步 HTTP 客户端：一次 GET/POST JSON，带退避重试与连接池退休。

三个数据源都要「失败能退避、连接坏了能换新、退出时干净关掉」，抽在这里避免
三份重复。重试纪律沿用电费插件 v1.2.1 的教训：

- **重试时绝不 aclose() 共享 client**：并行的双费种查询/房间扫描还握着手上的连接，
  当场关掉会打断它们。正确做法是把这批连接挪进 _retired 池（上限 4），攒满关最老的，
  只在 close() 时统一收干。
- 退避只对「连接层失败」和 5xx 生效；4xx 与业务错误码（方舟 code≠0、1999 code=5001）
  属于确定性错误，重试没有意义，直接抛给上层判断。
"""

import asyncio
import logging

import httpx

from .base import SourceError

logger = logging.getLogger("astrbot.plugin.games_monitor")

# 连接退休池上限：攒满后关掉最老的一批，防止长跑进程泄漏 fd
RETIRED_POOL_MAX = 4


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        user_agent: str = "",
        proxy: str = "",
        max_retries: int = 2,
        backoff_base: float = 1.0,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.proxy = proxy or None
        self.max_retries = max(1, int(max_retries))
        self.backoff_base = backoff_base
        self._client: httpx.AsyncClient | None = None
        self._retired: list[httpx.AsyncClient] = []
        self._lock = asyncio.Lock()

    # ---------- client 生命周期 ----------

    def _build_client(self) -> httpx.AsyncClient:
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        kwargs: dict = {"timeout": self.timeout, "follow_redirects": True}
        if headers:
            kwargs["headers"] = headers
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def _retire_client(self) -> None:
        """把当前 client 连同它手里的连接挪进退休池，而不是当场关掉。

        并行请求（两游戏同时拉、公告正文码扫描）可能还持有这批连接；
        aclose() 会在它们的 await 间隙把它们打断。这里延迟关闭，
        攒够 RETIRED_POOL_MAX 个就关掉最老的一批。

        必须是 async 并 await 关闭：`_safe_aclose` 是协程，裸调会留下
        "never awaited" 的悬挂协程，连接一个都没关——正是本模块要防的 fd 泄漏。
        """
        if self._client is None:
            return
        self._retired.append(self._client)
        self._client = None
        while len(self._retired) > RETIRED_POOL_MAX:
            old = self._retired.pop(0)
            await _safe_aclose(old)

    async def close(self) -> None:
        """收干所有连接：退休池 + 当前 client。await 缺一个就等于没关。"""
        for client in self._retired:
            await _safe_aclose(client)
        self._retired.clear()
        if self._client is not None:
            await _safe_aclose(self._client)
            self._client = None

    # ---------- 请求 ----------

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        cookies: dict | None = None,
    ) -> dict:
        """请求并解析 JSON，失败按指数退避重试。

        抛出 SourceError（含最后一次的原因）——调用方据此决定退避多久。
        """
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._once(method, url, params, json_body, headers, cookies)
            except _RetryableError as e:
                last = e
                await self._retire_client()
            except SourceError:
                raise
            if attempt < self.max_retries:
                delay = self.backoff_base * (2**attempt)
                logger.warning(
                    "请求失败（%s %s，%s），%.1fs 后重试 %d/%d",
                    method,
                    url,
                    last,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(delay)
        raise SourceError(f"{method} {url} 失败：{last}")

    async def _once(
        self,
        method: str,
        url: str,
        params: dict | None,
        json_body: dict | None,
        headers: dict | None,
        cookies: dict | None,
    ) -> dict:
        client = self._get_client()
        try:
            resp = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                cookies=cookies,
            )
        except (httpx.HTTPError, RuntimeError) as e:
            raise _RetryableError(f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 500:
            raise _RetryableError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            # HTTP 200 但不是 JSON：常见的「上游返回空页/风控页」，按可重试处理
            raise _RetryableError(f"响应非 JSON：{resp.text[:80]}") from e
        if not isinstance(data, dict):
            raise SourceError("响应 JSON 不是对象")
        return data


class _RetryableError(Exception):
    """内部信号：这次失败值得重试。"""


async def _safe_aclose(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception as e:
        logger.debug("关闭 http client 失败：%r", e)
