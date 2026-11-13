"""带 TTL 的内存缓存：查询结果短期复用，减少对数据源的重复请求。

为什么要有它：1999 官网挂着腾讯 EdgeOne 风控（TLS 指纹稍异就回 code=5001
「系统繁忙」），用户连问三次 /游戏 就是三次官网请求——纯粹在给风控送人头。
60 秒内的重复提问直接吃缓存，官网只看到一次。

设计要点：
- **只缓存成功结果**：失败的拉取让上层照旧走退避重试，绝不把一次失败
  冻结成 60 秒的「标准答案」。
- **条数语义**：缓存存的是「按某个 limit 拉到的完整列表」。这次要的
  条数 <= 缓存长度 → 截取返回（不打源）；比缓存长 → 返回 None 让调用方
  重拉（宁多打一次源，不给残缺列表）。
- **ttl <= 0 视为关闭**：get 永远 miss、put 直接丢弃——配置里把
  query_cache_seconds 填 0 即可完全回到「每次真打官网」。
"""

import time


class TTLCache:
    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 64) -> None:
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._data: dict[str, tuple[float, object]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, key: str, *, min_items: int = 0):
        """命中且（要求时）条数足够则返回缓存列表；否则 None 并顺手清掉过期项。"""
        entry = self._data.get(key)
        if entry is None:
            return None
        at, value = entry
        if not self.enabled or time.time() - at > self.ttl:
            self._data.pop(key, None)
            return None
        items = value if isinstance(value, list) else []
        if min_items and len(items) < min_items:
            return None
        return items

    def put(self, key: str, value) -> None:
        if not self.enabled:
            return
        if key not in self._data and len(self._data) >= self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.time(), value)

    def clear(self) -> None:
        self._data.clear()
