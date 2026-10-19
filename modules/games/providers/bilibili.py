"""B站官方动态与扫码登录客户端。

动态链路（无登录态也能读，实测 code=0 有数据）：

    1. GET /x/frontend/finger/spi            → data.b_3 即 buvid3
    2. GET /x/web-interface/nav              → data.wbi_img.img_url/sub_url
    3. 按 wbi 重排表拼 mixin_key，md5(params + mixin_key) 得 w_rid
    4. GET /x/polymer/web-dynamic/v1/feed/space?{签名前参数}&w_rid=...

扫码登录链路（拿 SESSDATA）：
    GET /x/passport-login/web/qrcode/generate → qrcode_key + url
    每 2 秒 GET /x/passport-login/web/qrcode/poll?qrcode_key=...
    0=成功 86090=已扫未确认 86038=已过期 86101=未扫

⚠️ 实测踩坑（写死在这里，别去"优化"掉）：
- **只挂 buvid3 一个 cookie 最稳**，带上主站 b_nut 等多余 cookie 反而触发 -352。
- -352 风控是间歇性的（同一配方时好时坏），必须退避重试而不是直接放弃。
- **feed 请求必须带 timezone_offset/platform/web_location**，只带 host_mid+wts
  会静默返回空列表（code=0 但 items 为空），看着像"该号没动态"，实际是参数缺了。
- 官方号 UID：1999=1197454103、方舟=161775300（均经官网外链核验）。
  方舟官方号当前动态列表为空（code=0 total=0），属数据源现实，要优雅处理。
"""

import asyncio
import hashlib
import logging
import time
import urllib.parse
from dataclasses import dataclass

from .base import SourceError
from .http_client import HttpClient

logger = logging.getLogger("astrbot.plugin.games_monitor")

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"

DYNAMIC_URL = f"{API}/x/polymer/web-dynamic/v1/feed/space"
SPI_URL = f"{API}/x/frontend/finger/spi"
NAV_URL = f"{API}/x/web-interface/nav"
QRCODE_GENERATE_URL = f"{PASSPORT}/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = f"{PASSPORT}/x/passport-login/web/qrcode/poll"

# wbi 重排表（B站公开算法，多年未变）。img_key + sub_key 按此表重排取前 32 位。
WBI_MIXIN_TABLE: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)

# 业务错误码 → 人话。-352 是风控，值得退避重试；其余是确定性错误。
RISK_BLOCKED = -352
TOO_FREQUENT = -799

# 扫码登录状态码
QR_OK = 0
QR_EXPIRED = 86038
QR_SCANNED = 86090
QR_NOT_SCANNED = 86101

QR_STATUS_TEXT: dict[int, str] = {
    QR_NOT_SCANNED: "等待扫码",
    QR_SCANNED: "已扫码，请在手机上确认",
    QR_EXPIRED: "二维码已过期，正在自动刷新",
    QR_OK: "登录成功",
}

# feed 必带的固定参数：缺任何一个都可能静默返回空列表（见模块 docstring）
FEED_BASE_PARAMS: dict[str, str] = {
    "timezone_offset": "-480",
    "platform": "web",
    "web_location": "1550101",
}

# wbi key 缓存有效期。B站会轮换 key，现取现签；缓存 1 小时兼顾命中率。
WBI_KEY_TTL = 3600.0

# code=0 但 items 为空时额外重试的次数与退避基数（秒）
EMPTY_RETRY = 2
EMPTY_RETRY_DELAY = 0.6


def mixin_key(img_url: str, sub_url: str) -> str:
    """按重排表把 (img_url, sub_url) 的文件名混成 32 位签名密钥。纯函数。"""
    raw = _key_of(img_url) + _key_of(sub_url)
    return "".join(raw[i] for i in WBI_MIXIN_TABLE if i < len(raw))[:32]


def _key_of(url: str) -> str:
    return str(url or "").rsplit("/", 1)[-1].split(".")[0]


def sign_params(params: dict, key: str, wts: int | None = None) -> tuple[str, str]:
    """给参数加 wts 并算 w_rid，返回 (查询串, w_rid)。纯函数，便于单测。"""
    full = dict(params)
    full["wts"] = int(wts if wts is not None else time.time())
    query = urllib.parse.urlencode(sorted(full.items()))
    w_rid = hashlib.md5((query + key).encode()).hexdigest()
    return query, w_rid


@dataclass(frozen=True)
class DynamicItem:
    """一条官方动态（文本/图文/视频/转发统一成一条展示记录）。"""

    game: str
    item_id: str
    title: str
    text: str
    published: float
    url: str
    kind: str


def parse_dynamics(payload: dict, game: str, limit: int = 0) -> list[DynamicItem]:
    """把 feed 响应解析成 DynamicItem 列表（纯函数，便于单测）。

    正文位置实测有三处，按优先级取第一个非空的：
      1. module_dynamic.major.opus.summary.text  （图文/长文）
      2. module_dynamic.desc.text                （纯文本/转发）
      3. module_dynamic.major.archive.title/desc  （视频投稿）
    """
    if not isinstance(payload, dict):
        raise SourceError("B站动态响应结构异常")
    code = payload.get("code")
    if code == RISK_BLOCKED:
        raise SourceError("B站风控拦截（-352）：请稍后重试或配置 SESSDATA")
    if code != 0:
        raise SourceError(f"B站动态接口返回 code={code} message={payload.get('message')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceError("B站动态响应缺少 data 字段")
    out: list[DynamicItem] = []
    for raw in data.get("items") or []:
        item = _parse_one(raw, game)
        if item is not None:
            out.append(item)
            if limit and len(out) >= limit:
                break
    return out


def _parse_one(raw: dict, game: str) -> DynamicItem | None:
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id_str") or "").strip()
    if not item_id:
        return None
    modules = raw.get("modules") or {}
    dyn = modules.get("module_dynamic") or {}
    author = modules.get("module_author") or {}
    major = dyn.get("major") or {}

    text = ""
    title = ""
    opus = major.get("opus") or {}
    archive = major.get("archive") or {}

    summary_text = str(((opus.get("summary") or {}).get("text")) or "").strip()
    desc_text = str(((dyn.get("desc") or {}).get("text")) or "").strip()
    arc_title = str(archive.get("title") or "").strip()
    arc_desc = str(archive.get("desc") or "").strip()

    if summary_text:
        text = summary_text
    if desc_text and len(desc_text) > len(text):
        text = desc_text
    if archive:
        title = arc_title or title
        if len(arc_desc) > len(text):
            text = arc_desc

    kind = _kind_of(raw, major, bool(opus), bool(archive))
    if not title:
        # 没标题就用正文首行；正文也空（风控把内容剥了）时别渲染成一个空壳，
        # 至少把"这是一条图片动态"说清楚。
        title = text.splitlines()[0][:40] if text else ""

    try:
        published = float(author.get("pub_ts") or 0)
    except (TypeError, ValueError):
        published = 0.0

    return DynamicItem(
        game=game,
        item_id=item_id,
        title=title,
        text=text,
        published=published,
        url=f"https://t.bilibili.com/{item_id}",
        kind=kind,
    )


# major.type → 展示名。风控剥内容时 major.opus 会是空 dict，只能靠这个字段判断形态。
MAJOR_KIND: dict[str, str] = {
    "MAJOR_TYPE_ARCHIVE": "视频",
    "MAJOR_TYPE_OPUS": "图文",
    "MAJOR_TYPE_DRAW": "图文",
    "MAJOR_TYPE_ARTICLE": "文章",
    "MAJOR_TYPE_LIVE_RCMD": "直播",
}


def _kind_of(raw: dict, major: dict, has_opus: bool, has_archive: bool) -> str:
    if str(raw.get("type") or "").endswith("FORWARD"):
        return "转发"
    if has_archive:
        return "视频"
    mapped = MAJOR_KIND.get(str(major.get("type") or ""))
    if mapped:
        return mapped
    if has_opus:
        return "图文"
    return "动态"


class BilibiliDynamics:
    """官方号动态拉取。持有 buvid3 与 wbi key，按 TTL 刷新。"""

    def __init__(self, client: HttpClient, sessdata: str = "") -> None:
        self.client = client
        self.sessdata = sessdata or ""
        self._buvid3 = ""
        self._wbi_key = ""
        self._wbi_at = 0.0

    def _cookies(self) -> dict:
        # 只挂 buvid3：多挂主站 cookie 反而更容易吃 -352（实测）
        cookies = {}
        if self._buvid3:
            cookies["buvid3"] = self._buvid3
        if self.sessdata:
            cookies["SESSDATA"] = self.sessdata
        return cookies

    async def _ensure_identity(self) -> None:
        if not self._buvid3:
            payload = await self.client.request_json("GET", SPI_URL)
            if payload.get("code") != 0:
                raise SourceError(f"B站 spi 接口返回 code={payload.get('code')}")
            self._buvid3 = str(((payload.get("data") or {}).get("b_3")) or "")
            if not self._buvid3:
                raise SourceError("B站 spi 未返回 buvid3")

        if self._wbi_key and (time.time() - self._wbi_at) < WBI_KEY_TTL:
            return
        payload = await self.client.request_json(
            "GET", NAV_URL, cookies=self._cookies(), headers={"Referer": "https://www.bilibili.com/"}
        )
        # 未登录时 nav 返回 code=-101，但 data.wbi_img 照常给——照样能用
        wbi = ((payload.get("data") or {}).get("wbi_img") or {})
        img, sub = str(wbi.get("img_url") or ""), str(wbi.get("sub_url") or "")
        if not img or not sub:
            raise SourceError("B站 nav 未返回 wbi 签名图（可能被风控）")
        self._wbi_key = mixin_key(img, sub)
        self._wbi_at = time.time()

    async def space_feed(self, uid: str, game: str, limit: int = 10) -> list[DynamicItem]:
        """拉某个官方号的最新动态。

        ⚠️ code=0 但 items 为空是 B站风控的典型表现——同一请求时好时坏，
        实测连续两次都拿到过空列表。所以这里额外重试 EMPTY_RETRY 次再认输。
        官方号确实一条动态都没发过时，最终仍是空列表（合法数据，上层要优雅处理）。
        """
        await self._ensure_identity()
        params = {"host_mid": str(uid), **FEED_BASE_PARAMS}
        query, w_rid = sign_params(params, self._wbi_key)
        url = f"{DYNAMIC_URL}?{query}&w_rid={w_rid}"
        cookies = self._cookies()
        headers = {"Referer": "https://www.bilibili.com/"}
        for attempt in range(EMPTY_RETRY + 1):
            payload = await self.client.request_json(
                "GET", url, cookies=cookies, headers=headers
            )
            items = parse_dynamics(payload, game, limit=limit)
            if items or attempt == EMPTY_RETRY:
                if not items:
                    logger.info("[B站] %s 动态列表为空（已重试 %d 次）", game, EMPTY_RETRY)
                return items
            await asyncio.sleep(EMPTY_RETRY_DELAY * (attempt + 1))
        return []

    async def close(self) -> None:
        await self.client.close()


class BilibiliLogin:
    """扫码登录：生成二维码 + 轮询确认，把 SESSDATA 交回调用方。"""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    async def generate(self) -> tuple[str, str]:
        """返回 (qrcode_key, 二维码内容 url)。"""
        payload = await self.client.request_json("GET", QRCODE_GENERATE_URL)
        if payload.get("code") != QR_OK:
            raise SourceError(f"生成二维码失败：code={payload.get('code')}")
        data = payload.get("data") or {}
        key, url = str(data.get("qrcode_key") or ""), str(data.get("url") or "")
        if not key or not url:
            raise SourceError("生成二维码失败：响应缺少 qrcode_key/url")
        return key, url

    async def poll(self, qrcode_key: str) -> tuple[int, str]:
        """轮询一次，返回 (状态码, sessdata 或空串)。

        状态码见 QR_* 常量；86101/86090/86038 都不是错误，交给前端轮询。
        """
        payload = await self.client.request_json(
            "GET", QRCODE_POLL_URL, params={"qrcode_key": qrcode_key}
        )
        code = payload.get("code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = QR_NOT_SCANNED
        if code != QR_OK:
            return code, ""
        return QR_OK, _extract_sessdata(payload)


def _extract_sessdata(payload: dict) -> str:
    """从 poll 成功响应里抠出 SESSDATA。

    data.url 的查询参数里带一份，data.headers 里的 Set-Cookie 带一份；
    两处都没有时回空串（上层按"没拿到"处理，不写配置）。
    """
    data = payload.get("data") or {}
    url = str(data.get("url") or "")
    query = urllib.parse.urlparse(url).query
    found = urllib.parse.parse_qs(query).get("SESSDATA")
    if found and found[0]:
        return found[0]
    # headers 是 {名字: 值} 的字典，要取 value 而不是遍历 key
    headers = data.get("headers") or {}
    for cookie in headers.values() if hasattr(headers, "values") else headers:
        for part in str(cookie).split(";"):
            part = part.strip()
            if part.lower().startswith("sessdata="):
                return part.split("=", 1)[1]
    return ""


class BilibiliClient:
    """动态 + 扫码登录的组合门面：共用一个 HttpClient，terminate 时关一次就够。"""

    def __init__(self, *, timeout: float = 15.0, user_agent: str = "", proxy: str = "",
                 sessdata: str = "", max_retries: int = 2) -> None:
        self.http = HttpClient(
            timeout=timeout, user_agent=user_agent, proxy=proxy, max_retries=max_retries
        )
        self.dynamics = BilibiliDynamics(self.http, sessdata=sessdata)
        self.login = BilibiliLogin(self.http)

    def set_sessdata(self, sessdata: str) -> None:
        """扫码成功后热更新登录态。设了就把身份 cookie 也带上（风控更松）。"""
        self.dynamics.sessdata = str(sessdata or "")

    async def close(self) -> None:
        await self.http.close()
