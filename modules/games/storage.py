"""JSON 文件存储：已读 ID、订阅关系、兑换码库。

存放于 AstrBot 数据目录 data/plugin_data/<plugin_name>/ 下，插件更新重装不丢数据。

沿用电费插件的两条纪律：
- **原子写 + fsync**：断电时不会留下「已 replace 但内容为空」的文件。
- **写盘去抖 + 损坏隔离**：轮询侧走去抖（60s），用户动作（订阅/退订）一律
  force=True；坏文件改名成 *.corrupt-<ts> 保留现场，不无声清零。
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("astrbot.plugin.games_monitor")

DEFAULT_DATA: dict = {"seen": {}, "subscriptions": {}, "codes": {}}
# 每个来源最多记多少条已读 ID。10 分钟一轮、一天 144 轮，记 400 条足够，
# 再多只是让存储文件无限膨胀。
MAX_SEEN_PER_SOURCE = 400
SAVE_DEBOUNCE_SECONDS = 60.0


class Store:
    def __init__(self, path: Path, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = max(1, int(keep_days))
        self.data = json.loads(json.dumps(DEFAULT_DATA))
        self._last_saved_at = 0.0
        self._dirty = False
        self._quarantine_failed = False
        self.load()

    # ---------- 落盘 ----------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("存储文件读取失败（%s: %s）：%s", type(e).__name__, e, self.path)
            self._quarantine_corrupt()
            return
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            data.setdefault("subscriptions", {})
            data.setdefault("codes", {})
            self.data = data
        else:
            logger.error("存储文件结构异常（seen 不是 dict），已按空数据起步：%s", self.path)
            self._quarantine_corrupt()

    def _quarantine_corrupt(self) -> None:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.path.rename(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
        except OSError as e:
            if not self._quarantine_failed:
                self._quarantine_failed = True
                logger.error("损坏存储文件改名失败（下次保存会覆盖，原始现场将丢失）：%s", e)

    def save(self, force: bool = False) -> None:
        """原子写；force=False 时 60 秒去抖（用户动作与退出时必须 force）。"""
        now = time.time()
        if not force and (now - self._last_saved_at) < SAVE_DEBOUNCE_SECONDS:
            self._dirty = True
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._last_saved_at = now
        self._dirty = False
        self._quarantine_failed = False

    def flush(self) -> bool:
        if not self._dirty:
            return False
        self.save(force=True)
        return True

    # ---------- 已读 ID ----------

    def seen_ids(self, source: str) -> set[str]:
        raw = self.data["seen"].get(source) or []
        return {str(x) for x in raw if isinstance(x, (str, int))}

    def mark_seen(self, source: str, ids) -> None:
        """把新 ID 并入已读集合并裁剪到上限。返回是否真的有新增。"""
        bucket = self.data["seen"].setdefault(source, [])
        known = {str(x) for x in bucket}
        added = False
        for i in ids:
            key = str(i)
            if key in known:
                continue
            bucket.append(key)
            known.add(key)
            added = True
        if len(bucket) > MAX_SEEN_PER_SOURCE:
            del bucket[: len(bucket) - MAX_SEEN_PER_SOURCE]
        return added

    def forget_seen(self, source: str, ids) -> None:
        """推送失败时回滚已读记账，下轮好重发。"""
        drop = {str(x) for x in ids}
        bucket = [x for x in self.data["seen"].get(source, []) if str(x) not in drop]
        self.data["seen"][source] = bucket

    # ---------- 订阅 ----------

    def subscription(self, umo: str) -> dict:
        return self.data["subscriptions"].get(umo) or {}

    def subscriptions(self) -> dict:
        return self.data["subscriptions"]

    def subscribers(self, game: str | None = None) -> list[tuple[str, dict]]:
        """返回 [(umo, sub)]；game 给了就只挑订阅了该游戏的会话。"""
        out = []
        for umo, sub in self.data["subscriptions"].items():
            if not isinstance(sub, dict):
                continue
            games = sub.get("games") or []
            if game and game not in games:
                continue
            out.append((umo, sub))
        return out

    def set_subscription(self, umo: str, games, dynamics: bool) -> None:
        self.data["subscriptions"][umo] = {
            "games": sorted(set(games)),
            "dynamics": bool(dynamics),
            "updated_at": time.time(),
        }

    def del_subscription(self, umo: str) -> bool:
        return self.data["subscriptions"].pop(umo, None) is not None

    def is_muted(self, umo: str) -> bool:
        try:
            return float(self.subscription(umo).get("muted_until") or 0) > time.time()
        except (TypeError, ValueError):
            return False

    # ---------- 兑换码 ----------

    def codes(self, game: str | None = None) -> list[dict]:
        all_codes: list[dict] = []
        for key, records in self.data["codes"].items():
            if game and key != game:
                continue
            for rec in records or []:
                if isinstance(rec, dict):
                    all_codes.append(rec)
        all_codes.sort(key=lambda r: float(r.get("found_at") or 0), reverse=True)
        return all_codes

    def set_codes(self, game: str, records: list[dict]) -> None:
        self.data["codes"][game] = records

    # ---------- 过期清理 ----------

    def prune(self) -> int:
        """清掉过期码与过期的订阅静音。返回清理条数。"""
        now = time.time()
        removed = 0
        for _key, records in list(self.data["codes"].items()):
            fresh = []
            for rec in records or []:
                try:
                    if float((rec or {}).get("expire_at") or 0) < now:
                        removed += 1
                        continue
                except (TypeError, ValueError):
                    pass
                fresh.append(rec)
            self.data["codes"][_key] = fresh
        for _umo, sub in list(self.data["subscriptions"].items()):
            try:
                if float((sub or {}).get("muted_until") or 0) and float(sub["muted_until"]) < now:
                    sub.pop("muted_until", None)
                    removed += 1
            except (TypeError, ValueError):
                sub.pop("muted_until", None)
        return removed
