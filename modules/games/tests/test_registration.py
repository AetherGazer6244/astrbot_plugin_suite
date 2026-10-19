"""注册面冒烟测试：宿主集成层的结构性守卫。

conftest 的桩把装饰器透传 + 各测试直接调用方法，宿主真实的注册语义在纯单测
环境里是盲区。本文件在桩层挂元数据标记，用不变式拦截结构性错误。

另有一条硬不变式：本插件**不能**用 @filter.command_group 注册「游戏」——
AstrBot v4.27 的指令组在只发组名时会抛 ValueError 并打印指令树，且组内没有
「默认子指令」（@group.command("") 注册出的名字是 "游戏 "，永远匹配不上），
而 /游戏 无参必须返回两游戏最新资讯。
"""

import inspect

from astrbot_plugin_games_monitor import main

_PLUGIN_CLS = main.GamesMonitorPlugin

# 用户可见的 6 条行为（计划书第二节）
EXPECTED_BEHAVIOURS = ("帮助", "最新资讯", "卡池", "兑换码", "版本", "订阅")


def _members():
    return vars(_PLUGIN_CLS).items()


def test_command_decorator_sits_on_cmd_named_function():
    for name, member in _members():
        if hasattr(member, "__games_command__"):
            assert name.startswith("cmd_"), (
                f"@filter.command 挂在了非 cmd_* 函数 {name} 上——"
                "宿主会把 event 当参数传进去，指令静默失效"
            )


def test_cmd_function_carries_command_metadata():
    for name, member in _members():
        if name.startswith("cmd_") and inspect.iscoroutinefunction(
            getattr(member, "__func__", member)
        ):
            assert hasattr(member, "__games_command__"), (
                f"cmd_* 函数 {name} 缺少 @filter.command 注册，它永远不会被触发"
            )


def test_exactly_one_command_handler():
    """裸指令 + 内部路由：只应有一个 @filter.command。

    若误加 command_group 再加裸指令，裸指令会吞掉全部子指令（CommandFilter
    用 startswith("游戏 ") 匹配），整个指令集静默失效。
    """
    cmds = [n for n, m in _members() if hasattr(m, "__games_command__")]
    assert cmds == ["cmd_games"], f"指令处理器数量异常：{cmds}"


def test_command_name_and_aliases_nonempty():
    for name, member in _members():
        meta = getattr(member, "__games_command__", None)
        if meta:
            cmd_name, alias = meta
            assert cmd_name and str(cmd_name).strip(), f"{name} 的指令名为空"
            assert all(a and str(a).strip() for a in alias), f"{name} 存在空别名"


def test_no_command_group_registered():
    """红线：指令组会让 /游戏 无参失效（见模块 docstring）。

    用 AST 查装饰器而不是 grep 源码——模块 docstring 里正好解释了这个决定，
    文本匹配会误伤自己。
    """
    import ast

    tree = ast.parse(inspect.getsource(main))
    used = {
        attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        for attr in [node.attr]
    }
    assert "command_group" not in used, (
        "本插件不能注册指令组：只发组名时 AstrBot 会抛 ValueError 打印指令树，"
        "而 /游戏 无参必须返回两游戏最新资讯"
    )
    assert "command" in used, "应当用裸指令 + 内部路由"


def test_every_llm_tool_has_args_section():
    for name, member in _members():
        if hasattr(member, "__games_llm_tool__"):
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
        if hasattr(member, "__games_llm_tool__"):
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


def test_llm_tool_docstring_args_types_are_supported():
    """宿主的 schema 解析只认 string/number/object/array/boolean。"""
    import re

    allowed = {"string", "number", "object", "array", "boolean"}
    pattern = re.compile(r"^\s*(\w+)\(([^)]*)\)\s*:")
    for name, member in _members():
        if not hasattr(member, "__games_llm_tool__"):
            continue
        doc = inspect.getdoc(getattr(member, "__func__", member)) or ""
        assert "Args:" in doc, f"{name} 缺 Args: 段"
        body = doc.split("Args:", 1)[1]
        params = [
            p
            for p in inspect.signature(getattr(member, "__func__", member)).parameters
            if p not in ("self", "event")
        ]
        seen = []
        for line in body.splitlines():
            m = pattern.match(line)
            if m:
                seen.append(m.group(1))
                assert m.group(2).strip() in allowed, (
                    f"{name} 的参数 {m.group(1)} 注解 {m.group(2)!r} "
                    "不在宿主支持的类型里"
                )
        assert seen == params, f"{name} 的 Args: 段与函数签名不一致：{seen} vs {params}"


def test_llm_tool_names_use_plugin_prefix():
    for name, member in _members():
        tool = getattr(member, "__games_llm_tool__", None)
        if tool:
            assert str(tool).startswith("games_"), (
                f"工具 {name} 的注册名 {tool} 不在 games_ 前缀内"
            )


def test_llm_tools_are_exactly_three():
    tools = sorted(
        m.__games_llm_tool__
        for _n, m in _members()
        if hasattr(m, "__games_llm_tool__")
    )
    assert tools == ["games_codes", "games_dynamics", "games_news"]


def test_llm_tools_return_str_not_yield():
    """工具必须 return 给 LLM；yield plain_result 会让模型收不到结果。"""
    for name, member in _members():
        if not hasattr(member, "__games_llm_tool__"):
            continue
        assert inspect.isasyncgenfunction(
            getattr(member, "__func__", member)
        ) is False, f"{name} 是异步生成器，应改成 return"


def test_llm_tools_do_not_leak_credentials():
    """SESSDATA 出现在任何工具输出里都是安全事故。"""
    for _name, member in _members():
        if hasattr(member, "__games_llm_tool__"):
            doc = inspect.getdoc(getattr(member, "__func__", member)) or ""
            assert "SESSDATA" not in doc


def test_help_lists_every_behaviour():
    from astrbot_plugin_games_monitor import formatting

    text = formatting.help_text(["re1999", "arknights"], 10, 15, False)
    for behaviour in EXPECTED_BEHAVIOURS:
        assert behaviour in text, f"帮助文案缺了「{behaviour}」"
