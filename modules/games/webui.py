"""WebUI：B站扫码登录 SESSDATA。

v1 不做仪表盘，pages/ 下只有这一个页面。AstrBot 的插件页面机制：
- pages/<name>/index.html 由宿主自动发现（只需目录里有 index.html，无需声明）
- 页面里的 JS 通过 window.AstrBotPluginPage Bridge 调后端：
    apiGet("login/generate")      → endpoint **不含**插件名，前端会自动拼
    apiGet("login/poll", {key})   → 已登录 WebUI 鉴权，走同一套权限
- 后端用 context.register_web_api(route, handler, methods, desc) 注册，
  route **必须含**插件名作为第一段

⚠️ 响应格式按宿主约定（docs/zh/dev/star/guides/plugin-pages.md）：
    {"status": "ok", "data": …}       → 前端 Promise resolve 为 data
    {"status": "error", "message": …} → 前端 Promise reject

🔒 安全：SESSDATA 等于账号登录态。任何响应、日志、聊天输出都不得回显它，
只有「是否已配置」这类布尔状态可以外传（有测试断言）。
"""

import logging
import time

logger = logging.getLogger("astrbot.plugin.games_monitor")

# 路由前缀必须用套件插件名（页面归属 astrbot_plugin_suite，前端 apiGet 按它拼接）。
PLUGIN_NAME = "astrbot_plugin_suite"
BASE = f"/{PLUGIN_NAME}/login"

# 二维码有效期 180 秒，超时前端会自动重新 generate
QRCODE_TTL_SECONDS = 180
# 一次 poll 允许的最长等待：前端每 2 秒调一次，这里只是兜底
POLL_MAX_WAIT_SECONDS = 8

STATUS_TEXT = {
    0: "登录成功",
    86038: "二维码已过期，正在自动刷新",
    86090: "已扫码，请在手机上确认",
    86101: "等待扫码",
}

ERR_NO_BILI = "B站客户端还没初始化，请稍后再试。"
ERR_NO_KEY = "二维码已过期，请刷新页面重新扫码。"


def _pname(plugin) -> str:
    return str(getattr(plugin, "plugin_name", "") or PLUGIN_NAME)


def _ok(data) -> dict:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


def register(plugin) -> None:
    """挂载扫码登录的两个端点。宿主不支持时静默跳过，插件照常加载。"""
    reg = getattr(plugin.context, "register_web_api", None)
    if not callable(reg):
        logger.info("[%s] 宿主不支持 register_web_api，扫码登录页未挂载", _pname(plugin))
        return
    try:
        reg(f"{BASE}/generate", plugin._web_generate, ["GET"], "B站登录：生成二维码")
        reg(f"{BASE}/poll", plugin._web_poll, ["GET"], "B站登录：轮询扫码状态")
    except Exception as e:
        logger.warning("[%s] 扫码登录端点注册失败：%r", _pname(plugin), e)


def _read_query(name: str, default: str = "") -> str:
    """从 Web API 的查询串里取参数。

    优先用 astrbot.api.web.request（v4.27 官方推荐），宿主没有就退回 quart。
    两条路都拿不到时返回默认值——不能因为读不到参数就让整个插件挂掉。
    """
    try:
        from astrbot.api.web import request as _req

        return str(_req.query.get(name, default) or default)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("astrbot.api.web.request 读取失败：%r", e)
    try:
        from quart import request as _qreq

        return str(_qreq.args.get(name, default) or default)
    except Exception:
        return default


async def generate(plugin) -> dict:
    """生成登录二维码。返回 {key, url, ttl}。"""
    bili = getattr(plugin, "bili", None)
    if bili is None:
        return _err(ERR_NO_BILI)
    try:
        key, url = await bili.login.generate()
    except Exception as e:
        logger.warning("[%s] 生成 B站二维码失败：%r", _pname(plugin), e)
        return _err("生成二维码失败，B站接口暂时不可用。")
    plugin._qr_at = time.time()
    return _ok({"key": key, "url": url, "ttl": QRCODE_TTL_SECONDS})


async def poll(plugin) -> dict:
    """轮询扫码状态。成功时把 SESSDATA 写进插件配置。"""
    bili = getattr(plugin, "bili", None)
    if bili is None:
        return _err(ERR_NO_BILI)
    key = _read_query("key")
    if not key:
        return _err("缺少二维码 key，请刷新页面。")

    generated = float(getattr(plugin, "_qr_at", 0) or 0)
    if generated and (time.time() - generated) > QRCODE_TTL_SECONDS:
        # 本地也判一次：省得为一个必然过期的 key 白跑一趟 B站
        return _ok({"code": 86038, "text": STATUS_TEXT[86038], "login": False})

    try:
        code, sessdata = await bili.login.poll(key)
    except Exception as e:
        logger.warning("[%s] 轮询 B站扫码状态失败：%r", _pname(plugin), e)
        return _err("查询扫码状态失败，B站接口暂时不可用。")

    if code != 0:
        return _ok({"code": code, "text": STATUS_TEXT.get(code, "未知状态"), "login": False})

    if not sessdata:
        # code=0 但没解析出登录态：告诉页面「再来一次」而不是谎报成功
        logger.warning("[%s] B站扫码返回成功但未取到 SESSDATA", _pname(plugin))
        return _ok({"code": -1, "text": "未取到登录态，请重新扫码", "login": False})

    _save_sessdata(plugin, sessdata)
    # 🔒 只回布尔状态，绝不回显 SESSDATA 本身
    return _ok({"code": 0, "text": STATUS_TEXT[0], "login": True})


def _save_sessdata(plugin, sessdata: str) -> None:
    """写入配置并热更新客户端。SESSDATA 本身不进日志。"""
    try:
        plugin.config["bilibili_sessdata"] = sessdata
        saver = getattr(plugin.config, "save_config", None)
        if callable(saver):
            saver()
    except Exception as e:
        logger.error("[%s] 保存 B站登录态失败：%r", _pname(plugin), e)
    if getattr(plugin, "bili", None) is not None:
        plugin.bili.set_sessdata(sessdata)
    logger.info("[%s] B站登录态已更新", _pname(plugin))
