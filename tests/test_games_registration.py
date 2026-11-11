"""注册面冒烟测试（游戏模块）：宿主集成层的结构性守卫。

套件布局下装饰器全部物理写在仓库根 main.py 的 SuitePlugin 上；本文件锁死
games 侧的结构不变式。

另有一条硬不变式：**不能**用 @filter.command_group 注册「游戏」——AstrBot
v4.27 的指令组在只发组名时会抛 ValueError 并打印指令树，且组内没有「默认
子指令」（@group.command("") 注册出的名字是 "游戏 "，永远匹配不上），而
/游戏 无参必须返回两游戏最新资讯。
"""

import ast
import inspect

from astrbot_plugin_suite import main

_PLUGIN_CLS = main.SuitePlugin

# 用户可见的 7 条行为（v1.1.0：默认视图改为 AI 要点，原始列表降为 /游戏 列表）
EXPECTED_BEHAVIOURS = ("帮助", "资讯要点", "列表", "卡池", "兑换码", "版本", "订阅")


def _members():
    return vars(_PLUGIN_CLS).items()


def test_command_decorator_sits_on_cmd_named_function():
    for name, member in _members():
        if hasattr(member, "__games_command__"):
            assert name.startswith("cmd_"), (
                f"@filter.command 挂在了非 cmd_* 函数 {name} 上——"
                "宿主会把 event 当参数传进去，指令静默失效"
            )


def test_cmd_games_carries_command_metadata():
    meta = getattr(main.SuitePlugin.cmd_games, "__games_command__", None)
    assert meta, "cmd_games 缺少 @filter.command 注册，它永远不会被触发"
    cmd_name, alias = meta
    assert cmd_name == "游戏"
    assert alias == {"games", "游戏资讯"}, f"/游戏 别名漂移：{alias}"


def test_exactly_one_games_command_handler():
    """裸指令 + 内部路由：games 侧只应有一个 @filter.command。

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


def test_no_command_group_registered_for_games():
    """红线：指令组会让 /游戏 无参失效（见模块 docstring）。

    套件 main.py 里电费的 command_group("电费") 是合法的；只拦「游戏」。
    用 AST 查装饰器而不是 grep 源码——套件 docstring 里可能解释这个决定，
    文本匹配会误伤自己。
    """
    tree = ast.parse(inspect.getsource(main))
    used = {
        attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        for attr in [node.attr]
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "command_group" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and "游戏" in str(first.value):
                raise AssertionError(
                    "游戏不能用 command_group 注册：只发组名时 AstrBot 会抛 "
                    "ValueError 打印指令树，而 /游戏 无参必须返回两游戏最新资讯"
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
    from astrbot_plugin_suite.modules.games import formatting

    text = formatting.help_text(["re1999", "arknights"], 10, 15, False)
    for behaviour in EXPECTED_BEHAVIOURS:
        assert behaviour in text, f"帮助文案缺了「{behaviour}」"
