"""内存事件流按会话隔离（A 批次 P0 修复）的规格测试。

旧实现是全局单 deque(200)：一个活跃群一次批量预警就能把其他会话的事件
全部挤掉，/电费 日志 与自检的「最近事件」瞬间变空窗——而展示端本来就按
umo 过滤，全局混存毫无意义。新实现：umo → deque(EVENTS_PER_SESSION)，
每会话各自有界、互不挤占；会话数另有硬顶（挤掉最久没动静的）。
"""

import asyncio
import time

from astrbot_plugin_suite.modules.electric.service import ElectricCore
from conftest import make_plugin

UMO = "qq:private:1"
UMO2 = "qq:private:2"


class _Ev:
    def __init__(self, umo):
        self.unified_msg_origin = umo

    def plain_result(self, text):
        return text


def _plugin():
    plugin = make_plugin()
    return plugin


# ================= 基本行为 =================


def test_record_event_routes_to_own_bucket():
    plugin = _plugin()
    plugin._record_event(UMO, "poll", "甲")
    plugin._record_event(UMO2, "alert", "乙")
    assert [e["text"] for e in plugin._events[UMO]] == ["甲"]
    assert [e["text"] for e in plugin._events[UMO2]] == ["乙"]


def test_busy_session_cannot_evict_other_sessions():
    """核心回归：A 会话狂刷事件，B 会话的事件必须原封不动。

    旧全局 deque 下，70 条 A 事件能把 B 的事件全部挤出 200 上限——
    「/电费 日志」在 B 会话直接变空窗。
    """
    plugin = _plugin()
    plugin._record_event(UMO2, "poll", "B 的唯一事件")
    for i in range(200):
        plugin._record_event(UMO, "poll", f"A 刷屏 {i}")
    assert len(plugin._events[UMO2]) == 1
    assert plugin._events[UMO2][0]["text"] == "B 的唯一事件"


def test_per_session_bucket_is_capped():
    plugin = _plugin()
    for i in range(ElectricCore.EVENTS_PER_SESSION + 10):
        plugin._record_event(UMO, "poll", f"e{i}")
    bucket = plugin._events[UMO]
    assert len(bucket) == ElectricCore.EVENTS_PER_SESSION
    # 有界 deque 挤掉的是最老的
    assert bucket[0]["text"] == "e10"
    assert bucket[-1]["text"].endswith(str(ElectricCore.EVENTS_PER_SESSION + 9))


def test_session_count_hard_cap_evicts_oldest(monkeypatch):
    """会话数超硬顶 → 挤掉「最久没动静」的会话整桶。"""
    monkeypatch.setattr(ElectricCore, "EVENTS_MAX_SESSIONS", 3)
    plugin = _plugin()
    old_ts = time.time() - 3600
    for umo, ts in ((UMO, old_ts), ("u2", time.time()), ("u3", time.time())):
        plugin._record_event(umo, "poll", "x")
        plugin._events[umo][-1]["t"] = ts
    plugin._record_event("u4", "poll", "新来的")
    assert UMO not in plugin._events  # 最久没动静的被挤掉
    assert set(plugin._events) == {"u2", "u3", "u4"}
    assert len(plugin._events) == 3


def test_terminate_clears_all_buckets():
    plugin = _plugin()
    plugin._record_event(UMO, "poll", "x")
    plugin._record_event(UMO2, "poll", "y")
    asyncio.run(plugin.terminate())
    assert not plugin._events


# ================= 端到端：/电费 日志 只看得到本会话桶 =================


def test_cmd_log_renders_only_own_session():
    plugin = _plugin()
    plugin._record_event(UMO, "poll", "本会话事件")
    plugin._record_event(UMO2, "alert", "别会话事件（不可见）")

    async def _drive():
        return [item async for item in plugin.cmd_log(_Ev(UMO), None)]

    text = "".join(str(x) for x in asyncio.run(_drive()))
    assert "本会话事件" in text
    assert "别会话事件" not in text
