"""备忘录存储：data/plugin_data/astrbot_plugin_suite/memo/memo.json。

按 unified_msg_origin（会话）隔离；每条结构：
{id, umo, text, due, reminded, done, created_at, updated_at}

写频低（用户动作 + 扫描有变化才写），原子写 tmp+replace 即可，不做 debounce。
坏文件隔离为 *.corrupt-<时间戳>，从空库开始（对齐 electric storage 红线）。
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("astrbot.plugin.suite.memo")


class MemoStore:
    def __init__(self, path, keep_done_days=7):
        self.path = Path(path)
        self.keep_done_days = max(1, int(keep_done_days))
        self.data: dict = {"items": [], "next_id": 1}
        self.load()

    # ================= IO =================

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning(f"[memo] 备忘存储读取失败：{e!r}")
            return
        try:
            parsed = json.loads(raw)
        except ValueError:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                self.path.rename(
                    self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
                )
            except OSError:
                pass
            logger.warning("[memo] 备忘存储损坏，已隔离为 *.corrupt-*，从空库开始")
            return
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            self.data = {
                "items": [i for i in parsed["items"] if isinstance(i, dict)],
                "next_id": int(parsed.get("next_id") or 1),
            }

    def save(self):
        """原子写 tmp+replace + fsync（对齐套件存储红线）。

        备忘虽不如凭证历史关键，但「os.replace 成功而数据还在页缓存」的
        断电窗口同样存在：replace 后掉电 = 用户记的待办无声消失。写频低
        （用户动作 + 扫描有变化才写），多一次 fsync 毫无压力。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ================= 增查改 =================

    def add(self, umo, text, due=None, now=None) -> dict:
        now = int(now if now is not None else time.time())
        item = {
            "id": int(self.data["next_id"]),
            "umo": umo,
            "text": str(text),
            "due": int(due) if due else None,
            "reminded": False,
            "done": False,
            "created_at": now,
            "updated_at": now,
        }
        self.data["next_id"] += 1
        self.data["items"].append(item)
        return item

    def items_of(self, umo, include_done=True) -> list[dict]:
        """本会话条目：未完成按到期时间排序（不限时的排最后），已完成垫底。"""
        mine = [i for i in self.data["items"] if i.get("umo") == umo]
        pending = [i for i in mine if not i.get("done")]
        pending.sort(
            key=lambda i: (
                i.get("due") is None,
                i.get("due") or 0,
                i.get("created_at", 0),
            )
        )
        if not include_done:
            return pending
        done = [i for i in mine if i.get("done")]
        return pending + done

    def pending_count(self, umo) -> int:
        return len(self.items_of(umo, include_done=False))

    def complete(self, umo, memo_id) -> dict | None:
        for item in self.data["items"]:
            if (
                item.get("umo") == umo
                and item.get("id") == memo_id
                and not item.get("done")
            ):
                item["done"] = True
                item["updated_at"] = int(time.time())
                return item
        return None

    def delete(self, umo, memo_id) -> dict | None:
        for idx, item in enumerate(self.data["items"]):
            if item.get("umo") == umo and item.get("id") == memo_id:
                return self.data["items"].pop(idx)
        return None

    # ================= 提醒扫描 =================

    def due_items(self, now_ts) -> list[dict]:
        """到期该推的：未完成、未推过、带时间且已到点。"""
        return [
            i
            for i in self.data["items"]
            if not i.get("done")
            and not i.get("reminded")
            and i.get("due") is not None
            and i["due"] <= now_ts
        ]

    def mark_reminded(self, memo_id, now_ts) -> bool:
        for item in self.data["items"]:
            if item.get("id") == memo_id:
                item["reminded"] = True
                item["updated_at"] = int(now_ts)
                return True
        return False

    def sweep(self, now_ts) -> int:
        """清理已完成超过保留期的条目（已提醒未完成的不删，用户自己收尾）。"""
        horizon = int(now_ts) - self.keep_done_days * 86400
        kept, removed = [], 0
        for item in self.data["items"]:
            if item.get("done") and int(item.get("updated_at", 0)) < horizon:
                removed += 1
            else:
                kept.append(item)
        self.data["items"] = kept
        return removed
