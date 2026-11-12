"""汉江师范学院缴费系统（pay2.hjnu.edu.cn）数据源。

接口为企业微信内嵌 H5 的 JSON API：
- POST 表单（键值对，层级选项序列化为 JSON 字符串）
- 需要携带 JSESSIONID 会话 Cookie 与带 wxwork 标识的 UA
- 未认证时返回 {"retcode": "91001", "errmsg": "会话已超时…"}
"""

import asyncio
import json
import logging
import re

import httpx

from .base import BalanceResult, ElecProvider, QueryError

SESSION_EXPIRED_CODE = "91001"

# 91001 复验延迟阶梯（秒）。学校「发癫」时会把**还活着的会话**临时判成 91001
# （实录：会话即生即死、几分钟后自愈），单次 91001 就宣判凭证失效会害用户
# 白折腾重新提取凭证——这里隔一小段把同一请求原样重发，复验通过就当抖动处理。
#
# 为什么要阶梯而不是固定一次：
#   - 固定 2.5s 一次只能挡「秒级」抖动。学校实录的抖动是「分钟级」
#     （会话即生即死、几分钟后自愈），2.5s 复验很可能也撞在抖动窗口里，
#     于是「复验也 91001」被当成真失效 → 白折腾一轮重新提取凭证。
#   - 阶梯 2.5s → 8s 覆盖到 ~10s，正好压住「几分钟自愈」的绝大多数情况；
#     再长就没必要了（真失效会在 main 层被连续确认计数 + 时间窗拦下）。
#
# 每次复验都换一条**全新连接**（不复用共享 client）：学校多后端节点时，
# 同一节点可能持续返回 91001，换连接才有机会落到健康节点。
EXPIRED_RECHECK_DELAYS: tuple[float, ...] = (2.5, 8.0)

# 兼容旧调用点/旧测试：首个复验延迟。
EXPIRED_RECHECK_DELAY = EXPIRED_RECHECK_DELAYS[0]

logger = logging.getLogger("astrbot.plugin.dorm_electric")


class SessionExpiredError(QueryError):
    """会话凭证失效（retcode 91001）。"""


async def _safe_aclose(client: httpx.AsyncClient) -> None:
    """关连接尽力而为：terminate 竞态下 aclose 可能抛，别让它盖掉真实异常。"""
    try:
        if not client.is_closed:
            await client.aclose()
    except Exception as e:  # 收尾路径不该再抛，盖掉真实异常就难查了
        logger.debug("关闭 httpx client 失败：%r", e)


# 例：A-8-17房间当前剩余电量94.66度
BALANCE_RE = re.compile(
    r"(?:剩余电量\s*([0-9]+(?:\.[0-9]+)?)\s*度|余额\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*元)"
)
ROOM_RE = re.compile(r"^\s*(\S+?)房间")


def parse_balance(raw_msg: str) -> tuple[float, str, str] | None:
    """从 errmsg 文本中解析 (余额, 房间名, 单位)。"""
    m = BALANCE_RE.search(raw_msg or "")
    if not m:
        return None
    room = ""
    mr = ROOM_RE.search(raw_msg or "")
    if mr:
        room = mr.group(1)
    if m.group(1) is not None:
        return float(m.group(1)), room, "度"
    return float(m.group(2)), room, "元"


class HjnuProvider(ElecProvider):
    name = "hjnu"
    # 退休连接池上限：重试换连接时不能当场 aclose（双费种并行查询、房间扫描
    # 可能还握着旧 client，中途关闭会让它们撞 RuntimeError），先攒着，
    # 攒够了再关最老的，最后 close() 兜底全关。
    retired_limit = 4

    def __init__(
        self,
        base_url: str,
        query_path: str,
        cookie: str,
        user_agent: str,
        referer: str,
        timeout: int,
        proxy: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.query_path = query_path
        self.cookie = (cookie or "").strip()
        self.user_agent = user_agent
        self.referer = referer
        self.timeout = timeout
        self.proxy = (proxy or "").strip() or None
        # 测试缝：注入 httpx.MockTransport 模拟学校服务器的 5xx/非 JSON/91001
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._retired: list[httpx.AsyncClient] = []

    def update_cookie(self, cookie: str) -> None:
        self.cookie = (cookie or "").strip()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.referer,
                "Origin": self.base_url,
            }
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                proxy=self.proxy,
                headers=headers,
                transport=self._transport,
            )
        return self._client

    async def _retire_client(self) -> None:
        """把异常连接移出共享池，等在途请求跑完再关（见 retired_limit 注释）。"""
        client, self._client = self._client, None
        if client is None or client.is_closed:
            return
        self._retired.append(client)
        while len(self._retired) > self.retired_limit:
            await _safe_aclose(self._retired.pop(0))

    async def close(self) -> None:
        clients = [c for c in [self._client, *self._retired] if c is not None]
        self._client = None
        self._retired.clear()
        for client in clients:
            await _safe_aclose(client)

    def _headers(self) -> dict:
        if not self.cookie:
            raise QueryError("未配置会话凭证。请先发送 /电费 凭证 更新 JSESSIONID。")
        return {"Cookie": self.cookie}

    @staticmethod
    def _j(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def _post_once(self, path: str, fields: dict) -> tuple[int, str, str]:
        """单次请求，返回 (status, text, err)；err 非空表示网络层失败。"""
        client = self._get_client()
        return await self._post_with_client(client, path, fields)

    async def _post_once_fresh(self, path: str, fields: dict) -> tuple[int, str, str]:
        """用一条全新的一次性连接发请求（91001 复验专用）。

        学校是多后端节点：同一节点可能持续返回 91001（会话状态被它判死），
        复验若复用同一连接，很可能拿到同样的 91001 → 白复验。换一条
        新连接 = 重新负载均衡，才有机会落到健康节点。

        用完即关（不进退休池）：复验连接是一次性的，不该跟业务连接抢名额。
        """
        client = httpx.AsyncClient(
            timeout=self.timeout,
            proxy=self.proxy,
            headers=self._headers(),
            transport=self._transport,
        )
        try:
            return await self._post_with_client(client, path, fields)
        finally:
            await _safe_aclose(client)

    async def _post_with_client(
        self, client: httpx.AsyncClient, path: str, fields: dict
    ) -> tuple[int, str, str]:
        try:
            resp = await client.post(
                self.base_url + path,
                data=fields,
                headers=self._headers(),
            )
        except (httpx.HTTPError, RuntimeError) as e:
            # RuntimeError：client 在请求发出前后被并发关闭（如 terminate 竞态）。
            # 它不是 httpx.HTTPError 子类，不接住会穿透到调用方的 gather 之外。
            hint = (
                "请检查 http_proxy 代理节点是否可用"
                if self.proxy
                else "请检查宿主机是否能直连学校接口，或配置 http_proxy 代理节点"
            )
            return 0, "", f"网络请求失败：{e!r}；{hint}"
        try:
            text = resp.text
        except Exception:
            text = ""
        return resp.status_code, text, ""

    async def _post(self, path: str, fields: dict) -> dict:
        """发请求、解析 JSON；91001 会隔一小段换连接复验（阶梯）。

        复验：学校发癫时会把活着的会话临时判成 91001，原样重发若通过，
        说明只是抖动——直接当正常结果返回，不惊动用户去换凭证。
        复验仍 91001 才交给调用方按「会话失效」处理。

        两处关键改动（防「学校波动造成凭证假失效」）：
        1. 阶梯复验（EXPIRED_RECHECK_DELAYS）：单次 2.5s 复验只挡得住秒级
           抖动；学校实录的抖动是分钟级（会话即生即死、几分钟后自愈），
           2.5s 复验往往也撞在抖动窗口里 → 复验也 91001 → 被当成真失效，
           害用户白折腾一轮重新提取凭证。阶梯 2.5s→8s 压住绝大多数抖动。
        2. 每次复验换一条全新连接（不复用共享 client）：学校是多后端节点，
           同一节点可能持续返回 91001，换连接才有机会落到健康节点。
        """
        data = await self._post_retry(path, fields)
        for delay in EXPIRED_RECHECK_DELAYS:
            if not self._is_expired(data):
                break
            await asyncio.sleep(delay)
            logger.info(
                "电费接口返回 91001，%.1fs 后换新连接复验（%s）", delay, path
            )
            data = await self._post_retry(path, fields, fresh=True)
        return data

    @staticmethod
    def _is_expired(data: dict) -> bool:
        return (
            isinstance(data, dict)
            and str(data.get("retcode", "")) == SESSION_EXPIRED_CODE
        )

    async def _post_retry(self, path: str, fields: dict, fresh: bool = False) -> dict:
        # 5xx（如学校服务器偶发 502）自动换新连接重试 2 次
        # fresh=True：91001 复验专用——每轮都用全新连接，不复用共享 client，
        # 也不进退休池（复验连接用完即关，不该跟业务连接抢名额）。
        last_err = ""
        for attempt in range(3):
            if fresh:
                status, text, net_err = await self._post_once_fresh(path, fields)
            else:
                status, text, net_err = await self._post_once(path, fields)
            if net_err:
                last_err = net_err
                logger.error("电费接口网络错误(%s)：%s", path, net_err)
            elif status == 200:
                try:
                    data = json.loads(text)
                except ValueError as e:
                    # 学校偶发「chunked 空页」抖动：200 但内容非 JSON，按瞬态故障重试
                    snippet = (text or "").strip()[:200]
                    last_err = "接口返回的不是 JSON（可能凭证已被服务端作废）"
                    logger.error("电费接口返回非 JSON(%s)：%s", path, snippet)
                    if attempt >= 2:
                        raise QueryError(last_err) from e
                else:
                    if not isinstance(data, dict):
                        raise QueryError("接口返回了意外结构")
                    return data
            else:
                snippet = (text or "").strip()[:200]
                last_err = f"接口返回 HTTP {status}"
                logger.error("电费接口 HTTP %s(%s)：%s", status, path, snippet)
                if status < 500:
                    break
            if attempt < 2:
                await asyncio.sleep(1.5)
                # 5xx/网络错误时丢弃旧连接引用，避免复用异常连接。
                # 不在此处直接 aclose：同一实例上可能有并发请求（双费种并行查询、
                # 房间扫描）仍握着旧 client；交给 _retire_client 攒够再关、
                # terminate 时 close() 兜底全关。
                if not fresh:
                    await self._retire_client()
        raise QueryError(f"{last_err}（学校服务器暂时无响应，已自动重试仍失败，请稍后再试）")

    @staticmethod
    def _check(data: dict) -> None:
        retcode = str(data.get("retcode", ""))
        if retcode == SESSION_EXPIRED_CODE:
            raise SessionExpiredError(data.get("errmsg", "会话已超时"))
        if retcode != "0":
            raise QueryError(f"接口错误 retcode={retcode}: {data.get('errmsg', '')}")

    async def fetch(self, binding: dict) -> BalanceResult:
        """ElecProvider 接口：从绑定中取参数查询余额。"""
        return await self.query_room(binding.get("params") or {})

    async def query_room(self, params: dict) -> BalanceResult:
        """params: {aid, area, building, floor, room}，值为选项 dict。"""
        fields = {"aid": params["aid"]}
        for key in ("area", "building", "floor", "room"):
            if params.get(key):
                fields[key] = self._j(params[key])
        try:
            data = await self._post(self.query_path, fields)
            self._check(data)
        except SessionExpiredError as e:
            return BalanceResult(ok=False, value=None, raw=str(e), session_expired=True)
        except QueryError as e:
            return BalanceResult(ok=False, value=None, raw=str(e))
        raw_msg = str(data.get("errmsg", ""))
        parsed = parse_balance(raw_msg)
        if parsed is None:
            return BalanceResult(ok=False, value=None, raw=raw_msg or "接口未返回余额文本")
        value, _room, unit = parsed
        return BalanceResult(ok=True, value=value, raw=raw_msg, unit=unit)

    async def list_areas(self, aid: str) -> list[dict]:
        data = await self._post("/wechat/basicQuery/queryElecArea.html", {"aid": aid})
        self._check(data)
        return list(data.get("areatab") or [])

    async def list_buildings(self, aid: str, area: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecBuilding.html",
            {"aid": aid, "area": self._j(area)},
        )
        self._check(data)
        return list(data.get("buildingtab") or [])

    async def list_floors(self, aid: str, area: dict, building: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecFloor.html",
            {"aid": aid, "area": self._j(area), "building": self._j(building)},
        )
        self._check(data)
        return list(data.get("floortab") or [])

    async def list_rooms(self, aid: str, area: dict, building: dict, floor: dict) -> list[dict]:
        data = await self._post(
            "/wechat/basicQuery/queryElecRoom.html",
            {
                "aid": aid,
                "area": self._j(area),
                "building": self._j(building),
                "floor": self._j(floor),
            },
        )
        self._check(data)
        return list(data.get("roomtab") or [])
