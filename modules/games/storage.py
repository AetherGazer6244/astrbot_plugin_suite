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

DEFAULT_DATA: dict = {
    "seen": {},
    "subscriptions": {},
    "codes": {},
    # v1.2.0：AI 摘要落盘（key → {ck, text, at}），重启后不用等下一轮重新生成
    "digests": {},
    # v1.2.0：广播失败的兑换码队列（key → [records]），补发前过滤已过期
    "pending_codes": {},
}
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
            data.setdefault("digests", {})
            data.setdefault("pending_codes", {})
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
        prev = self.data["subscriptions"].get(umo)
        sub: dict = {
            "games": sorted(set(games)),
            "dynamics": bool(dynamics),
            "updated_at": time.time(),
        }
        # 静音状态是会话自己的选择，不能因为一次订阅操作就被无声冲掉
        if isinstance(prev, dict) and prev.get("muted_until"):
            sub["muted_until"] = prev["muted_until"]
        self.data["subscriptions"][umo] = sub

    def del_subscription(self, umo: str) -> bool:
        return self.data["subscriptions"].pop(umo, None) is not None

    def is_muted(self, umo: str) -> bool:
        try:
            return float(self.subscription(umo).get("muted_until") or 0) > time.time()
        except (TypeError, ValueError):
            return False

    def set_muted(self, umo: str, hours: float) -> float:
        """静音本会话推送 N 小时。

        没订阅也允许静音：条目建成空订阅（games=[] 不影响任何广播匹配），
        用户之后订阅时 set_subscription 会保留 muted_until（v1.1.2 语义）。
        返回静音截止时间戳。
        """
        sub = self.data["subscriptions"].get(umo)
        if not isinstance(sub, dict):
            sub = {"games": [], "dynamics": False, "updated_at": time.time()}
            self.data["subscriptions"][umo] = sub
        until = time.time() + max(0.0, float(hours)) * 3600.0
        sub["muted_until"] = until
        return until

    def clear_muted(self, umo: str) -> float:
        """解除静音。返回原截止时间戳（本来就没静音返回 0）。"""
        sub = self.data["subscriptions"].get(umo)
        if isinstance(sub, dict):
            prev = sub.pop("muted_until", None)
            try:
                return float(prev or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def muted_until(self, umo: str) -> float:
        try:
            return float(self.subscription(umo).get("muted_until") or 0)
        except (TypeError, ValueError):
            return 0.0

    # ---------- AI 摘要落盘（v1.2.0） ----------

    def set_digest(self, key: str, ck, text: str) -> None:
        self.data["digests"][key] = {
            "ck": [str(x) for x in ck],
            "text": str(text),
            "at": time.time(),
        }

    def all_digests(self) -> dict:
        """全部落盘摘要，形状与内存 _digest_cache 一致：key → (ck元组, text)。"""
        out: dict = {}
        for key, raw in self.data["digests"].items():
            if isinstance(raw, dict) and raw.get("text") and isinstance(raw.get("ck"), list):
                out[str(key)] = (tuple(str(x) for x in raw["ck"]), str(raw["text"]))
        return out

    # ---------- 兑换码广播失败队列（v1.2.0 落盘） ----------

    def pending_codes(self, key: str) -> list[dict]:
        raw = self.data["pending_codes"].get(key)
        if not isinstance(raw, list):
            return []
        return [r for r in raw if isinstance(r, dict)]

    def set_pending_codes(self, key: str, records: list[dict]) -> None:
        if records:
            self.data["pending_codes"][key] = list(records)
        else:
            self.data["pending_codes"].pop(key, None)

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
