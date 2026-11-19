"""快捷数字菜单（/菜单 → 回数字直达）的测试。

覆盖矩阵：
1. 数字解析：阿拉伯/全角/中文数字/带前缀/带标点 ✅；多位数字 ❌
2. 布防与 TTL：过期不接管、跨会话隔离、发送者隔离
3. 菜单渲染：编号连续、取消项、TTL 提示
4. 条目过滤：模块关闭后条目不出现且重新连号
5. 接线（套件根 main.py）：/菜单 出菜单；数字回复执行对应动作
6. **回归护栏**：6 位确认码不被菜单吞（绑定确认链路不能断）
"""

import asyncio

from astrbot_plugin_suite.modules.quick.service import (
    QuickCore,
    parse_digit,
)
from conftest import FakeEvent, make_quick_plugin

UMO = "qq:private:1"
GROUP_UMO = "qq:group:9"


# ================= 数字解析 =================


def test_parse_digit_accepts_common_forms():
    """阿拉伯数字、全角、中文数字、带前缀、带尾标点都认。"""
    for text, want in (
        ("1", "1"),
        ("9", "9"),
        (" 1 ", "1"),
        ("１", "1"),  # 全角
        ("一", "1"),
        ("九", "9"),
        ("按1", "1"),
        ("选1", "1"),
        ("选择1", "1"),
        ("回复1", "1"),
        ("选 1", "1"),
        ("1。", "1"),
        ("1、", "1"),
        ("1.", "1"),
        ("1！", "1"),
    ):
        assert parse_digit(text) == want, f"{text!r} 应解析成 {want}"


def test_parse_digit_rejects_multi_digit():
    """多位数一律不认——6 位确认码、备忘编号都靠这条保护。"""
    for text in (
        "481526",  # 电费 6 位确认码
        "12",
        "481526 度电够吗",
        "1 度电",
        "abc",
        "",
        "   ",
        "第1",
    ):
        assert parse_digit(text) is None, f"{text!r} 不该被解析成数字"


def test_parse_digit_does_not_eat_embedded_digits():
    """「按一」可以，「一共3个」不行——后者是日常聊天，不能劫持。"""
    assert parse_digit("按一") == "1"
    assert parse_digit("一共3个") is None


# ================= 布防与 TTL =================


def _entries():
    async def h1(event):
        return "余额"

    async def h2(event):
        return "历史"

    return [("查电费余额", h1), ("电费历史", h2)]


def test_arm_assigns_consecutive_digits():
    plugin = make_quick_plugin()
    items = plugin.arm(UMO, "u1", _entries())
    assert [i.digit for i in items] == ["1", "2"]
    assert [i.label for i in items] == ["查电费余额", "电费历史"]


def test_take_returns_item_and_renews():
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    pick = plugin.take(UMO, "u1", "2")
    assert pick is not None
    assert pick.kind == "run"
    assert pick.item.label == "电费历史"


def test_take_without_arm_is_none():
    """没布防就不接管——消息照旧走 AI。"""
    plugin = make_quick_plugin()
    assert plugin.take(UMO, "u1", "1") is None


def test_take_after_expiry_is_none():
    """过期后不接管（TTL 到点自动失效）。"""
    plugin = make_quick_plugin(config={"quick_menu_ttl_seconds": 30})
    plugin.arm(UMO, "u1", _entries())
    plugin._armed[UMO].expires_at = 0  # 假装已过期
    assert plugin.take(UMO, "u1", "1") is None
    # 过期状态被顺手清掉
    assert UMO not in plugin._armed


def test_take_isolated_by_session():
    """A 会话布防的菜单，B 会话回数字不生效。"""
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    assert plugin.take("qq:private:2", "u1", "1") is None


def test_take_isolated_by_sender():
    """同一会话里别人打的数字不生效。"""
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    assert plugin.take(UMO, "u2", "1") is None


def test_cancel_digit_disarms():
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    pick = plugin.take(UMO, "u1", "0")
    assert pick is not None
    assert pick.kind == "cancel"
    # 取消后不再接管
    assert plugin.take(UMO, "u1", "1") is None


def test_out_of_range_digit_is_invalid():
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    pick = plugin.take(UMO, "u1", "7")
    assert pick is not None
    assert pick.kind == "invalid"
    # 越界不解除布防，还能继续选
    assert plugin.take(UMO, "u1", "1").kind == "run"


def test_multi_digit_confirm_code_is_not_consumed():
    """回归护栏：6 位确认码绝不能被菜单当成编号（否则绑定确认直接断链）。"""
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    assert plugin.take(UMO, "u1", "481526") is None


def test_arm_clears_previous_menu_for_same_session():
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    plugin.arm(UMO, "u1", [("只有一条", _entries()[0][1])])
    pick = plugin.take(UMO, "u1", "1")
    assert pick.item.label == "只有一条"
    assert plugin.take(UMO, "u1", "2").kind == "invalid"


# ================= 菜单渲染 =================


def test_menu_text_lists_items_and_cancel():
    plugin = make_quick_plugin()
    items = plugin.arm(UMO, "u1", _entries())
    text = plugin.menu_text(items)
    assert "1. 查电费余额" in text
    assert "2. 电费历史" in text
    assert "0. 取消菜单" in text
    assert "分钟内有效" in text or "秒内有效" in text


def test_menu_disabled_config():
    plugin = make_quick_plugin(config={"quick_menu_enabled": False})
    assert plugin.enabled() is False
    plugin2 = make_quick_plugin()
    assert plugin2.enabled() is True


def test_ttl_clamped():
    plugin = make_quick_plugin(config={"quick_menu_ttl_seconds": 5})
    assert plugin.ttl_seconds() == 30
    plugin2 = make_quick_plugin(config={"quick_menu_ttl_seconds": 99999})
    assert plugin2.ttl_seconds() == 3600


# ================= 接线：套件根 main.py =================


def _suite(config=None, context=None):
    from astrbot_plugin_suite.main import SuitePlugin

    class _Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

        def save_config(self):
            return None

    data = {
        "modules": {},
        "electric": {},
        "games": {},
        "memo": {},
        "quick": dict(config or {}),
    }
    return SuitePlugin(context or object(), _Cfg(data))


def _collect(agen):
    async def run():
        return [r async for r in agen]

    return asyncio.run(run())


def test_cmd_menu_renders_menu():
    """``/菜单`` 出一份带编号的菜单。"""
    plugin = _suite()
    out = _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    text = out[0]
    assert "1." in text
    assert "取消菜单" in text


def test_cmd_menu_group_is_refused():
    """群里不出菜单（数字回复只在私聊生效）。"""
    plugin = _suite()
    out = _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=GROUP_UMO, private=False)))
    assert "私聊" in out[0]


def test_cmd_menu_disabled_config():
    plugin = _suite(config={"quick_menu_enabled": False})
    out = _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    assert "关闭" in out[0]


def test_digit_reply_executes_action():
    """数字回复执行对应动作并回结果。"""
    plugin = _suite()
    called = []

    async def handler(event):
        called.append(event)
        return "⚡ 余额 50.00 度"

    plugin._quick_entries = lambda umo: [("查电费余额", handler)]
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=UMO)))
    assert len(called) == 1
    assert "余额 50.00 度" in out[0]


def test_digit_reply_without_menu_is_ignored():
    """没布防时数字消息不接管（消息照旧走 AI）。"""
    plugin = _suite()
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=UMO)))
    assert out == []


def test_digit_reply_in_group_is_ignored():
    """群里回数字不接管。"""
    plugin = _suite()
    plugin._quick_entries = lambda umo: []
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=GROUP_UMO, private=False)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=GROUP_UMO, private=False)))
    assert out == []


def test_digit_reply_confirm_code_not_consumed():
    """回归护栏：6 位确认码不被菜单吞掉。"""
    plugin = _suite()
    called = []

    async def handler(event):
        called.append(event)
        return "结果"

    plugin._quick_entries = lambda umo: [("查电费余额", handler)]
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("481526", umo=UMO)))
    assert out == []
    assert called == []


def test_suite_terminate_clears_armed_menu():
    """套件 terminate 必须清掉布防状态（重启/停用后不残留旧菜单）。"""
    plugin = _suite()
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    assert plugin.quick.current(UMO)  # 已布防
    asyncio.run(plugin.terminate())
    assert plugin.quick.current(UMO) == []
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=UMO)))
    assert out == []


def test_out_of_range_digit_reprints_menu():
    plugin = _suite()
    plugin._quick_entries = lambda umo: [("查电费余额", _entries()[0][1])]
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("7", umo=UMO)))
    assert "没有 7" in out[0]
    assert "取消菜单" in out[0]


def test_zero_cancels_menu():
    plugin = _suite()
    plugin._quick_entries = lambda umo: [("查电费余额", _entries()[0][1])]
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("0", umo=UMO)))
    assert "取消" in out[0]
    # 取消后再回数字不接管
    out2 = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=UMO)))
    assert out2 == []


def test_menu_filters_disabled_modules_and_renumbers():
    """模块关掉后条目不出现，剩下的重新连号（不留空洞）。"""
    plugin = _suite()
    plugin._module_enabled = lambda name, umo=None: name == "electric"

    async def h(event):
        return "ok"

    class _Core:
        def quick_actions(self):
            return [("条目", h)]

    plugin.electric = _Core()
    plugin.games = _Core()
    plugin.memo = _Core()
    out = _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    text = out[0]
    assert text.count("\n1.") == 1
    assert "2." not in text


def test_action_exception_is_caught():
    """动作抛异常不能让整个监听器炸掉——回一句失败提示即可。"""

    async def boom(event):
        raise RuntimeError("炸了")

    plugin = _suite()
    plugin._quick_entries = lambda umo: [("会炸的", boom)]
    _collect(plugin.cmd_menu(FakeEvent("/菜单", umo=UMO)))
    out = _collect(plugin.on_quick_digit_reply(FakeEvent("1", umo=UMO)))
    assert "失败" in out[0]


# ================= Core 直连 =================


def test_quick_core_direct_arm_and_take():
    """直接用 Core（不经 main.py）也能跑通——模块自身可独立测试。"""
    plugin = make_quick_plugin()

    async def handler(event):
        return "done"

    plugin.arm(UMO, "u1", [("条目", handler)])
    pick = plugin.take(UMO, "u1", "1")
    assert pick.kind == "run"
    assert asyncio.run(pick.item.handler(None)) == "done"


def test_terminate_clears_state():
    plugin = make_quick_plugin()
    plugin.arm(UMO, "u1", _entries())
    plugin.terminate()
    assert plugin.take(UMO, "u1", "1") is None


def test_quick_core_importable_without_astrbot():
    """quick 是纯逻辑模块：不 import astrbot，宿主外也能测。"""
    import astrbot_plugin_suite.modules.quick.service as svc

    assert "astrbot" not in svc.__dict__
    assert isinstance(QuickCore, type)
