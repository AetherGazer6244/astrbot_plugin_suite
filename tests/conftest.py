"""测试环境准备：注入 astrbot 桩模块，使套件 main.py 与各模块 service.py 可在宿主外导入测试。

桩只覆盖顶层 import 所需的最小表面（装饰器按注册名挂元数据标记，供注册面
冒烟测试使用），被测逻辑（providers/storage/formatting 等纯模块）均为真实实现。

合并自 electric / games 两份旧 conftest（桩接口本就同构）：
- make_plugin → ElectricCore（electric 工厂保留原名）；make_games_plugin → GamesCore
- 工厂内 config 用 SectionConfig 包裹（测试仍传扁平 dict，语义不变）
- games 的假事件/假源/FakeProvider 等与 tools/smoke.py 共用同一份替身
"""

import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # astrbot_plugin_suite 的父目录
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class StubAstrBotConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def save_config(self):
        return None


def _wrapped_config(config, section: str):
    """测试传扁平 config dict → 包成嵌套根再套 SectionConfig 视图。

    兼容直接传嵌套根（含同名分组）的用法。
    """
    cfg = StubAstrBotConfig(config or {})
    if not isinstance(cfg.get(section), dict):
        cfg = StubAstrBotConfig({section: dict(config or {}), "modules": {}})
    from astrbot_plugin_suite.modules import config_view

    return config_view.SectionConfig(cfg, section)


def make_plugin(config=None, store=None, hjnu=None, context=None, scheduler=None):
    """electric 模块统一脚手架：__new__ 绕过 __init__ 后补全全部实例属性。

    AGENTS.md 记录过两次「某测试文件的 _plugin() 工厂漏补新属性 →
    AttributeError」的坑——根因是每个测试文件手搓一份脚手架、加字段时
    漏改。以后 Core 加新字段，只改这里；各测试文件的 _plugin() 一律
    薄封装本工厂。
    """
    from astrbot_plugin_suite.modules.electric import service as _service

    plugin = _service.ElectricCore.__new__(_service.ElectricCore)
    plugin.context = context
    plugin.config = _wrapped_config(config, "electric")
    plugin.store = store
    plugin.hjnu = hjnu
    plugin.scheduler = scheduler
    plugin._wizard = {}
    plugin._tasks = []
    plugin._events = {}
    plugin._pending_alerts = {}
    plugin._last_raw = {}
    plugin._bind_tokens = {}
    plugin._lookup_cache = {}
    plugin._last_room = {}
    plugin._alert_muted = {}
    return plugin


def make_games_plugin(config=None, store=None, sources=None, bili=None, context=None, scheduler=None):
    """games 模块统一脚手架（原 make_plugin，套件里改名避免与电费工厂撞名）。"""
    from astrbot_plugin_suite.modules.games import service as _service

    plugin = _service.GamesCore.__new__(_service.GamesCore)
    plugin.context = context
    plugin.config = _wrapped_config(config, "games")
    plugin.store = store
    plugin.sources = sources if sources is not None else {}
    plugin.bili = bili
    plugin.scheduler = scheduler
    plugin._tasks = []
    plugin._events = []
    plugin._digest_cache = {}
    plugin._digest_tasks = {}
    plugin._failure_backoff = {}
    return plugin


def make_memo_plugin(config=None, store=None, context=None, scheduler=None):
    """memo 模块统一脚手架（同 make_plugin 惯例：__new__ 绕过 __init__ 补全字段）。"""
    from astrbot_plugin_suite.modules.memo import service as _service

    plugin = _service.MemoCore.__new__(_service.MemoCore)
    plugin.context = context
    plugin.config = _wrapped_config(config, "memo")
    plugin.store = store
    plugin.scheduler = scheduler
    plugin._tasks = []
    return plugin


def load_fixture(name: str) -> dict:
    """读取 tests/fixtures 下的真实抓取样本。"""
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# 假数据源 / 假事件 / 假上下文：测试与 tools/smoke.py 共用一份，
# 保证「单测里验的」和「真跑里跑的」是同一套替身。

GROUP_UMO = "aiocqhttp:GroupMessage:12345"
PRIVATE_UMO = "aiocqhttp:FriendMessage:999"


class FakeEvent:
    """最小事件替身：记录 plain_result 的输出，供断言「用户看到什么」。"""

    def __init__(self, text="游戏", umo=PRIVATE_UMO, private=True, admin=True):
        self.message_str = text
        self.unified_msg_origin = umo
        self._private = private
        self._admin = admin
        self.result = None

    def get_message_str(self):
        return self.message_str

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        self.result = text
        return text


class FakeContext:
    """记录所有推送给 (session, text)，ok=False 可模拟发送失败。

    provider 非空时通过 get_using_provider 暴露，供 AI 摘要路径使用。
    """

    def __init__(self, ok=True, provider=None):
        self.ok = ok
        self.provider = provider
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain.get_plain_text()))
        return self.ok

    def get_using_provider(self, umo=None):
        return self.provider


class FakeProvider:
    """宿主对话模型替身：text_chat(prompt=...) → completion_text。

    text 是「模型要回的摘要」；error 非空模拟 LLM 故障；delay 模拟慢模型（秒）。
    calls 记录每次收到的 prompt，供断言「喂给模型的里有没有 URL」与调用次数。
    """

    def __init__(self, text="- 要点一\n- 要点二", error=None, delay=0.0):
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[str] = []

    async def text_chat(self, prompt=None, **kwargs):
        import asyncio as _asyncio

        self.calls.append(str(prompt or ""))
        if self.delay:
            await _asyncio.sleep(self.delay)
        if isinstance(self.error, Exception):
            raise self.error

        class _Resp:
            completion_text = self.text

        return _Resp()


class FakeSource:
    """官网数据源替身。error 非空时模拟数据源故障。"""

    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.calls: list[tuple] = []

    async def latest(self, limit=10):
        self.calls.append(("latest", limit))
        if self.error:
            raise self.error
        return list(self.items)

    async def by_category(self, category, limit=20):
        self.calls.append((category, limit))
        if self.error:
            raise self.error
        return list(self.items)

    async def close(self):
        self.closed = True


class FakeLogin:
    """B站扫码登录替身。gen/poll 传异常即模拟接口故障。"""

    def __init__(self, gen=("k1", "https://qr"), poll_result=(86101, "")):
        self._gen = gen
        self._poll = poll_result
        self.gen_calls = 0

    async def generate(self):
        self.gen_calls += 1
        if isinstance(self._gen, Exception):
            raise self._gen
        return self._gen

    async def poll(self, key):
        if isinstance(self._poll, Exception):
            raise self._poll
        return self._poll


class FakeDynamics:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    async def space_feed(self, uid, game, limit=10):
        if self.error:
            raise self.error
        return list(self.items)


class FakeBili:
    def __init__(self, dyns=None, login=None, dyn_error=None):
        self.dynamics = FakeDynamics(dyns, dyn_error)
        self.login = login or FakeLogin()
        self.sessdata = ""
        self.closed = False

    def set_sessdata(self, value):
        self.sessdata = value

    async def close(self):
        self.closed = True


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    api.AstrBotConfig = StubAstrBotConfig
    api.logger = logging.getLogger("astrbot.stub")

    api_event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        """最小事件桩：各测试按需补属性（is_private_chat / get_sender_id 等）。"""

    class MessageChain:
        def __init__(self):
            self.chain: list[str] = []

        def message(self, text):
            self.chain.append(text)
            return self

        def get_plain_text(self):
            return "".join(str(c) for c in self.chain)

    class _CmdGroup:
        """@filter.command_group 的返回物。组内 .command 只有电费在用 → dorm 标记。"""

        def command(self, name, *args, **kwargs):
            def deco(func):
                func.__dorm_command__ = (name, set(kwargs.get("alias") or ()))
                return func

            return deco

    class _EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    def _passthrough(*args, **kwargs):
        def deco(func):
            return func

        return deco

    def _plain_command_kind(name):
        """裸指令按注册名归模块（套件 main.py 里三类裸指令共用 filter.command）。"""
        if name == "游戏":
            return "games"
        if name == "备忘":
            return "memo"
        if name == "套件":
            return "suite"
        return "dorm"

    def _llm_tool_kind(name):
        for prefix, kind in (
            ("dorm_electric_", "dorm"),
            ("games_", "games"),
            ("memo_", "memo"),
        ):
            if str(name or "").startswith(prefix):
                return kind
        return "dorm"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def command_group(*args, **kwargs):
            def deco(func):
                return _CmdGroup()

            return deco

        @staticmethod
        def command(name=None, *args, **kwargs):
            # 挂元数据标记：注册面冒烟测试靠它断言「指令装饰器必须落在
            # cmd_* 函数上」这类结构性错误（dorm v1.1.5 曾踩过）。
            def deco(func):
                kind = _plain_command_kind(name)
                setattr(func, f"__{kind}_command__", (name, set(kwargs.get("alias") or ())))
                return func

            return deco

        # 事件级监听器在宿主里由元数据登记，测试只需透传装饰器本身。
        event_message_type = staticmethod(_passthrough)
        on_llm_request = staticmethod(_passthrough)

        @staticmethod
        def llm_tool(name=None, **kwargs):
            # 同样挂标记：冒烟测试断言每个工具 docstring 可被宿主解析
            # （含 Args: 段）、参数都有默认值（宿主生成的 schema 没有 required）。
            def deco(func):
                kind = _llm_tool_kind(name)
                setattr(func, f"__{kind}_llm_tool__", name or func.__name__)
                return func

            return deco

    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.MessageChain = MessageChain
    api_event.filter = _Filter()

    api_star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register

    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: "data"
    path_mod.get_astrbot_config_path = lambda: "data/config"

    astrbot.api = api
    astrbot.api.event = api_event
    astrbot.api.star = api_star
    astrbot.core = core
    core.utils = utils
    utils.astrbot_path = path_mod

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
        ("astrbot.core", core),
        ("astrbot.core.utils", utils),
        ("astrbot.core.utils.astrbot_path", path_mod),
    ):
        sys.modules[name] = mod


_install_stubs()
