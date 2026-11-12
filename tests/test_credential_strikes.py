"""凭证失效误报修复：Provider 层阶梯复验 + 连续确认计数 + 时间窗。

背景：学校缴费系统夜里「发癫」时会把**还活着的会话**临时判成 91001
（实录：会话即生即死、几分钟后自愈，整段发癫可达 40 分钟）。旧实现是
单次 91001 就发失效通知 + 置 credential_dead，用户被叫去重新提取凭证
——纯白折腾。

三道防线：
1. Provider 层：91001 按 EXPIRED_RECHECK_DELAYS 阶梯复验（2.5s→8s），
   每次换一条**全新连接**（不复用共享 client）——挡秒级抖动 + 换节点。
   任一级复验通过就当抖动处理。
2. main 层：连续 CREDENTIAL_CONFIRM_STRIKES 次（跨轮询/保活周期）才发通知、
   才置 credential_dead（挡「两次探测凑巧撞上抖动」）。
3. main 层时间窗：连续被拒必须**持续满 N 分钟**（默认 30）才判失效——
   挡「学校连着抖 8 分钟以上」这种整段发癫。没有它的话，保活 8 分钟一次
   就能把计数凑到 2，学校抖 8 分钟就被误判成凭证已失效。
   窗口内若出现 5xx/超时混杂（学校整体故障形态），窗口自动加倍。

网络失败不推进计数；任一路余额取到即清零（含时间窗起点）。
"""

import asyncio
import json

import httpx
from astrbot_plugin_suite.modules.electric.providers import http_json
from astrbot_plugin_suite.modules.electric.providers.base import BalanceResult
from astrbot_plugin_suite.modules.electric.providers.http_json import (
    HjnuProvider,
    SessionExpiredError,
)
from conftest import make_plugin

OK_BALANCE = {"retcode": "0", "errmsg": "A-8-17房间剩余电量94.66度"}
EXPIRED = {"retcode": "91001", "errmsg": "会话已超时"}
ROOM_PARAMS = {
    "aid": "a1",
    "area": {"areaname": "校本部"},
    "building": {"building": "春雪楼2"},
    "floor": {"floor": "8层"},
    "room": {"room": "A-8-17", "roomid": 646},
}
UMO = "qq:private:1"


class _Store:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def save(self, force=False):
        self.saved += 1


def _provider(script, monkeypatch):
    """按脚本回放响应；script 元素：(status, text)。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = script[len(calls)] if len(calls) < len(script) else script[-1]
        calls.append(str(request.url.path))
        status, text = action
        return httpx.Response(status, text=text)

    provider = HjnuProvider(
        base_url="http://school.test",
        query_path="/q",
        cookie="JSESSIONID=x",
        user_agent="ua",
        referer="http://school.test/",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(http_json.asyncio, "sleep", _nosleep)
    return provider, calls


# ================= Provider 层：91001 阶梯复验 =================


def test_query_room_91001_then_ok_is_treated_as_flap(monkeypatch):
    """第一次 91001、复验拿到余额 → 正常返回余额，不报凭证失效。"""
    provider, calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps(OK_BALANCE)),
        ],
        monkeypatch,
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok is True
    assert result.value == 94.66
    assert result.session_expired is False
    assert len(calls) == 2  # 复验确实重发了一次


def test_query_room_91001_then_91001_then_ok_is_treated_as_flap(monkeypatch):
    """阶梯复验：第一级仍 91001、第二级通过 → 照样当抖动（分钟级抖动）。"""
    provider, calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps(EXPIRED)),
            (200, json.dumps(OK_BALANCE)),
        ],
        monkeypatch,
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok is True
    assert result.value == 94.66
    assert result.session_expired is False
    assert len(calls) == 3


def test_query_room_91001_whole_ladder_is_expired(monkeypatch):
    """整条阶梯都 91001 → 真失效（会话已失效）。"""
    provider, calls = _provider(
        [(200, json.dumps(EXPIRED)), (200, json.dumps(EXPIRED))],
        monkeypatch,
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok is False
    assert result.session_expired is True
    # 原请求 + 整条阶梯（2 级）
    assert len(calls) == 1 + len(http_json.EXPIRED_RECHECK_DELAYS)


def test_list_areas_91001_then_ok_is_treated_as_flap(monkeypatch):
    """保活探测走的 list_areas 同样带复验（抖动不误判失效）。"""
    provider, calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps({"retcode": "0", "areatab": [{"area": "1"}]})),
        ],
        monkeypatch,
    )
    areas = asyncio.run(provider.list_areas("a1"))
    assert areas == [{"area": "1"}]
    assert len(calls) == 2


def test_list_areas_91001_whole_ladder_raises_session_expired(monkeypatch):
    provider, calls = _provider(
        [(200, json.dumps(EXPIRED)), (200, json.dumps(EXPIRED))],
        monkeypatch,
    )
    try:
        asyncio.run(provider.list_areas("a1"))
    except SessionExpiredError:
        pass
    else:
        raise AssertionError("整条阶梯都 91001 时应抛 SessionExpiredError")
    assert len(calls) == 1 + len(http_json.EXPIRED_RECHECK_DELAYS)


def test_recheck_delay_is_short(monkeypatch):
    """复验延迟阶梯：首级是秒级（太长会拖慢用户查询）。"""
    assert 0 < http_json.EXPIRED_RECHECK_DELAY <= 5
    assert http_json.EXPIRED_RECHECK_DELAYS[0] == http_json.EXPIRED_RECHECK_DELAY


def test_recheck_ladder_is_bounded(monkeypatch):
    """阶梯总时长有上限（不能无限等学校）。"""
    assert len(http_json.EXPIRED_RECHECK_DELAYS) >= 2
    assert sum(http_json.EXPIRED_RECHECK_DELAYS) <= 15


def test_recheck_uses_fresh_connection(monkeypatch):
    """复验必须换新连接：学校是多后端节点，同节点可能持续返回 91001。

    断言方式：复验走的是独立的一次性 client（不进共享池、不进退休池）。
    """
    provider, _calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps(OK_BALANCE)),
        ],
        monkeypatch,
    )
    asyncio.run(provider.query_room(ROOM_PARAMS))
    # 共享 client 未被换掉（业务连接还在用），退休池也为空
    assert provider._retired == []
    assert provider._client is not None
    assert not provider._client.is_closed


# ================= main 层：连续确认计数 =================


def _plugin(store=None):
    store = store or _Store()
    return make_plugin(store=store), store


def test_strikes_increment_but_not_confirmed_before_window():
    """计数到了也先不判失效——时间窗没到之前一律只观察。"""
    plugin, store = _plugin()
    assert plugin._expired_strikes() == 0
    assert plugin._credential_dead_confirmed() is False

    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 1
    assert plugin._credential_dead_confirmed() is False

    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 2
    assert store.data["credential"]["expired_strikes"] == 2
    # 连续被拒刚起步，时间窗没到 → 不判失效（学校抖 8 分钟不算凭证过期）
    assert plugin._credential_dead_confirmed() is False


def test_strikes_confirmed_after_time_window():
    """连续被拒满时间窗 → 判定凭证确实失效。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(expired=True)
    # 把连续被拒起点往前挪 31 分钟，模拟「已连续被拒 31 分钟」
    meta = store.data["credential"]
    meta["expired_first_at"] = meta["expired_first_at"] - 31 * 60
    assert plugin._credential_dead_confirmed() is True


def test_strikes_cleared_by_ok():
    plugin, _store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(ok=True)
    assert plugin._expired_strikes() == 0
    assert plugin._credential_dead_confirmed() is False


def test_ok_clears_time_window_start():
    """学校恢复了 → 时间窗起点也一并清零（下一轮从零开始算）。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    assert store.data["credential"]["expired_first_at"] > 0
    plugin._update_expired_strikes(ok=True)
    assert store.data["credential"]["expired_first_at"] == 0


def test_network_failure_does_not_touch_strikes():
    """网络失败既不推进也不清除观察进度（ok=False, expired=False）。"""
    plugin, _store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(ok=False, expired=False)
    assert plugin._expired_strikes() == 1


def test_mixed_failure_marks_window_and_doubles_it():
    """连续被拒期间出现 5xx/超时（学校整体发癫的形态）→ 判定窗口加倍。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(expired=True)
    base_span = plugin._confirm_span_minutes()
    assert store.data["credential"].get("expired_mixed") is not True

    plugin._update_expired_strikes(net_fail=True)

    assert store.data["credential"]["expired_mixed"] is True
    assert plugin._confirm_span_minutes() == base_span * 2
    # 计数不动（网络失败不推进）
    assert plugin._expired_strikes() == 2


def test_mixed_failure_cleared_by_ok():
    """学校恢复 → 混杂标记一并清零（下一轮重新判断形态）。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(net_fail=True)
    assert store.data["credential"]["expired_mixed"] is True
    plugin._update_expired_strikes(ok=True)
    assert store.data["credential"]["expired_mixed"] is False


def test_confirm_span_is_configurable():
    """时间窗可配（学校频繁整段发癫就调大，想早发现就调小）。"""
    plugin, _store = _plugin()
    plugin.config["credential_confirm_min_span_minutes"] = 45
    assert plugin._confirm_span_minutes() == 45


def test_strikes_survive_restart():
    """计数持久化在 store.data["credential"]，重启不清零。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin2 = make_plugin(store=store)
    assert plugin2._expired_strikes() == 1


def test_strikes_without_store_is_noop():
    plugin = make_plugin(store=None)
    assert plugin._update_expired_strikes(expired=True) == 0
    assert plugin._expired_strikes() == 0


# ================= 手动换凭证清零 =================


class _Ev:
    def __init__(self, umo=UMO, private=True, is_admin=True):
        self.unified_msg_origin = umo
        self._private = private
        self._is_admin = is_admin

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._is_admin

    def plain_result(self, text):
        return text


async def _collect(agen):
    return [item async for item in agen]


def test_manual_credential_update_resets_strikes():
    """用户手工换新凭证 → 旧凭证攒下的观察计数清零。"""
    store = _Store()
    plugin = make_plugin(store=store)
    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 1

    out = "".join(
        asyncio.run(_collect(plugin.cmd_credential(_Ev(), "JSESSIONID=NEW")))
    )
    assert "凭证已保存" in out
    assert plugin._expired_strikes() == 0


# ================= 交互查询：单次 91001 的呈现 =================


def test_expired_result_renders_as_expired():
    """复验后仍失效 → 明确提示凭证失效（用户据此换凭证）。"""
    result = BalanceResult(ok=False, value=None, raw="会话已超时", session_expired=True)
    assert result.session_expired is True
