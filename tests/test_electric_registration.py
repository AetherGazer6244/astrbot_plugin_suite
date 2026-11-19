"""注册面冒烟测试（电费模块）：宿主集成层的结构性守卫。

套件布局下装饰器全部物理写在仓库根 main.py 的 SuitePlugin 上（宿主按
「装饰器函数的定义模块」归属 handler），modules/electric/service.py 只保留
无装饰器的方法体。本文件锁死电费侧的结构不变式：
- 16 条指令装饰器必须落在 cmd_* 函数上（v1.1.5 事故红线）
- 10 个 dorm_electric_* 工具必须有 Args: 段、参数有默认值、名字带插件前缀
"""

import asyncio
import inspect

from astrbot_plugin_suite import main

_PLUGIN_CLS = main.SuitePlugin

# 电费 16 条指令（v1.1.6 起）；套件里它们经 @electric.command 注册 → dorm 标记
EXPECTED_DORM_COMMANDS = {
    "帮助", "状态", "检查", "查询", "凭证", "历史", "日志",
    "绑定", "确认", "静音", "预警线", "校区", "楼栋", "楼层", "房间", "解绑",
}

# 10 个工具注册名（v1.1.7 起）
EXPECTED_DORM_TOOLS = {
    "dorm_electric_browse", "dorm_electric_pick", "dorm_electric_bind_room",
    "dorm_electric_unbind", "dorm_electric_confirm", "dorm_electric_balance",
    "dorm_electric_mute_alerts", "dorm_electric_set_alert_threshold",
    "dorm_electric_query_room", "dorm_electric_check",
}


def _members():
    return vars(_PLUGIN_CLS).items()


def test_every_command_decorator_sits_on_cmd_named_function():
    for name, member in _members():
        if hasattr(member, "__dorm_command__"):
            assert name.startswith("cmd_"), (
                f"@electric.command 挂在了非 cmd_* 函数 {name} 上——"
                "宿主会把 event 当参数传进去，指令静默失效"
                "（参见 v1.1.5 的 /电费 检查 事故）"
            )


def test_every_cmd_function_carries_command_metadata():
    """套件里 cmd_* 家族 = 电费 16 条 + /游戏 + /套件，都必须带指令标记。"""
    kinds = (
        "__dorm_command__",
        "__games_command__",
        "__memo_command__",
        "__suite_command__",
        "__quick_command__",
    )
    for name, member in _members():
        if name.startswith("cmd_") and asyncio.iscoroutinefunction(
            getattr(member, "__func__", member)
        ):
            assert any(hasattr(member, k) for k in kinds), (
                f"cmd_* 函数 {name} 缺少任何指令注册装饰器——"
                "它在宿主里永远不会被触发"
            )


def test_dorm_command_names_locked():
    names = {
        m.__dorm_command__[0] for _n, m in _members() if hasattr(m, "__dorm_command__")
    }
    assert names == EXPECTED_DORM_COMMANDS, (
        f"电费指令集漂移：多出 {names - EXPECTED_DORM_COMMANDS}，"
        f"缺失 {EXPECTED_DORM_COMMANDS - names}"
    )


def test_command_names_and_aliases_nonempty():
    for name, member in _members():
        meta = getattr(member, "__dorm_command__", None)
        if meta:
            cmd_name, alias = meta
            assert cmd_name and str(cmd_name).strip(), f"{name} 的指令名为空"
            assert all(a and str(a).strip() for a in alias), f"{name} 存在空别名"


def test_dorm_llm_tool_names_locked():
    tools = {
        m.__dorm_llm_tool__ for _n, m in _members() if hasattr(m, "__dorm_llm_tool__")
    }
    assert tools == EXPECTED_DORM_TOOLS, (
        f"电费工具集漂移：多出 {tools - EXPECTED_DORM_TOOLS}，"
        f"缺失 {EXPECTED_DORM_TOOLS - tools}"
    )


def test_every_llm_tool_has_args_section():
    for name, member in _members():
        if hasattr(member, "__dorm_llm_tool__"):
            doc = inspect.getdoc(getattr(member, "__func__", member)) or ""
            params = [
                p
                for p in inspect.signature(
                    getattr(member, "__func__", member)
                ).parameters
                if p not in ("self", "event")
            ]
            if params:
                assert "Args:" in doc, (
                    f"LLM 工具 {name} 有参数但 docstring 缺 Args: 段——"
                    "宿主只从 Args: 段解析 schema，参数不会暴露给模型"
                )


def test_every_llm_tool_param_has_default():
    for name, member in _members():
        if hasattr(member, "__dorm_llm_tool__"):
            for pname, p in (
                inspect.signature(getattr(member, "__func__", member))
                .parameters.items()
            ):
                if pname in ("self", "event"):
                    continue
                assert p.default is not inspect.Parameter.empty, (
                    f"LLM 工具 {name} 的参数 {pname} 缺默认值——宿主生成的 "
                    "schema 没有 required，模型裸调时会 TypeError"
                )


def test_llm_tool_names_use_plugin_prefix():
    for name, member in _members():
        tool = getattr(member, "__dorm_llm_tool__", None)
        if tool:
            assert str(tool).startswith("dorm_electric_"), (
                f"工具 {name} 的注册名 {tool} 不在 dorm_electric_ 前缀内"
            )
