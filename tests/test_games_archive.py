"""v1.3.0 资讯档案库的行为规格：本地库查询、深抓游标、回填、真实度三件套。

用户两条原始需求：「咨询信息渠道少、信息少」+「定时抓取信息，保存，方便
快速调用」。落点是三件事：

1. **ArchiveStore**：全量公告落本地（独立文件、原子写+fsync+去抖、
   坏文件隔离），upsert 只增不覆盖，entries 支持分类/关键词/时间窗过滤。
2. **深抓游标**：每小时从游标继续翻（整页已归档 → 重置 1 巡检；页里有新
   条目 → 推进；空页 → 末端重置 1；拉取失败 → **游标不动**下轮原处再试）。
   回填同理，但中途失败必须保住游标（百页进度不能一次异常就作废）。
3. **真实度三件套**：渠道标签（news_lines）、摘要附原文标题（视图/推送）、
   extract_time_ranges 的时间合理性窗口（发布-30 天 ~ 发布+2 年）。
"""

import asyncio
import time

import pytest
from astrbot_plugin_suite.modules.games.archive import (
    ARCHIVE_SOURCE_BILI_DYN,
    ARCHIVE_SOURCE_OFFICIAL,
    ArchiveStore,
)
from astrbot_plugin_suite.modules.games.formatting import news_lines
from astrbot_plugin_suite.modules.games.keywords import extract_time_ranges
from astrbot_plugin_suite.modules.games.providers import NewsItem, SourceError
from astrbot_plugin_suite.modules.games.storage import Store
from conftest import FakeContext, make_games_plugin

# 发布时刻基准：2026-10-15 12:00（时间段合理性的对照原点）
PUB = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))


def _ni(item_id, title="公告", published=0.0, game="re1999", category="公告",
        text="", source=""):
    return NewsItem(
        game=game,
        item_id=item_id,
        title=title,
        category=category,
        published=published or (time.time() - 60),
        url=f"https://example.com/{item_id}",
        summary="摘要",
        content=text or "正文",
        source=source,
    )


class PageSource:
    """按页返回造好的条目：pages[i] 是第 i+1 页；越界即空页（= 翻过末端）。

    error 非空时抛异常（模拟官网/风控故障），与 FakeSource 的语义对齐。
    """

    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.calls: list[tuple] = []

    async def fetch_page(self, category, page=1):
        self.calls.append((category, page))
        if self.error:
            raise self.error
        idx = page - 1
        if idx < 0 or idx >= len(self.pages):
            return []
        return list(self.pages[idx])

    async def latest(self, limit=10):
        self.calls.append(("latest", limit))
        return [i for p in self.pages for i in p][:limit]

    async def by_category(self, category, limit=20):
        self.calls.append((category, limit))
        return [i for p in self.pages for i in p][:limit]

    async def close(self):
        pass


def _plugin(tmp_path, pages=None, archive=None, config=None, sources=None):
    arch = archive if archive is not None else ArchiveStore(tmp_path / "archive.json")
    src = PageSource(pages or [])
    p = make_games_plugin(
        config=config or {},
        store=Store(tmp_path / "s.json"),
        sources=sources if sources is not None else {"re1999": src},
        context=FakeContext(),
        archive=arch,
    )
    p._src = src
    return p, src, arch


# ==================== ArchiveStore 单元 ====================


def test_upsert_is_add_only_and_counts_new(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    n1 = _ni("1", "原题", published=PUB)
    assert arch.upsert([n1], ARCHIVE_SOURCE_OFFICIAL) == 1
    # 同 id 再入库：不覆盖（首见时间与标题都不动），返回 0
    n1b = _ni("1", "改成别的题", published=PUB)
    assert arch.upsert([n1b], ARCHIVE_SOURCE_OFFICIAL) == 0
    rec = arch.entries("re1999")[0]
    assert rec["title"] == "原题"
    assert arch.count("re1999") == 1


def test_entries_filters_by_category_keyword_days_limit(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    now = time.time()
    items = [
        _ni("a", "限定寻访开启", published=now - 100, category="公告"),
        _ni("b", "维护更新公告", published=now - 200, category="公告", text="停机说明"),
        _ni("c", "夏日活动汇总", published=now - 100 * 86400, category="活动"),
        _ni("d", "联动 PV 上线", published=now - 400 * 86400, category="新闻"),
    ]
    arch.upsert(items)
    # 分类
    assert {r["id"] for r in arch.entries("re1999", category="活动")} == {"c"}
    # 关键词（标题命中）
    assert {r["id"] for r in arch.entries("re1999", keyword="寻访")} == {"a"}
    # 关键词（正文命中）
    assert {r["id"] for r in arch.entries("re1999", keyword="停机")} == {"b"}
    # 时间窗（90 天内：a/b；c 是 100 天前被排除，d 是 400 天前）
    assert {r["id"] for r in arch.entries("re1999", days=90)} == {"a", "b"}
    # 放宽到 150 天 → c 回来
    assert {r["id"] for r in arch.entries("re1999", days=150)} == {"a", "b", "c"}
    # limit + 时间倒序
    got = arch.entries("re1999", limit=2)
    assert [r["id"] for r in got] == ["a", "b"]


def test_cursor_and_meta_roundtrip(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    assert arch.cursor("re1999", "公告") == 1  # 缺省 1
    arch.set_cursor("re1999", "公告", 7)
    arch.set_meta("backfilled_at", 123.0)
    arch.save(force=True)
    reloaded = ArchiveStore(tmp_path / "archive.json")
    assert reloaded.cursor("re1999", "公告") == 7
    assert reloaded.meta("backfilled_at") == 123.0


def test_prune_drops_expired_and_caps_per_game(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json", keep_days=30, max_per_game=3)
    now = time.time()
    old = [_ni(str(i), published=now - (40 + i) * 86400) for i in range(4)]
    fresh = [_ni(f"f{i}", published=now - i * 60) for i in range(5)]
    arch.upsert(old + fresh)
    removed = arch.prune()
    # 4 条超窗 + 2 条超硬顶（5 条新鲜的留 3 条最新的）
    assert removed == 6
    assert arch.count("re1999") == 3
    assert {r["id"] for r in arch.entries("re1999")} == {"f0", "f1", "f2"}


def test_corrupt_file_is_quarantined_not_silently_reset(tmp_path):
    path = tmp_path / "archive.json"
    path.write_text("{这不是 JSON", encoding="utf-8")
    arch = ArchiveStore(path)
    assert arch.count() == 0  # 按空档案继续
    quarantined = list(tmp_path.glob("archive.json.corrupt-*"))
    assert len(quarantined) == 1  # 坏文件保留现场


def test_save_debounce_and_force(tmp_path):
    path = tmp_path / "archive.json"
    arch = ArchiveStore(path)
    arch.upsert([_ni("1")])
    arch.save()  # 首次（_last_save=0）必然落盘
    assert path.exists()
    # 60s 去抖窗口内：dirty 后普通 save 不落盘，force 旁路
    arch.upsert([_ni("2")])
    path.unlink()
    arch.save()
    assert not path.exists()  # 去抖跳过
    arch.save(force=True)
    assert path.exists()
    assert ArchiveStore(path).count() == 2


# ==================== 深抓游标语义 ====================


def test_walk_advances_cursor_on_new_items(tmp_path):
    p, _src, arch = _plugin(
        tmp_path,
        pages=[[_ni("a"), _ni("b")], [_ni("c"), _ni("d")]],
    )
    # max_pages=2：两页翻完就收（不翻第三页的空页，否则会触发末端重置——
    # 那是另一个测试的事）
    cursor, added = asyncio.run(p._walk_category("re1999", "公告", 1, 2))
    assert added == 4
    assert cursor == 3  # 游标停在「下次要翻的第 3 页」
    assert arch.count("re1999") == 4
    assert arch.cursor("re1999", "公告") == 3


def test_walk_resets_when_page_fully_archived(tmp_path):
    p, _src, arch = _plugin(tmp_path, pages=[[_ni("a"), _ni("b")]])
    arch.upsert([_ni("a"), _ni("b")])
    arch.set_cursor("re1999", "公告", 5)
    cursor, added = asyncio.run(p._walk_category("re1999", "公告", 5, 10))
    assert added == 0
    assert cursor == 1  # 整页已归档 → 巡检模式
    assert arch.cursor("re1999", "公告") == 1


def test_walk_resets_on_empty_page(tmp_path):
    p, _src, arch = _plugin(tmp_path, pages=[[_ni("a")]])
    cursor, _added = asyncio.run(p._walk_category("re1999", "公告", 2, 10))
    assert cursor == 1  # 空页 = 翻过末端（也覆盖「深抓翻完历史」的巡检收敛）
    assert arch.cursor("re1999", "公告") == 1


def test_walk_keeps_cursor_on_failure(tmp_path):
    p, src, arch = _plugin(tmp_path, pages=[[_ni("a")]])
    arch.set_cursor("re1999", "公告", 9)
    src.error = SourceError("官网 500")
    cursor, added = asyncio.run(p._walk_category("re1999", "公告", 9, 10))
    assert added == 0
    assert cursor == 9  # 失败 ≠ 末端：游标不动，下轮原处再试
    assert arch.cursor("re1999", "公告") == 9


def test_backfill_walks_to_end_and_resets(tmp_path):
    # parallel=2：第一轮并发翻 1、2 页，第二轮翻 3 页（空）→ hit_end
    p, _src, arch = _plugin(
        tmp_path,
        pages=[[_ni("a"), _ni("b")], [_ni("c"), _ni("d")], []],
    )
    added = asyncio.run(p._backfill_walk("re1999", "公告", 2))
    assert added == 4
    assert arch.count("re1999") == 4
    assert arch.cursor("re1999", "公告") == 1


def test_backfill_stop_at_caught_up_batch_not_after_absorb(tmp_path):
    """回归：追平判定必须在 absorb **之前**统计，否则刚入库的必然全 known、
    回填永远停在第二批。场景：档案已有一二页，游标 3 → 只翻第 3 页就应停。"""
    arch = ArchiveStore(tmp_path / "archive.json")
    p1 = [_ni("a"), _ni("b")]
    p2 = [_ni("c"), _ni("d")]
    p3 = [_ni("e"), _ni("f")]
    arch.upsert(p1 + p2)
    arch.set_cursor("re1999", "公告", 3)
    p, _src, arch = _plugin(tmp_path, pages=[p1, p2, p3, []], archive=arch)
    added = asyncio.run(p._backfill_walk("re1999", "公告", 1))
    assert added == 2  # 只有第 3 页的 e/f 是新的
    assert {r["id"] for r in arch.entries("re1999")} == {"a", "b", "c", "d", "e", "f"}


def test_backfill_failure_preserves_cursor(tmp_path):
    """中途失败保游标：百页回填进度不能一次异常就作废。"""
    arch = ArchiveStore(tmp_path / "archive.json")
    arch.upsert([_ni("a")])
    arch.set_cursor("re1999", "公告", 7)
    p, src, arch = _plugin(tmp_path, pages=[[ _ni("x")]], archive=arch)
    src.error = SourceError("风控")
    added = asyncio.run(p._backfill_walk("re1999", "公告", 2))
    assert added == 0
    assert arch.cursor("re1999", "公告") == 7  # 游标没被打回 1


# ==================== 查询走本地库（新鲜度兜底） ====================


def test_archive_items_fresh_and_enough_skips_network(tmp_path):
    now = time.time()
    items = [_ni(str(i), published=now - i * 60) for i in range(15)]
    p, src, arch = _plugin(tmp_path, pages=[items])
    arch.upsert(items)
    calls_before = len(src.calls)
    got = asyncio.run(p._archive_items("re1999", 10))
    assert got is not None and len(got) == 10
    assert len(src.calls) == calls_before  # 库新且够 → 官网一次都不打


def test_archive_items_stale_triggers_live_fetch_and_absorb(tmp_path):
    stale_ts = time.time() - 48 * 3600  # 两天前：远超 2×轮询间隔
    items = [_ni(str(i), published=stale_ts - i * 60) for i in range(15)]
    fresh_page = [_ni("new1", published=time.time() - 30)]
    p, src, arch = _plugin(tmp_path, pages=[fresh_page])
    arch.upsert(items)
    got = asyncio.run(p._archive_items("re1999", 10))
    # 探了一次官网（fetch_page 兜底在 _category_cached→by_category），增量入库后回库查
    assert len(src.calls) >= 1
    assert {i.item_id for i in got} >= {"new1"}


def test_archive_items_official_down_still_serves_local(tmp_path):
    """官网挂了也照常回库里数据——这正是档案库的意义。"""
    stale_ts = time.time() - 48 * 3600
    items = [_ni(str(i), published=stale_ts - i * 60) for i in range(15)]
    p, src, arch = _plugin(tmp_path, pages=[items])
    arch.upsert(items)
    src.error = SourceError("官网 500")
    got = asyncio.run(p._archive_items("re1999", 10))
    assert got is not None and len(got) == 10


def test_archive_disabled_falls_back_to_direct_fetch(tmp_path):
    """archive=None（配置关闭）= 旧行为：直打官网缓存路径。"""
    now = time.time()
    items = [_ni(str(i), published=now - i * 60) for i in range(8)]
    p, _src, _arch = _plugin(tmp_path, pages=[items])
    p.archive = None
    got = asyncio.run(p._archive_items("re1999", 5))
    assert got is not None and len(got) == 5
    assert len(p._src.calls) == 1  # 直连了源


# ==================== 真实度三件套 ====================


def test_news_lines_channel_labels():
    dyn = _ni("d1", "动态标题", source=ARCHIVE_SOURCE_BILI_DYN)
    vid = _ni("v1", "视频标题", source="B站视频")
    off = _ni("o1", "官网公告", category="公告")
    lines = news_lines([dyn, vid, off])
    # 每条目产出「标题 + 摘要 + 日期」三行，标题行下标 0/3/6
    assert "[B站动态]" in lines[0] and "📺" in lines[0]
    assert "[B站视频]" in lines[3] and "🎬" in lines[3]
    assert "[公告]" in lines[6] and "📢" in lines[6]  # 官网条目保持分类口径


@pytest.mark.parametrize(
    ("text", "published", "expect"),
    [
        # 正常范围（发布前后几天）→ 输出
        ("活动时间：10月16日 16:00 - 10月30日 03:59，详见官网", PUB,
         ["10月16日 16:00 - 10月30日 03:59"]),
        # 未来超 +2 年（年份错配的解析事故）→ 丢弃
        ("活动时间：2030年10月16日 16:00 - 2030年10月30日 03:59", PUB, []),
        # 过去超 30 天宽容度 → 丢弃
        ("活动时间：2020年10月16日 16:00 - 2020年10月30日 03:59", PUB, []),
        # 非法日期（2月30日）单点 → 不输出，宁可少给不给错的
        ("维护时间：2026年2月30日06:00 更新",
         time.mktime((2026, 2, 25, 12, 0, 0, 0, 0, -1)), []),
        # 单点时间同样受窗口约束
        ("维护时间：2020年10月09日06:00 开始", PUB, []),
    ],
)
def test_extract_time_ranges_plausibility_window(text, published, expect):
    assert extract_time_ranges(text, published=published) == expect


def test_extract_time_ranges_without_published_keeps_old_behavior():
    """published 缺省 0 = 不过滤（旧数据/旧调用兼容）。"""
    text = "活动时间：2030年10月16日 16:00 - 2030年10月30日 03:59"
    assert extract_time_ranges(text) != []


def test_entry_to_item_roundtrip(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    n = _ni("1", "标题", published=PUB, text="正文内容", source=ARCHIVE_SOURCE_OFFICIAL)
    arch.upsert([n])
    p, _src, arch2 = _plugin(tmp_path, pages=[], archive=arch)
    recs = arch2.entries("re1999")
    item = p._entry_to_item(recs[0])
    assert isinstance(item, NewsItem)
    assert item.item_id == "1" and item.title == "标题"
    assert item.published == pytest.approx(PUB)
    assert item.content == "正文内容"
    assert item.source == ARCHIVE_SOURCE_OFFICIAL


# ==================== AI 工具 games_history ====================


def test_tool_games_history_renders_entries(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    now = time.time()
    arch.upsert([
        _ni("1", "九月维护公告", published=now - 5 * 86400, text="停机 6 小时补偿 200 石"),
        _ni("2", "旧卡池", published=now - 120 * 86400),
    ])
    p, _src, _arch = _plugin(tmp_path, pages=[], archive=arch)
    out = asyncio.run(p.tool_games_history(FakeContext(), "1999", "维护", 30))
    assert "九月维护公告" in out
    assert "停机 6 小时" in out  # 最新一条的正文摘录
    assert "旧卡池" not in out  # 120 天前超出 30 天窗口


def test_tool_games_history_empty_hints_widen(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    p, _src, _arch = _plugin(tmp_path, pages=[], archive=arch)
    out = asyncio.run(p.tool_games_history(FakeContext(), "1999", "", 30))
    assert "没有相关公告" in out and "365" in out  # 引导放大时间范围


def test_tool_games_history_days_clamped(tmp_path):
    arch = ArchiveStore(tmp_path / "archive.json")
    arch.upsert([_ni("1", "很旧的公告", published=time.time() - 400 * 86400)])
    p, _src, _arch = _plugin(tmp_path, pages=[], archive=arch)
    # days 传 99999 → 夹到 365 → 还是查不到（400 > 365）
    out = asyncio.run(p.tool_games_history(FakeContext(), "1999", "", 99999))
    assert "没有相关公告" in out


def test_tool_games_history_disabled_archive(tmp_path):
    p, _src, _arch = _plugin(tmp_path, pages=[])
    p.archive = None
    out = asyncio.run(p.tool_games_history(FakeContext(), "1999"))
    assert "档案库未启用" in out
