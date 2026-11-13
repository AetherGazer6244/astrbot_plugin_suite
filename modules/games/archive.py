"""资讯档案库：全量抓下来的历史公告落盘，查询走本地不打官网（v1.3.0）。

为什么独立一个文件（不塞 store.json）：方舟公告 918 条 + 1999 全文条目
（单条 ~3KB 纯文本），档案几 MB——塞进 store.json 会让每次订阅/静音的
小改动都全量重写几 MB。档案的写入节奏完全不同（回填批量写、轮询增量写），
独立文件、独立去抖。

条目形状（与 NewsItem 字段对齐，round-trip 重建后视图/AI 工具零改动）：
    {id, game, title, category, published, summary, text, url, source, first_seen}
    - source 是**渠道名**（官网 / B站动态 / B站视频），不是网址——聊天输出
      不放裸链接是 v1.1.0 的用户决策，档案里存渠道用于「真实度」标注。

游标（cursor）：深抓「从第 1 页翻到整页已归档即停」的滚动位置。
- 整页都已归档 → 说明前面没有缺口，游标重置回 1 进入巡检模式（下轮从
  第 1 页找新公告——新公告只会出现在最前面）。
- 页里有新条目 → 游标 +1 继续往后翻（回填进行中 / 大量补发）。
- 空页 → 翻过历史末端，重置回 1。
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("astrbot.plugin.games_monitor")

# 保留窗口与硬顶（用户选「深」档：全量回填 + 保留 1 年）
DEFAULT_KEEP_DAYS = 365
DEFAULT_MAX_PER_GAME = 1500
SAVE_DEBOUNCE_SECONDS = 60.0

# 渠道名常量在 providers.base（渠道是 provider 侧事实），这里导入并 re-export
# ——service 与 bilibili 统一从各自顺手的位置导入，真值只有这一份。
try:
    from .providers.base import (
        ARCHIVE_SOURCE_BILI_DYN,
        ARCHIVE_SOURCE_BILI_VIDEO,
        ARCHIVE_SOURCE_OFFICIAL,
    )
except ImportError:  # 兜底：被以非包方式加载时
    from providers.base import (  # type: ignore
        ARCHIVE_SOURCE_BILI_DYN,
        ARCHIVE_SOURCE_BILI_VIDEO,
        ARCHIVE_SOURCE_OFFICIAL,
    )

# re-export 清单：service 从本模块导入渠道常量，真值只有 providers.base 一份
__all__ = [
    "ARCHIVE_SOURCE_BILI_DYN",
    "ARCHIVE_SOURCE_BILI_VIDEO",
    "ARCHIVE_SOURCE_OFFICIAL",
    "ArchiveStore",
]


class ArchiveStore:
    def __init__(self, path: Path, keep_days: int = DEFAULT_KEEP_DAYS,
                 max_per_game: int = DEFAULT_MAX_PER_GAME) -> None:
        self.path = Path(path)
        self.keep_days = int(keep_days)
        self.max_per_game = int(max_per_game)
        self.data: dict = {"items": {}, "cursor": {}, "meta": {}}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data["items"] = raw.get("items") if isinstance(raw.get("items"), dict) else {}
                self.data["cursor"] = raw.get("cursor") if isinstance(raw.get("cursor"), dict) else {}
                self.data["meta"] = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        except (OSError, ValueError) as e:
            # 坏文件隔离保留现场（电费/套件红线：不无声清零）
            stamp = time.strftime("%Y%m%d-%H%M%S")
            quarantine = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, quarantine)
                logger.error(f"档案库文件损坏，已隔离为 {quarantine.name}：{e!r}")
            except OSError:
                logger.error(f"档案库文件损坏且隔离失败（忽略，按空档案继续）：{e!r}")
            self.data = {"items": {}, "cursor": {}, "meta": {}}

    def save(self, force: bool = False) -> None:
        """去抖落盘；回填批次结束、游标推进等关键时刻用 force。

        原子写 + fsync：断电不留「已 replace 但内容为空」的文件（套件红线，
        memo 模块 v1.1.1 补过同款 fsync）。
        """
        now = time.time()
        if not force and (not self._dirty or now - self._last_save < SAVE_DEBOUNCE_SECONDS):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # Windows 上 replace 原子覆盖已有文件
        self._dirty = False
        self._last_save = now

    # ---------- 条目 ----------

    def has(self, game: str, item_id: str) -> bool:
        return str(item_id) in self.data["items"].get(game, {})

    def upsert(self, items, source: str = ARCHIVE_SOURCE_OFFICIAL) -> int:
        """增量入库。返回**新**入库条数（已有条目不覆盖——首见时间不动，
        正文也不更新：官网极少改已发布公告，改了以轮询最新页为准也没意义）。
        """
        bucket = self.data["items"]
        now = time.time()
        added = 0
        for it in items:
            game_bucket = bucket.setdefault(it.game, {})
            iid = str(it.item_id)
            if iid in game_bucket:
                continue
            game_bucket[iid] = {
                "id": iid,
                "game": it.game,
                "title": str(it.title),
                "category": str(it.category),
                "published": float(it.published or 0),
                "summary": str(getattr(it, "summary", "") or ""),
                "text": str(getattr(it, "content", "") or ""),
                "url": str(getattr(it, "url", "") or ""),
                "source": source,
                "first_seen": now,
            }
            added += 1
        if added:
            self._dirty = True
        return added

    def entries(self, game: str | None = None, category: str | None = None,
                keyword: str | None = None, days: float | None = None,
                limit: int = 0) -> list[dict]:
        """按条件查询，时间倒序。keyword 对标题+正文子串匹配（不区分大小写）。"""
        now = time.time()
        games = [game] if game else list(self.data["items"])
        kw = (keyword or "").strip().lower()
        floor = (now - days * 86400) if days else 0
        out: list[dict] = []
        for g in games:
            for rec in self.data["items"].get(g, {}).values():
                if not isinstance(rec, dict):
                    continue
                if category and rec.get("category") != category:
                    continue
                pub = float(rec.get("published") or 0)
                if floor and pub < floor:
                    continue
                if kw:
                    hay = f"{rec.get('title','')}\n{rec.get('text','')}".lower()
                    if kw not in hay:
                        continue
                out.append(rec)
        out.sort(key=lambda r: float(r.get("published") or 0), reverse=True)
        if limit and len(out) > limit:
            out = out[:limit]
        return out

    def count(self, game: str | None = None) -> int:
        if game:
            return len(self.data["items"].get(game, {}))
        return sum(len(v) for v in self.data["items"].values())

    def latest_published(self, game: str) -> float:
        best = 0.0
        for rec in self.data["items"].get(game, {}).values():
            if isinstance(rec, dict):
                best = max(best, float(rec.get("published") or 0))
        return best

    # ---------- 游标 ----------

    def cursor(self, game: str, category: str) -> int:
        raw = (self.data["cursor"].get(game) or {}).get(category)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def set_cursor(self, game: str, category: str, page: int) -> None:
        self.data["cursor"].setdefault(game, {})[category] = max(1, int(page))
        self._dirty = True

    # ---------- 元信息 ----------

    def meta(self, key: str, default=None):
        return self.data["meta"].get(key, default)

    def set_meta(self, key: str, value) -> None:
        self.data["meta"][key] = value
        self._dirty = True

    # ---------- 裁剪 ----------

    def prune(self) -> int:
        """清超窗条目（keep_days）与超硬顶（max_per_game，丢最老的）。返回清理数。"""
        now = time.time()
        removed = 0
        floor = now - self.keep_days * 86400
        for game, bucket in list(self.data["items"].items()):
            for iid in [k for k, r in bucket.items()
                        if isinstance(r, dict) and float(r.get("published") or 0) < floor]:
                bucket.pop(iid, None)
                removed += 1
            if len(bucket) > self.max_per_game:
                ranked = sorted(bucket.values(),
                                key=lambda r: float(r.get("published") or 0), reverse=True)
                keep = {str(r.get("id")) for r in ranked[: self.max_per_game]}
                for iid in [k for k in bucket if k not in keep]:
                    bucket.pop(iid, None)
                    removed += 1
            if not bucket:
                self.data["items"].pop(game, None)
        if removed:
            self._dirty = True
        return removed
