"""明日方舟官网资讯源。

    GET https://ak.hypergryph.com/api/news?category={CAT}&page={n}
    CAT ∈ LATEST | ANNOUNCEMENT | ACTIVITY | NEWS

响应 {"code":0,"data":{"list":[{cid,tab,title,displayTime,brief,...}],"total":N,"end":bool}}，
tab: "0"=公告 "1"=活动 "2"=新闻；displayTime 是 Unix 秒。

这是官网 SPA 自己的后端（从 web.hycdn.cn 的 page-*.js 里挖出来的），
无任何风控，直连即可。
"""

from .base import NewsItem, SourceError
from .http_client import HttpClient

BASE_URL = "https://ak.hypergryph.com"
API_PATH = "/api/news"
DETAIL_TMPL = f"{BASE_URL}/news/{{}}"

# 内部分类 → 官网 category 参数
CATEGORY_PARAM: dict[str, str] = {
    "公告": "ANNOUNCEMENT",
    "活动": "ACTIVITY",
    "新闻": "NEWS",
    "最新": "LATEST",
}

# 官网 tab 值 → 展示名
TAB_NAME: dict[str, str] = {"0": "公告", "1": "活动", "2": "新闻"}


def parse_items(payload: dict, limit: int = 0) -> list[NewsItem]:
    """把官网返回的 JSON 解析成 NewsItem 列表（纯函数，便于单测）。"""
    if not isinstance(payload, dict):
        raise SourceError("方舟接口返回结构异常")
    if payload.get("code") != 0:
        raise SourceError(f"方舟接口返回 code={payload.get('code')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceError("方舟接口缺少 data 字段")
    items: list[NewsItem] = []
    for raw in data.get("list") or []:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cid") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not cid or not title:
            continue
        brief = str(raw.get("brief") or "").strip()
        try:
            published = float(raw.get("displayTime") or 0)
        except (TypeError, ValueError):
            published = 0.0
        items.append(
            NewsItem(
                game="arknights",
                item_id=cid,
                title=title,
                category=TAB_NAME.get(str(raw.get("tab")), "资讯"),
                published=published,
                url=DETAIL_TMPL.format(cid),
                summary=brief,
                # 方舟列表接口不给正文，brief 就是官网摘要在列表页展示的那段
                content=brief,
            )
        )
        if limit and len(items) >= limit:
            break
    return items


class ArknightsSource:
    key = "arknights"
    display_name = "明日方舟"

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    async def latest(self, limit: int = 10) -> list[NewsItem]:
        return await self.by_category("最新", limit=limit)

    async def by_category(self, category: str, limit: int = 20) -> list[NewsItem]:
        param = CATEGORY_PARAM.get(category)
        if not param:
            raise SourceError(f"方舟没有分类「{category}」")
        payload = await self.client.request_json(
            "GET",
            f"{BASE_URL}{API_PATH}",
            params={"category": param, "page": 1},
        )
        return parse_items(payload, limit=limit)

    async def close(self) -> None:
        await self.client.close()
