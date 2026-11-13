"""重返未来：1999 官网资讯源。

    POST https://re.bluepoch.com/activity/official/websites/information/query
    body {"id":"","informationType":"","current":1,"pageSize":8,"isTop":""}
    informationType: ""=最新(公告+新闻) 2=公告 3=活动 4=新闻

⚠️ 两个实测坑（AGENTS.md 同款纪律，务必保留）：
1. **必须用 Python TLS**。curl 的 TLS 指纹会被腾讯 EdgeOne 拦，返回
   {"code":5001,"msg":"系统繁忙，请稍后再试"}；参数怎么改都没用，别去调参数。
2. **列表接口直接含公告全文 HTML**（content 字段），不需要再请求详情页。
   兑换码兜底扫描直接用 content，零额外请求。
"""

from .base import NewsItem, SourceError, strip_html
from .http_client import HttpClient

BASE_URL = "https://re.bluepoch.com"
API_PATH = "/activity/official/websites/information/query"
DETAIL_TMPL = f"{BASE_URL}/home/detail.html#newsId?{{}}"

BROWSER_HEADERS = {
    "Referer": f"{BASE_URL}/home/",
    "Origin": BASE_URL,
    "Content-Type": "application/json; charset=utf-8",
}

# EdgeOne 的风控返回码：不是接口坏了，是 TLS 指纹被认出来了
RISK_BLOCK_CODE = 5001

# 内部分类 → informationType
CATEGORY_PARAM: dict[str, str] = {
    "最新": "",
    "公告": "2",
    "活动": "3",
    "新闻": "4",
}

# informationType → 展示名（"" 是混合的，按标题之外无从区分，标「资讯」）
TYPE_NAME: dict[str, str] = {"": "资讯", "2": "公告", "3": "活动", "4": "新闻"}


def _parse_time(text: str) -> float:
    """把 "2026-09-23 18:01:08" 转成 Unix 秒；解析不了回 0（排序沉到末尾）。"""
    from datetime import datetime

    s = str(text or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def parse_items(payload: dict, limit: int = 0) -> list[NewsItem]:
    """把官网返回的 JSON 解析成 NewsItem 列表（纯函数，便于单测）。"""
    if not isinstance(payload, dict):
        raise SourceError("1999 接口返回结构异常")
    code = payload.get("code")
    if code == RISK_BLOCK_CODE:
        raise SourceError("1999 官网风控拦截（code 5001）：本机 TLS 指纹被 EdgeOne 拒绝")
    if code != 200:
        raise SourceError(f"1999 接口返回 code={code} msg={payload.get('msg')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceError("1999 接口缺少 data 字段")
    items: list[NewsItem] = []
    for raw in data.get("pageData") or []:
        if not isinstance(raw, dict):
            continue
        iid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not iid or not title:
            continue
        content_html = str(raw.get("content") or "")
        info_type = str(raw.get("informationType") or "")
        items.append(
            NewsItem(
                game="re1999",
                item_id=iid,
                title=title,
                category=TYPE_NAME.get(info_type, "资讯"),
                published=_parse_time(raw.get("onlineTime")),
                url=DETAIL_TMPL.format(iid),
                summary=strip_html(content_html, limit=80),
                content=content_html,
            )
        )
        if limit and len(items) >= limit:
            break
    return items


class Re1999Source:
    key = "re1999"
    display_name = "重返未来：1999"

    def __init__(self, client: HttpClient, page_size: int = 20) -> None:
        self.client = client
        self.page_size = page_size

    async def latest(self, limit: int = 10) -> list[NewsItem]:
        return await self.by_category("最新", limit=limit)

    async def by_category(self, category: str, limit: int = 20) -> list[NewsItem]:
        param = CATEGORY_PARAM.get(category)
        if param is None:
            raise SourceError(f"1999 没有分类「{category}」")
        # 最新列表要扫码，卡池/版本要正文全文，两条都靠 content，
        # 一次多取几条比来回翻页划算（单条正文约 7KB）
        size = self.page_size if limit <= 0 else max(limit, self.page_size)
        payload = await self.client.request_json(
            "POST",
            f"{BASE_URL}{API_PATH}",
            json_body={
                "id": "",
                "informationType": param,
                "current": 1,
                "pageSize": size,
                "isTop": "",
            },
            headers=BROWSER_HEADERS,
        )
        return parse_items(payload, limit=limit)

    async def fetch_page(self, category: str, page: int = 1) -> list[NewsItem]:
        """拉指定页的原始条目（档案库深抓/回填用）。

        ⚠️ 1999 的正文在列表接口里（content 字段，单条约 7KB），一页 8 条
        就是 ~56KB——深抓必须串行小步走，别把 EdgeOne 惹毛（TLS 指纹稍异
        直接 code=5001）。翻过末页 pageData 为空列表，调用方拿它当停止信号。
        """
        param = CATEGORY_PARAM.get(category)
        if param is None:
            raise SourceError(f"1999 没有分类「{category}」")
        payload = await self.client.request_json(
            "POST",
            f"{BASE_URL}{API_PATH}",
            json_body={
                "id": "",
                "informationType": param,
                "current": max(1, int(page)),
                "pageSize": 8,
                "isTop": "",
            },
            headers=BROWSER_HEADERS,
        )
        return parse_items(payload, limit=0)

    async def close(self) -> None:
        await self.client.close()
