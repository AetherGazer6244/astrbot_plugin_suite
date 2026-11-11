"""MemoCore 行为测试：指令流、LLM 工具、扫描推送、存储生命周期。

推送红线：发送成功才 mark_reminded（ok=False 不记账，下一轮重发）——
与 electric 的 _flush_alerts / _notify_session_dead 同一条可靠性约束。
"""

import asyncio
import time

from astrbot_plugin_suite.modules.memo import service as memo_service
from astrbot_plugin_suite.modules.memo.storage import MemoStore
from conftest import PRIVATE_UMO, FakeContext, FakeEvent, make_memo_plugin

OTHER_UMO = "aiocqhttp:GroupMessage:777"


def _collect(agen):
    async def run():
        return [r async for r in agen]

    return asyncio.run(run())


def _plugin(tmp_path, config=None, ok=True):
    store = MemoStore(tmp_path / "memo.json")
    ctx = FakeContext(ok=ok)
    plugin = make_memo_plugin(config=config, store=store, context=ctx)
    return plugin, store, ctx


# ================= 指令流 =================


def test_cmd_memo_empty_shows_help(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    ev = FakeEvent("/备忘")
    out = _collect(plugin.cmd_memo(ev))
    assert len(out) == 1 and "备忘录用法" in out[0]
    assert ev.result == out[0]


def test_cmd_memo_add_plain_todo(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path)
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 交作业")))
    assert "已记下（#1）：交作业" in out[0]
    assert "纯待办" in out[0]
    assert store.data["items"][0]["due"] is None


def test_cmd_memo_add_with_time(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path)
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 30分钟后 买酱油")))
    assert "已记下" in out[0] and "会提醒这个会话" in out[0]
    item = store.data["items"][0]
    assert item["due"] is not None and item["due"] > int(time.time())
    assert item["text"] == "买酱油"


def test_cmd_memo_list_complete_delete(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    _collect(plugin.cmd_memo(FakeEvent("/备忘 任务甲")))
    _collect(plugin.cmd_memo(FakeEvent("/备忘 任务乙")))
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 列表")))
    assert "未完成 2 条" in out[0] and "#1" in out[0] and "#2" in out[0]

    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 完成 1")))
    assert "已完成：任务甲" in out[0]
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 列表")))
    assert "未完成 1 条" in out[0] and "已完成 1 条" in out[0]

    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 删除 2")))
    assert "已删除：任务乙" in out[0]
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 列表")))
    assert "全部完成" in out[0]


def test_cmd_memo_complete_and_delete_need_valid_id(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 完成")))
    assert "要完成哪条" in out[0]
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 完成 99")))
    assert "没找到" in out[0]
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 删除 abc")))
    assert "要删哪条" in out[0]


def test_cmd_memo_bare_number_hints_complete(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 3")))
    assert "/备忘 完成 3" in out[0]


def test_capacity_limit(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path, config={"memo_max_per_session": 2})
    plugin.store.add(PRIVATE_UMO, "一")
    plugin.store.add(PRIVATE_UMO, "二")
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 第三条")))
    assert "未完成备忘已有 2 条" in out[0]
    assert len(plugin.store.data["items"]) == 2, "满员时不得写入"


def test_store_not_ready_message(tmp_path):
    plugin = make_memo_plugin(config={}, store=None, context=FakeContext())
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 列表")))
    assert "还没初始化完成" in out[0]


# ================= 会话隔离 =================


def test_cross_session_isolation(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path)
    store.add(PRIVATE_UMO, "我的备忘")
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 列表", umo=OTHER_UMO)))
    assert "本会话还没有备忘" in out[0]
    out = _collect(plugin.cmd_memo(FakeEvent("/备忘 完成 1", umo=OTHER_UMO)))
    assert "没找到" in out[0]
    assert store.data["items"][0]["done"] is False, "别的会话不许动我的条目"


# ================= 到期扫描与推送 =================


def test_scan_pushes_due_and_marks_reminded(tmp_path):
    plugin, store, ctx = _plugin(tmp_path)
    now = int(time.time())
    store.add(PRIVATE_UMO, "到期的提醒", due=now - 10)
    store.add(PRIVATE_UMO, "还没到点的", due=now + 3600)
    asyncio.run(plugin._scan())
    assert len(ctx.sent) == 1
    session, text = ctx.sent[0]
    assert session == PRIVATE_UMO and "⏰ 备忘提醒：到期的提醒" in text
    items = {i["text"]: i for i in store.data["items"]}
    assert items["到期的提醒"]["reminded"] is True
    assert items["还没到点的"]["reminded"] is False


def test_scan_does_not_repeat_after_reminded(tmp_path):
    plugin, store, ctx = _plugin(tmp_path)
    store.add(PRIVATE_UMO, "一次就好", due=int(time.time()) - 5)
    asyncio.run(plugin._scan())
    asyncio.run(plugin._scan())
    assert len(ctx.sent) == 1


def test_scan_send_failure_retries_next_round(tmp_path):
    plugin, store, ctx = _plugin(tmp_path, ok=False)
    store.add(PRIVATE_UMO, "失败的提醒", due=int(time.time()) - 5)
    asyncio.run(plugin._scan())
    assert len(ctx.sent) == 1
    assert store.data["items"][0]["reminded"] is False, "发送失败不记账"
    assert store.path.exists() is False, "没有可记账的变化就不落盘"
    ctx.ok = True
    asyncio.run(plugin._scan())
    assert len(ctx.sent) == 2, "下一轮重发"
    assert store.data["items"][0]["reminded"] is True


def test_scan_sweeps_old_done(tmp_path):
    plugin, store, ctx = _plugin(tmp_path, config={"memo_keep_done_days": 7})
    now = int(time.time())
    old_done = store.add(PRIVATE_UMO, "很久前完成的")
    old_done["done"] = True
    old_done["updated_at"] = now - 8 * 86400
    fresh_done = store.add(PRIVATE_UMO, "刚完成的")
    fresh_done["done"] = True
    store.save()
    store.add(PRIVATE_UMO, "一直没做的", due=now - 8 * 86400)  # 超期未完成：不清理
    # 但它已到点且未提醒过——为免干扰「清扫」断言，标记成已提醒（不推送）
    store.data["items"][-1]["reminded"] = True
    asyncio.run(plugin._scan())
    texts = [i["text"] for i in store.data["items"]]
    assert "很久前完成的" not in texts
    assert "刚完成的" in texts and "一直没做的" in texts
    assert ctx.sent == [], "超期未完成不该被推送"


# ================= 存储生命周期 =================


def test_corrupt_file_quarantined(tmp_path):
    path = tmp_path / "memo.json"
    path.write_text("{not json", encoding="utf-8")
    MemoStore(path)
    assert not path.exists()
    quarantined = list(tmp_path.glob("memo.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json", "隔离保留现场"


def test_store_roundtrip(tmp_path):
    path = tmp_path / "memo.json"
    store = MemoStore(path)
    store.add(PRIVATE_UMO, "任务", due=123456)
    store.save()
    store2 = MemoStore(path)
    assert store2.data["items"][0]["text"] == "任务"
    assert store2.data["items"][0]["due"] == 123456
    assert store2.data["next_id"] == 2


def test_pending_sort_puts_unscheduled_last(tmp_path):
    store = MemoStore(tmp_path / "memo.json")
    a = store.add(PRIVATE_UMO, "不限时")
    b = store.add(PRIVATE_UMO, "较早到期", due=2000)
    c = store.add(PRIVATE_UMO, "较晚到期", due=3000)
    assert [i["id"] for i in store.items_of(PRIVATE_UMO)] == [b["id"], c["id"], a["id"]]


# ================= LLM 工具 =================


def test_tool_memo_add_list_complete(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    ev = FakeEvent("提醒我交作业")
    out = asyncio.run(plugin.tool_memo_add(ev, text="交作业"))
    assert "已记下（#1）：交作业" in out and "纯待办" in out
    out = asyncio.run(plugin.tool_memo_list(ev))
    assert "#1" in out
    out = asyncio.run(plugin.tool_memo_complete(ev, memo_id="1"))
    assert "已完成：交作业" in out


def test_tool_memo_add_with_when(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path)
    out = asyncio.run(plugin.tool_memo_add(FakeEvent(), text="买酱油", when="30分钟后"))
    assert "会提醒" in out
    assert store.data["items"][0]["text"] == "买酱油"


def test_tool_memo_add_rejects_unparsed_when(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path)
    out = asyncio.run(plugin.tool_memo_add(FakeEvent(), text="买菜", when="随时"))
    assert "没看懂" in out and "随时" in out
    assert store.data["items"] == [], "时间没解析出来不许静默降级成纯待办"


def test_tool_memo_add_empty_text(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    out = asyncio.run(plugin.tool_memo_add(FakeEvent(), text="", when=""))
    assert "想记什么" in out


def test_tool_memo_complete_needs_real_id(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    out = asyncio.run(plugin.tool_memo_complete(FakeEvent(), memo_id="abc"))
    assert "编号要是数字" in out
    out = asyncio.run(plugin.tool_memo_complete(FakeEvent(), memo_id="9"))
    assert "没找到" in out


def test_tool_memo_capacity(tmp_path):
    plugin, store, _ctx = _plugin(tmp_path, config={"memo_max_per_session": 1})
    store.add(PRIVATE_UMO, "占位")
    out = asyncio.run(plugin.tool_memo_add(FakeEvent(), text="再记一条"))
    assert "未完成备忘已有 1 条" in out


def test_tools_disabled_by_config(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path, config={"ai_memo_tools_enabled": False})
    ev = FakeEvent()
    for coro in (
        plugin.tool_memo_add(ev, text="x"),
        plugin.tool_memo_list(ev),
        plugin.tool_memo_complete(ev, memo_id="1"),
    ):
        assert "已在插件配置里关闭" in asyncio.run(coro)


# ================= 幕后提示注入 =================


class _Req:
    def __init__(self, system_prompt=""):
        self.system_prompt = system_prompt


def test_inject_memo_hint_on_keyword(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    req = _Req("你是助手")
    asyncio.run(plugin.inject_memo_hint(FakeEvent("记得提醒我明天交作业"), req))
    assert memo_service.AI_MEMO_HINT in req.system_prompt
    assert req.system_prompt.startswith("你是助手")


def test_inject_memo_hint_without_keyword_is_noop(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    req = _Req("你是助手")
    asyncio.run(plugin.inject_memo_hint(FakeEvent("今天天气不错"), req))
    assert req.system_prompt == "你是助手"


def test_inject_memo_hint_idempotent(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    req = _Req("")
    asyncio.run(plugin.inject_memo_hint(FakeEvent("帮我记一下"), req))
    once = req.system_prompt
    asyncio.run(plugin.inject_memo_hint(FakeEvent("帮我记一下"), req))
    assert req.system_prompt == once


def test_inject_memo_hint_respects_switches(tmp_path):
    for key in ("ai_memo_prompt_hint", "ai_memo_tools_enabled"):
        plugin, _store, _ctx = _plugin(tmp_path, config={key: False})
        req = _Req("你是助手")
        asyncio.run(plugin.inject_memo_hint(FakeEvent("提醒我喝水"), req))
        assert req.system_prompt == "你是助手", f"{key}=False 时不许注入"


def test_inject_scans_recent_contexts(tmp_path):
    plugin, _store, _ctx = _plugin(tmp_path)
    req = _Req("")
    req.contexts = [
        {"role": "user", "content": [{"type": "text", "text": "别忘了带伞"}]},
    ]
    asyncio.run(plugin.inject_memo_hint(FakeEvent("好的"), req))
    assert memo_service.AI_MEMO_HINT in req.system_prompt
