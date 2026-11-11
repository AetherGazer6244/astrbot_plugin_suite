"""套件装配面测试：包装器转发、模块开关（全局 + 配置档覆盖）、/套件 指令流。

包装器是套件的注册面（宿主按定义模块归属 handler，装饰器全部在 SuitePlugin
上）；这里用实例属性替换 Core 方法做哨兵，验证：
- 转发参数逐个透传（event 在首位的形态不变形）
- 全局开关 / profiles.json 档内覆盖的拦截语义
- 指令被拦时给用户文案；监听器/hint 被拦时静默
"""

import asyncio
import json

import astrbot_plugin_suite.main as suite_main
from astrbot_plugin_suite.main import SuitePlugin
from conftest import FakeEvent, StubAstrBotConfig

ELECTRIC_DISABLED = "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
GAMES_DISABLED = "游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
MEMO_DISABLED = "备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。"


# ================= 脚手架 =================


def _suite_config(modules=None, **sections):
    data = {"modules": dict(modules or {}), "electric": {}, "games": {}, "memo": {}}
    for k, v in sections.items():
        data[k] = v
    return StubAstrBotConfig(data)


class _CountingConfig(StubAstrBotConfig):
    def __init__(self, data=None):
        super().__init__(data or {})
        self.saved = 0

    def save_config(self):
        self.saved += 1


class _Mgr:
    def __init__(self, info=None, listing=None):
        self._info = info or {}
        self._listing = listing or []

    def get_conf_info(self, umo):
        return self._info

    def get_conf_list(self):
        return self._listing


class _MgrContext:
    def __init__(self, mgr=None):
        self.astrbot_config_mgr = mgr


def _suite(config=None, context=None, data_dir=None):
    if data_dir is not None:
        orig = suite_main.get_astrbot_data_path
        suite_main.get_astrbot_data_path = lambda: str(data_dir)
        try:
            plugin = SuitePlugin(context or _MgrContext(), config or _suite_config())
        finally:
            suite_main.get_astrbot_data_path = orig
    else:
        plugin = SuitePlugin(context or _MgrContext(), config or _suite_config())
    return plugin


def _stub_gen(name, calls):
    async def g(event, *a, **k):
        calls.append((name, event, a, k))
        yield f"gen:{name}"

    return g


def _stub_coro(name, calls):
    async def c(event, *a, **k):
        calls.append((name, event, a, k))
        return f"coro:{name}"

    return c


def _collect(agen):
    async def run():
        return [r async for r in agen]

    return asyncio.run(run())


# ================= 包装器转发 =================


def test_command_wrapper_forwards_to_core_with_event():
    calls = []
    plugin = _suite()
    plugin.electric.cmd_status = _stub_gen("cmd_status", calls)
    ev = FakeEvent("/电费 状态")
    out = _collect(plugin.cmd_status(ev))
    assert out == ["gen:cmd_status"]
    assert calls == [("cmd_status", ev, (), {})]


def test_tool_wrapper_forwards_and_returns():
    calls = []
    plugin = _suite()
    plugin.electric.tool_dorm_electric_balance = _stub_coro("balance", calls)
    ev = FakeEvent("电费还剩多少")
    res = asyncio.run(plugin.tool_dorm_electric_balance(ev, 7))
    assert res == "coro:balance"
    assert calls == [("balance", ev, (7,), {})]


def test_tool_wrapper_kwargs_forwarded():
    calls = []
    plugin = _suite()
    plugin.electric.tool_dorm_electric_pick = _stub_coro("pick", calls)
    res = asyncio.run(plugin.tool_dorm_electric_pick(FakeEvent(), index=2, page=1))
    assert res == "coro:pick"
    # 包装器按位置转发（event 在首），工具签名默认值仍在
    assert calls[0][2] == (2, 1)
    assert calls[0][3] == {}


def test_games_command_and_tools_forward():
    calls = []
    plugin = _suite()
    plugin.games.cmd_games = _stub_gen("cmd_games", calls)
    plugin.games.tool_games_news = _stub_coro("news", calls)
    ev = FakeEvent("/游戏")
    out = _collect(plugin.cmd_games(ev))
    res = asyncio.run(plugin.tool_games_news(ev, "1999"))
    assert out == ["gen:cmd_games"]
    assert res == "coro:news"
    assert [c[0] for c in calls] == ["cmd_games", "news"]
    assert all(c[1] is ev for c in calls)


def test_memo_command_and_tools_forward():
    calls = []
    plugin = _suite()
    plugin.memo.cmd_memo = _stub_gen("cmd_memo", calls)
    plugin.memo.tool_memo_add = _stub_coro("add", calls)
    plugin.memo.tool_memo_list = _stub_coro("list", calls)
    plugin.memo.tool_memo_complete = _stub_coro("complete", calls)
    ev = FakeEvent("/备忘")
    assert _collect(plugin.cmd_memo(ev)) == ["gen:cmd_memo"]
    assert asyncio.run(plugin.tool_memo_add(ev, "交作业", "明天 8:00")) == "coro:add"
    assert asyncio.run(plugin.tool_memo_list(ev)) == "coro:list"
    assert asyncio.run(plugin.tool_memo_complete(ev, "3")) == "coro:complete"
    assert [c[0] for c in calls] == ["cmd_memo", "add", "list", "complete"]
    assert all(c[1] is ev for c in calls)


def test_memo_global_switch():
    calls = []
    plugin = _suite(_suite_config(modules={"memo": False}))
    plugin.memo.cmd_memo = _stub_gen("cmd_memo", calls)
    plugin.memo.tool_memo_add = _stub_coro("add", calls)
    plugin.memo.inject_memo_hint = _stub_coro("hint", calls)
    assert _collect(plugin.cmd_memo(FakeEvent())) == [MEMO_DISABLED]
    assert asyncio.run(plugin.tool_memo_add(FakeEvent(), "x")) == MEMO_DISABLED
    assert asyncio.run(plugin.inject_memo_hint(FakeEvent(), req=object())) is None
    assert calls == [], "被关闭模块的 Core 方法不许被调"


# ================= 全局开关 =================


def test_global_switch_blocks_command_with_text():
    calls = []
    plugin = _suite(_suite_config(modules={"electric": False}))
    plugin.electric.cmd_status = _stub_gen("cmd_status", calls)
    out = _collect(plugin.cmd_status(FakeEvent()))
    assert out == [ELECTRIC_DISABLED]
    assert calls == [], "被关闭模块的 Core 方法不许被调"


def test_global_switch_blocks_tool_and_listeners_are_silent():
    calls = []
    plugin = _suite(_suite_config(modules={"electric": False}))
    plugin.electric.tool_dorm_electric_check = _stub_coro("check", calls)
    plugin.electric.on_user_replied_code = _stub_coro("listener", calls)
    plugin.electric.inject_electric_hint = _stub_coro("hint", calls)
    assert asyncio.run(plugin.tool_dorm_electric_check(FakeEvent())) == ELECTRIC_DISABLED
    assert asyncio.run(plugin.on_user_replied_code(FakeEvent())) is None
    assert asyncio.run(plugin.inject_electric_hint(FakeEvent(), req=object())) is None
    assert calls == [], "监听器/hint 被禁时必须静默，不许发文案"


def test_games_global_switch():
    plugin = _suite(_suite_config(modules={"games": False}))
    plugin.games.tool_games_codes = _stub_coro("codes", [])
    assert asyncio.run(plugin.tool_games_codes(FakeEvent())) == GAMES_DISABLED


# ================= 配置档覆盖 =================


def _write_profiles(data_dir, pid, override):
    pdir = data_dir / "plugin_data" / "astrbot_plugin_suite"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profiles.json").write_text(
        json.dumps({pid: override}, ensure_ascii=False), encoding="utf-8"
    )


def test_profile_override_disables_games_only(tmp_path):
    calls = []
    _write_profiles(tmp_path, "aaa-guid", {"games": False})
    ctx = _MgrContext(_Mgr(info={"id": "aaa-guid"}))
    plugin = _suite(context=ctx, data_dir=tmp_path)
    plugin.games.cmd_games = _stub_gen("cmd_games", calls)
    plugin.electric.cmd_help = _stub_gen("cmd_help", calls)
    assert _collect(plugin.cmd_games(FakeEvent())) == [GAMES_DISABLED]
    assert _collect(plugin.cmd_help(FakeEvent())) == ["gen:cmd_help"]
    assert [c[0] for c in calls] == ["cmd_help"], "只有 games 被档内覆盖关闭"


def test_unknown_profile_id_follows_global(tmp_path):
    _write_profiles(tmp_path, "aaa-guid", {"games": False})
    ctx = _MgrContext(_Mgr(info={"id": "other-guid"}))
    plugin = _suite(context=ctx, data_dir=tmp_path)
    plugin.games.cmd_games = _stub_gen("cmd_games", [])
    assert _collect(plugin.cmd_games(FakeEvent())) == ["gen:cmd_games"]


def test_no_config_mgr_api_falls_back_to_global(tmp_path):
    _write_profiles(tmp_path, "aaa-guid", {"games": False})
    plugin = _suite(context=object(), data_dir=tmp_path)  # context 无 astrbot_config_mgr
    plugin.games.cmd_games = _stub_gen("cmd_games", [])
    assert _collect(plugin.cmd_games(FakeEvent())) == ["gen:cmd_games"]


# ================= /套件 指令流 =================


def test_suite_status_lists_all_modules():
    plugin = _suite()
    out = _collect(plugin.cmd_suite(FakeEvent("/套件")))
    text = out[0]
    assert "宿舍电费(electric)：全局 开" in text
    assert "游戏资讯(games)：全局 开" in text
    assert "备忘录(memo)：全局 开" in text
    assert "跟随全局" in text
    assert "/套件 模块" in text and "/套件 档案" in text


def test_suite_status_shows_profile_override(tmp_path):
    _write_profiles(tmp_path, "aaa-guid", {"games": False})
    ctx = _MgrContext(_Mgr(info={"id": "aaa-guid"}))
    plugin = _suite(context=ctx, data_dir=tmp_path)
    out = _collect(plugin.cmd_suite(FakeEvent("/套件")))
    assert "游戏资讯(games)：全局 开；本档 关" in out[0]
    assert "宿舍电费(electric)：全局 开；本档 跟随全局" in out[0]


def test_suite_set_global_toggles_and_saves():
    cfg = _CountingConfig(_suite_config())
    plugin = _suite(config=cfg)
    ev = FakeEvent("/套件 模块 电费 关", admin=True)
    out = _collect(plugin.cmd_suite(ev))
    assert "宿舍电费模块已全局关" in out[0]
    assert cfg["modules"]["electric"] is False
    assert cfg.saved >= 1
    # 拦截立即生效
    plugin.electric.cmd_status = _stub_gen("cmd_status", [])
    assert _collect(plugin.cmd_status(FakeEvent())) == [ELECTRIC_DISABLED]


def test_suite_set_global_requires_admin():
    cfg = _CountingConfig(_suite_config())
    plugin = _suite(config=cfg)
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 模块 电费 关", admin=False)))
    assert "管理员" in out[0]
    assert cfg["modules"].get("electric") is not False
    assert cfg.saved == 0


def test_suite_set_global_alias_and_invalid_module():
    cfg = _CountingConfig(_suite_config())
    plugin = _suite(config=cfg)
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 模块 游戏 关", admin=True)))
    assert "游戏资讯模块已全局关" in out[0]
    assert cfg["modules"]["games"] is False
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 模块 备忘 关", admin=True)))
    assert "备忘录模块已全局关" in out[0]
    assert cfg["modules"]["memo"] is False
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 模块 微博 关", admin=True)))
    assert "没认出模块" in out[0]


def test_suite_profile_set_and_reset(tmp_path):
    listing = [{"id": "aaa-guid", "name": "AAA"}]
    ctx = _MgrContext(_Mgr(listing=listing))
    plugin = _suite(context=ctx, data_dir=tmp_path)
    ev = FakeEvent("/套件 档案 AAA 游戏 关", admin=True)
    out = _collect(plugin.cmd_suite(ev))
    assert "已关" in out[0]
    saved = json.loads(
        (tmp_path / "plugin_data" / "astrbot_plugin_suite" / "profiles.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved == {"aaa-guid": {"games": False}}

    # 新实例（避开 mtime 缓存）→ 恢复默认
    plugin2 = _suite(context=ctx, data_dir=tmp_path)
    out = _collect(plugin2.cmd_suite(FakeEvent("/套件 档案 AAA 游戏 默认", admin=True)))
    assert "恢复跟随全局" in out[0]
    saved = json.loads(
        (tmp_path / "plugin_data" / "astrbot_plugin_suite" / "profiles.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved == {}, "覆盖全空后条目应删除"


def test_suite_profile_unknown_name():
    ctx = _MgrContext(_Mgr(listing=[{"id": "x", "name": "default"}]))
    plugin = _suite(context=ctx)
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 档案 不存在的档 游戏 关", admin=True)))
    assert "没找到配置档" in out[0]


def test_suite_profile_requires_admin(tmp_path):
    ctx = _MgrContext(_Mgr(listing=[{"id": "x", "name": "AAA"}]))
    plugin = _suite(context=ctx, data_dir=tmp_path)
    out = _collect(plugin.cmd_suite(FakeEvent("/套件 档案 AAA 游戏 关", admin=False)))
    assert "管理员" in out[0]
    assert not (tmp_path / "plugin_data" / "astrbot_plugin_suite" / "profiles.json").exists()


# ================= 生命周期 =================


def test_initialize_runs_migration_and_skips_disabled_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(suite_main, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(
        suite_main, "get_astrbot_config_path", lambda: str(tmp_path / "config")
    )
    (tmp_path / "config").mkdir()
    # 旧配置文件在场 → initialize 应完成迁移
    (tmp_path / "config" / "astrbot_plugin_dorm_electric_config.json").write_text(
        json.dumps({"hjnu_cookie": "legacy"}), encoding="utf-8"
    )
    cfg = _CountingConfig(_suite_config(modules={"games": False}))
    plugin = _suite(config=cfg, data_dir=tmp_path)
    calls = []

    async def electric_init():
        calls.append("electric")

    async def games_init():
        calls.append("games")

    async def memo_init():
        calls.append("memo")

    async def electric_term():
        calls.append("electric:term")

    async def games_term():
        calls.append("games:term")

    async def memo_term():
        calls.append("memo:term")

    plugin.electric.initialize = electric_init
    plugin.games.initialize = games_init
    plugin.memo.initialize = memo_init
    plugin.electric.terminate = electric_term
    plugin.games.terminate = games_term
    plugin.memo.terminate = memo_term

    asyncio.run(plugin.initialize())
    assert calls == ["electric", "memo"], "关闭的模块不许初始化"
    assert cfg["electric"]["hjnu_cookie"] == "legacy", "迁移应在 initialize 里发生"
    assert cfg.saved >= 1

    asyncio.run(plugin.terminate())
    assert calls == [
        "electric",
        "memo",
        "electric:term",
        "games:term",
        "memo:term",
    ], (
        "terminate 必须连已关闭模块也清理（构造时三者都存在）"
    )


def test_migrated_data_file_is_visible_to_core(tmp_path):
    """数据迁移后，Core 用套件路径能读到旧 history.json。"""
    src = tmp_path / "plugin_data" / "astrbot_plugin_dorm_electric" / "history.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"bindings": {"qq:private:1": {"room_label": "旧"}}}', encoding="utf-8")
    notes = suite_main.migrate.migrate_data(tmp_path)
    assert notes
    dst = tmp_path / "plugin_data" / "astrbot_plugin_suite" / "electric" / "history.json"
    assert dst.is_file()
    assert json.loads(dst.read_text(encoding="utf-8"))["bindings"]["qq:private:1"]


def test_suite_data_dir_layout():
    plugin = _suite()
    assert plugin._data_dir.name == "astrbot_plugin_suite"
    assert plugin._profiles_path().name == "profiles.json"
