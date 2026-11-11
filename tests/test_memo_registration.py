"""memo 模块注册面守卫：3 个工具 + 1 条裸指令，锁死清单与形态。"""

import inspect

from astrbot_plugin_suite import main

_PLUGIN_CLS = main.SuitePlugin


def _members():
    return vars(_PLUGIN_CLS).items()


def test_memo_tools_locked():
    tools = sorted(
        m.__memo_llm_tool__ for _n, m in _members() if hasattr(m, "__memo_llm_tool__")
    )
    assert tools == ["memo_add", "memo_complete", "memo_list"], (
        f"memo 工具集漂移：{tools}"
    )


def test_cmd_memo_registration_locked():
    meta = getattr(main.SuitePlugin.cmd_memo, "__memo_command__", None)
    assert meta, "cmd_memo 缺少 @filter.command 注册，它永远不会被触发"
    cmd_name, alias = meta
    assert cmd_name == "备忘"
    assert alias == {"待办", "todo", "memo"}, f"/备忘 别名漂移：{alias}"


def test_memo_command_decorator_sits_on_cmd_named_function():
    for name, member in _members():
        if hasattr(member, "__memo_command__"):
            assert name.startswith("cmd_"), (
                f"@filter.command 挂在了非 cmd_* 函数 {name} 上——"
                "宿主会把 event 当参数传进去，指令静默失效"
            )


def test_memo_tools_return_str_not_yield():
    for name, member in _members():
        if hasattr(member, "__memo_llm_tool__"):
            assert inspect.isasyncgenfunction(getattr(member, "__func__", member)) is False, (
                f"{name} 是异步生成器，应改成 return（yield plain_result 模型收不到）"
            )


def test_memo_tool_args_section_matches_signature():
    for name, member in _members():
        if not hasattr(member, "__memo_llm_tool__"):
            continue
        func = getattr(member, "__func__", member)
        params = [
            p for p in inspect.signature(func).parameters if p not in ("self", "event")
        ]
        if not params:
            continue
        doc = inspect.getdoc(func) or ""
        assert "Args:" in doc, f"{name} 有参数但 docstring 缺 Args: 段"
        for pname, p in inspect.signature(func).parameters.items():
            if pname in ("self", "event"):
                continue
            assert p.default is not inspect.Parameter.empty, (
                f"{name} 参数 {pname} 缺默认值（宿主 schema 无 required）"
            )
