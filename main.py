"""AstrBot 功能套件：宿舍电费监控 + 游戏资讯聚合 + 备忘录提醒。

一个插件、三个互不依赖的模块：
- modules/electric  宿舍电费余额监控预警（/电费 …，10 个 dorm_electric_* 工具）
- modules/games     游戏资讯聚合（/游戏 …，3 个 games_* 工具）
- modules/memo      备忘录待办与定时提醒（/备忘 …，3 个 memo_* 工具）

⚠️ 框架约束（宿主 star_manager 按「装饰器函数的定义模块」归属 handler）：
所有 @filter 装饰器必须物理写在本文件，本文件是唯一注册面。装饰器方法一律
是薄壳——做模块开关检查后转发给对应模块的 Core；全部业务逻辑在
modules/<模块>/service.py 及其子模块里。

配置分四组（_conf_schema.json）：modules 总开关、electric、games、memo。旧单体
插件的配置与数据在首次 initialize 时自动迁移（modules/migrate.py，原件保留
可回滚）。某个配置档只想启用部分模块：管理员发 /套件 档案 <档名> <模块> 关。
"""

import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import (
        get_astrbot_config_path,
        get_astrbot_data_path,
    )
except ImportError:

    def get_astrbot_config_path() -> str:
        return "data/config"

    def get_astrbot_data_path() -> str:
        return "data"


try:
    from .modules import migrate
    from .modules.config_view import SectionConfig
    from .modules.electric import service as electric_service
    from .modules.games import service as games_service
    from .modules.memo import service as memo_service
except ImportError:  # 以仓库根为工作目录直接导入（测试/脚本）
    from modules import migrate
    from modules.config_view import SectionConfig
    from modules.electric import service as electric_service
    from modules.games import service as games_service
    from modules.memo import service as memo_service

PLUGIN_NAME = "astrbot_plugin_suite"

# 模块别名 → 内部 key（/套件 与开关文案共用）
MODULE_ALIASES: dict[str, str] = {
    "electric": "electric",
    "games": "games",
    "memo": "memo",
    "电费": "electric",
    "游戏": "games",
    "备忘": "memo",
    "待办": "memo",
}
MODULE_LABEL = {"electric": "宿舍电费", "games": "游戏资讯", "memo": "备忘录"}
MODULE_DISABLED_TEXT = {
    "electric": "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。",
    "games": "游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。",
    "memo": "备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。",
}

# 档内模块覆盖存 data/plugin_data/astrbot_plugin_suite/profiles.json
PROFILES_FILE = "profiles.json"


def _llm_request_hook():
    """取 on_llm_request 装饰器；宿主版本过旧没有该钩子时退化为 no-op。

    插件必须在缺钩子的宿主上照常加载，所以这里不抛 AttributeError。
    """
    deco = getattr(filter, "on_llm_request", None)
    if deco is None:
        return lambda func: func
    return deco()


@filter.command_group("电费")
def electric():
    """宿舍电费余额监控预警。"""


@register(
    PLUGIN_NAME,
    "Cecilian",
    "功能套件：宿舍电费监控 + 游戏资讯聚合 + 备忘录提醒，一个插件装齐，模块可按配置档开关",
    "1.1.0",
)
class SuitePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.electric = electric_service.ElectricCore(
            context, SectionConfig(config, "electric")
        )
        self.games = games_service.GamesCore(
            context, SectionConfig(config, "games")
        )
        self.memo = memo_service.MemoCore(
            context, SectionConfig(config, "memo")
        )
        # 档内模块覆盖缓存：mtime 变了才重读 profiles.json
        self._profiles: dict[str, dict[str, bool]] = {}
        self._profiles_mtime: float = -1.0
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME

    # ================= 模块开关 =================

    def _module_enabled_global(self, name: str) -> bool:
        try:
            modules_cfg = self.config.get("modules")
        except (AttributeError, TypeError):
            return True
        if not isinstance(modules_cfg, dict):
            return True
        return modules_cfg.get(name) is not False

    def _profiles_path(self) -> Path:
        return self._data_dir / PROFILES_FILE

    def _load_profiles(self) -> dict[str, dict[str, bool]]:
        path = self._profiles_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._profiles = {}
            self._profiles_mtime = -1.0
            return self._profiles
        if mtime == self._profiles_mtime:
            return self._profiles
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profiles: dict[str, dict[str, bool]] = {}
            for pid, override in raw.items():
                if isinstance(override, dict):
                    profiles[str(pid)] = {
                        str(k): bool(v) for k, v in override.items()
                    }
            self._profiles = profiles
            self._profiles_mtime = mtime
        except (OSError, ValueError):
            self._profiles = {}
        return self._profiles

    def _save_profiles(self) -> None:
        path = self._profiles_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._profiles, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._profiles_mtime = path.stat().st_mtime
        except OSError as e:
            logger.warning(f"[{PLUGIN_NAME}] profiles.json 写入失败：{e!r}")

    def _profile_id(self, umo: str | None) -> str | None:
        """会话 → 配置档 id。宿主缺该 API 或解析失败时返回 None（仅全局开关生效）。"""
        if not umo:
            return None
        mgr = getattr(self.context, "astrbot_config_mgr", None)
        fn = getattr(mgr, "get_conf_info", None)
        if not callable(fn):
            return None
        try:
            info = fn(umo)
        except Exception:
            return None
        if isinstance(info, dict):
            pid = info.get("id")
        else:
            pid = getattr(info, "id", None)
        return str(pid) if pid is not None else None

    def _module_enabled(self, name: str, umo: str | None = None) -> bool:
        """开关判定：全局总开关 > 档内覆盖（profiles.json）> 默认开。"""
        if not self._module_enabled_global(name):
            return False
        pid = self._profile_id(umo)
        if pid:
            override = self._load_profiles().get(pid)
            if override and name in override:
                return bool(override[name])
        return True

    # ================= /套件：模块开关管理 =================

    def _suite_args(self, event) -> str:
        """取掉指令名后的参数部分（裸指令按空格切，与 /游戏 同一套路）。"""
        getter = getattr(event, "get_message_str", None)
        raw = ""
        if callable(getter):
            try:
                raw = str(getter() or "")
            except Exception:
                raw = ""
        if not raw:
            raw = str(getattr(event, "message_str", "") or "")
        raw = " ".join(raw.split()).strip()
        if not raw:
            return ""
        return raw.partition(" ")[2].strip()

    def _suite_status_lines(self, event) -> list[str]:
        lines = ["【套件模块开关】"]
        pid = self._profile_id(getattr(event, "unified_msg_origin", None))
        profiles = self._load_profiles()
        override = profiles.get(pid) if pid else None
        for key, label in MODULE_LABEL.items():
            glob = "开" if self._module_enabled_global(key) else "关"
            line = f"{label}({key})：全局 {glob}"
            if override and key in override:
                line += "；本档 " + ("开" if override[key] else "关")
            else:
                line += "；本档 跟随全局"
            lines.append(line)
        lines.append(
            "修改：/套件 模块 <电费|游戏|备忘> <开|关>（全局，重启后停止后台轮询，指令立即拦截）"
        )
        lines.append("　　　/套件 档案 <档名> <电费|游戏|备忘> <开|关|默认>（只影响该配置档）")
        return lines

    def _suite_set_global(self, event, args: list[str]) -> str:
        if not args or args[0] in ("帮助", "help"):
            return "用法：/套件 模块 <电费|游戏|备忘> <开|关>"
        if not event.is_admin():
            return "🔒 模块总开关只有管理员能改。"
        if len(args) != 2 or args[1] not in ("开", "关"):
            return "用法：/套件 模块 <电费|游戏|备忘> <开|关>"
        key = MODULE_ALIASES.get(args[0])
        if key is None:
            return f"没认出模块「{args[0]}」。可用：电费 / 游戏 / 备忘"
        try:
            modules_cfg = dict(self.config.get("modules") or {})
        except (AttributeError, TypeError):
            modules_cfg = {}
        modules_cfg[key] = args[1] == "开"
        self.config["modules"] = modules_cfg
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存模块开关失败：{e!r}")
        tail = "" if args[1] == "开" else "（后台轮询将在下次重启/重载后停止，指令已立即拦截）"
        return f"✅ {MODULE_LABEL[key]}模块已全局{args[1]}{tail}"

    def _suite_resolve_profile(self, name: str) -> tuple[str | None, str]:
        """档名 → 档 id。返回 (id, 错误说明)。"""
        if not name:
            return None, "用法：/套件 档案 <档名> <电费|游戏|备忘> <开|关|默认>"
        mgr = getattr(self.context, "astrbot_config_mgr", None)
        fn = getattr(mgr, "get_conf_list", None)
        candidates = []
        if callable(fn):
            try:
                candidates = list(fn() or [])
            except Exception:
                candidates = []
        for info in candidates:
            iname = info.get("name") if isinstance(info, dict) else getattr(info, "name", None)
            if str(iname) == name:
                iid = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
                return (str(iid) if iid is not None else None), ""
        for info in candidates:
            iid = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
            if str(iid) == name:
                return str(iid), ""
        return None, f"没找到配置档「{name}」。"

    def _suite_set_profile(self, event, args: list[str]) -> str:
        if not args or args[0] in ("帮助", "help"):
            return "用法：/套件 档案 <档名> <电费|游戏|备忘> <开|关|默认>"
        if not event.is_admin():
            return "🔒 档内开关只有管理员能改。"
        if len(args) != 3 or args[2] not in ("开", "关", "默认"):
            return "用法：/套件 档案 <档名> <电费|游戏|备忘> <开|关|默认>"
        key = MODULE_ALIASES.get(args[1])
        if key is None:
            return f"没认出模块「{args[1]}」。可用：电费 / 游戏 / 备忘"
        pid, err = self._suite_resolve_profile(args[0])
        if pid is None:
            return err
        profiles = self._load_profiles()
        override = dict(profiles.get(pid) or {})
        if args[2] == "默认":
            override.pop(key, None)
            verb = "恢复跟随全局"
        else:
            override[key] = args[2] == "开"
            verb = f"已{args[2]}"
        if override:
            profiles[pid] = override
        else:
            profiles.pop(pid, None)
        self._save_profiles()
        label = MODULE_LABEL[key]
        return f"✅ 配置档 {pid} 的{label}模块{verb}（立即生效）"

    @filter.command("套件", alias={"suite"})
    async def cmd_suite(self, event: AstrMessageEvent):
        """套件模块开关：/套件 看状态；/套件 模块 电费 关；/套件 档案 AAA 游戏 关。"""
        parts = self._suite_args(event).split()
        if not parts or parts[0] in ("帮助", "help", "状态"):
            yield event.plain_result("\n".join(self._suite_status_lines(event)))
            return
        if parts[0] == "模块":
            yield event.plain_result(self._suite_set_global(event, parts[1:]))
            return
        if parts[0] == "档案":
            yield event.plain_result(self._suite_set_profile(event, parts[1:]))
            return
        yield event.plain_result("\n".join(self._suite_status_lines(event)))

    # ================= 生命周期 =================

    async def initialize(self):
        for note in migrate.migrate_configs(self.config, get_astrbot_config_path()):
            logger.info(f"[{PLUGIN_NAME}] {note}")
        for note in migrate.migrate_data(get_astrbot_data_path()):
            logger.info(f"[{PLUGIN_NAME}] {note}")
        for name, core in (
            ("electric", self.electric),
            ("games", self.games),
            ("memo", self.memo),
        ):
            if not self._module_enabled_global(name):
                logger.info(f"[{PLUGIN_NAME}] 模块 {name} 已全局关闭，跳过初始化")
                continue
            try:
                await core.initialize()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 模块 {name} 初始化失败（不影响其他模块）")

    async def terminate(self):
        for name, core in (
            ("electric", self.electric),
            ("games", self.games),
            ("memo", self.memo),
        ):
            try:
                await core.terminate()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 模块 {name} 退出清理失败")
        self._profiles = {}
        self._profiles_mtime = -1.0

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_user_replied_code(self, event: AstrMessageEvent):
        """记录「用户亲手回复了确认验证码」。

        放在事件级而不是 on_llm_request：这样标记与 LLM 是否被唤醒、模型是否
        支持 function calling 完全无关，纯数字消息被别的插件先吃掉也不影响。
        判定很严——整条消息里除标点外只剩验证码，所以群里问
        「481526 度电够吗」不会被误判成已同意。不发消息、不阻断事件。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return
        return await self.electric.on_user_replied_code(event)

    @_llm_request_hook()
    async def inject_electric_hint(self, event: AstrMessageEvent, req) -> None:
        """关键词命中的对话，在 system_prompt 末尾追加电费工具提示（幂等）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return
        return await self.electric.inject_electric_hint(event, req)

    @filter.llm_tool(name="dorm_electric_browse")
    async def tool_dorm_electric_browse(self, event: AstrMessageEvent) -> str:
        """查看宿舍电费的学校结构：当前该选校区、楼栋、楼层还是房间。

        第一次调用会从「校区」开始；之后按已选到的层级继续往下。
        只读，群里也能用。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_browse(event)

    @filter.llm_tool(name="dorm_electric_pick")
    async def tool_dorm_electric_pick(
        self,
        event: AstrMessageEvent,
        index: int = 0,
        page: int = 0,
    ) -> str:
        """在最近一次 dorm_electric_browse 列出的选项里选一项，往下一层走。

        Args:
            index(int): 要选的序号，取自 browse 结果里带编号的那一行
            page(int): 当前层是房间列表时要翻到第几页，0 表示不翻页
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_pick(event, index, page)

    @filter.llm_tool(name="dorm_electric_bind_room")
    async def tool_dorm_electric_bind_room(
        self, event: AstrMessageEvent, room_no: int = 0, hint: str = ""
    ) -> str:
        """选定房间并向用户发一个绑定确认码（此时还没真正绑定）。

        用户想绑定、换宿舍或改绑监控房间时调用本工具。用户说过房间信息
        （例如「春雪楼2 8层 A817」「春雪楼817」）时，把原话传给
        hint 直接发起，完全不需要 browse/pick；几轮之前说过的也算——对话里能找到
        房间就传，不要重新问。用户只说了「绑定」这类碎片时，工具会提示本会话
        最近定位过的房间，照它给的 hint 重调即可。楼栋名以工具返回为准，不要臆造。
        已绑定时传一个不同的房间就是「改绑」：同样只发一个确认码，确认后
        原房间的历史与预警状态清零、开始监控新房间。传回同一个房间会被拒绝。
        只有完全说不出房间信息时，才用 browse/pick 逐级选到房间列表，
        再把列表里带编号那行的编号传给 room_no。
        凭证（JSESSIONID）这类密钥永远不要向用户索要、不要转述或保存：
        用户自己贴出来时，让他私下发送 /电费 凭证 JSESSIONID=… 更新。

        Args:
            room_no(int): 仅当已用 browse/pick 走到房间列表时，列表里带编号那行的编号
            hint(string): 用户提到的房间原话，如「春雪楼817」「春雪楼2 8层 A817」「A-8-17」；传了它就不需要 room_no
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_bind_room(event, room_no, hint)

    @filter.llm_tool(name="dorm_electric_unbind")
    async def tool_dorm_electric_unbind(self, event: AstrMessageEvent) -> str:
        """向用户发一个解绑确认验证码（此时还没解绑）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_unbind(event)

    @filter.llm_tool(name="dorm_electric_confirm")
    async def tool_dorm_electric_confirm(
        self, event: AstrMessageEvent, code: str = ""
    ) -> str:
        """提交用户回复的确认码，完成绑定或解绑。

        Args:
            code(string): 用户刚刚回复的确认码，必须是用户亲手发的那串数字
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_confirm(event, code)

    @filter.llm_tool(name="dorm_electric_balance")
    async def tool_dorm_electric_balance(
        self, event: AstrMessageEvent, days: int = 0
    ) -> str:
        """查本会话绑定宿舍的当前电费余额（空调费 + 宿舍电费），顺带告知预警线与播报设置。

        只要用户问起本会话宿舍的电费/空调费，例如「电费还剩多少」「空调费还有多少」
        「这个月用了多少」「还能用几天」「余额够不够」，都调用本工具；不要凭印象回答。

        Args:
            days(int): 顺便看最近几天的每日余额，0 表示只看当前余额，最大 60
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_balance(event, days)

    @filter.llm_tool(name="dorm_electric_mute_alerts")
    async def tool_dorm_electric_mute_alerts(
        self, event: AstrMessageEvent, hours: float = 0, scope: str = "alerts"
    ) -> str:
        """暂停或恢复本会话的余额提醒（预警推送和/或每日播报），或查看静音状态。

        用户说「别再提醒了」「静音 24 小时」「烦死了别报了」→ scope=all 或默认；
        只嫌早上播报吵 → scope=daily。静音不需要验证码（只影响提醒、随时可逆），
        但写操作仅限私聊。

        Args:
            hours(number): 大于 0 = 静音 N 小时（上限 168）；0 = 只查当前状态；-1 = 解除静音
            scope(string): 作用范围："alerts"=只静音余额预警（默认）；"daily"=只静音每日播报；"all"=两者都静音
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_mute_alerts(event, hours, scope)

    @filter.llm_tool(name="dorm_electric_set_alert_threshold")
    async def tool_dorm_electric_set_alert_threshold(
        self,
        event: AstrMessageEvent,
        warn: float = 0,
        critical: float = 0,
        kind: str = "all",
    ) -> str:
        """设置本会话的余额预警线（只影响这个会话的提醒，不改全局配置、不影响其他会话）。

        用户说「低于 20 就提醒我」「空调费低于 5 立刻告诉我」这类话时调用。
        空调费按度计、宿舍电费按元计，消耗速度不同，可以只给其中一个设置（kind 传 ac 或 elec）。
        写操作仅限私聊。预警线只对该会话已绑定的房间生效，未绑定无法设置。

        Args:
            warn(number): 预警线：余额低于它时发预警提醒；0 = 只看当前生效值；-1 = 恢复全局默认
            critical(number): 紧急线（低于它时发紧急提醒），可不传，默认取预警线的一半
            kind(string): 作用费种："ac"=只设空调费；"elec"=只设宿舍电费；"all"=两者都设（默认）；「空调」「电」这类中文也可以
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_set_alert_threshold(event, warn, critical, kind)

    @filter.llm_tool(name="dorm_electric_query_room")
    async def tool_dorm_electric_query_room(
        self, event: AstrMessageEvent, room_hint: str = ""
    ) -> str:
        """按房间名/编号查任意宿舍的当前电费余额，不需要绑定。

        用户想查「某间宿舍」的电费时（无论本会话是否已绑定），把他说的话直接传进来，
        例如「春雪楼817 电费多少」「帮我看看 5-302」。用户只说了模糊片段（如「春雪」）时，
        工具会提示本会话最近定位过的房间，照它给的 hint 重调即可；楼栋名以工具返回为准，
        不要臆造。

        Args:
            room_hint(string): 用户的原话直接传，例如 春雪楼817、817、A-8-17、A817、春雪楼2 8层 A817
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_query_room(event, room_hint)

    @electric.command("帮助", alias={"help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看指令帮助"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_help(event):
            yield _r

    @electric.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前绑定与插件运行状态"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_status(event):
            yield _r

    @filter.llm_tool(name="dorm_electric_check")
    async def tool_dorm_electric_check(self, event: AstrMessageEvent) -> str:
        """一次性自检：凭证状态 + 绑定摘要 + 两个费种实时查询 + 本会话最近事件。只读，群里也能用。

        用户说「凭证还有效吗」「怎么没提醒我」「电费是不是查不了/挂了」「检查一下电费查询」
        这类话时调用。
        凭证（JSESSIONID）永远不要向用户索要、不要转述或保存：用户自己贴出来时，
        让管理员私下发送 /电费 凭证 JSESSIONID=… 更新（该指令仅限管理员）。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            return "宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.electric.tool_dorm_electric_check(event)

    @electric.command("检查", alias={"自检"})
    async def cmd_check(self, event: AstrMessageEvent):
        """一次性自检：凭证是否生效、绑定是否正确、余额能否查到"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_check(event):
            yield _r

    @electric.command("校区")
    async def cmd_area(self, event: AstrMessageEvent, area_id: str | None = None):
        """选择缴费项目的校区"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_area(event, area_id):
            yield _r

    @electric.command("楼栋")
    async def cmd_building(self, event: AstrMessageEvent, building_id: str | None = None):
        """选择楼栋"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_building(event, building_id):
            yield _r

    @electric.command("楼层")
    async def cmd_floor(self, event: AstrMessageEvent, floor_id: str | None = None):
        """选择楼层"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_floor(event, floor_id):
            yield _r

    @electric.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, room_no: str | None = None):
        """浏览房间列表（无参翻页、p<页码>跳页）或按绝对编号选择房间"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_room(event, room_no):
            yield _r

    @electric.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, confirm: str | None = None):
        """启动绑定宿舍向导（无参）或确认绑定（带参 1）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_bind(event, confirm):
            yield _r

    @electric.command("确认")
    async def cmd_confirm(self, event: AstrMessageEvent, code: str | None = None):
        """提交 AI 给的确认验证码（带参=执行，不带参=看待确认项）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_confirm(event, code):
            yield _r

    @electric.command("静音")
    async def cmd_mute(self, event: AstrMessageEvent, arg: str | None = None):
        """暂停/恢复余额预警推送（仅私聊）：无参看状态，0/取消=恢复，N=静音 N 小时"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_mute(event, arg):
            yield _r

    @electric.command("预警线")
    async def cmd_threshold(self, event: AstrMessageEvent, arg: str | None = None):
        """查看/设置本会话预警线（仅私聊）：无参看当前，取消=恢复全局，数字=设置"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_threshold(event, arg):
            yield _r

    @electric.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """取消本会话的电费监控"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_unbind(event):
            yield _r

    @electric.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        """立即查询绑定的房间余额"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_query(event):
            yield _r

    @electric.command("历史")
    async def cmd_history(self, event: AstrMessageEvent, n: str | None = None):
        """查看最近 n 天每日余额快照（默认 7 天，最多 60 天）"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_history(event, n):
            yield _r

    @electric.command("日志")
    async def cmd_log(self, event: AstrMessageEvent, n: str | None = None):
        """查看最近 n 条事件 + 最近一次原始返回（默认 20，最多 100）"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_log(event, n):
            yield _r

    @electric.command("凭证")
    async def cmd_credential(self, event: AstrMessageEvent, credential: str | None = None):
        """更新缴费系统会话凭证（JSESSIONID，仅限管理员私聊）"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("electric", umo):
            yield event.plain_result("宿舍电费模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.electric.cmd_credential(event, credential):
            yield _r

    @filter.command("游戏", alias={"games", "游戏资讯"})
    async def cmd_games(self, event: AstrMessageEvent):
        """游戏资讯监控：/游戏、/游戏 1999、/游戏 1999 卡池、/游戏 订阅 全部。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("games", umo):
            yield event.plain_result("游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.games.cmd_games(event):
            yield _r

    @_llm_request_hook()
    async def inject_games_hint(self, event: AstrMessageEvent, req) -> None:
        """聊到游戏话题时在 system_prompt 末尾追加幕后提示（幂等 + 全包 try）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("games", umo):
            return
        return await self.games.inject_games_hint(event, req)

    @filter.llm_tool(name="games_news")
    async def tool_games_news(
        self,
        event: AstrMessageEvent,
        game: str = "",
        category: str = "",
        keyword: str = "",
    ) -> str:
        """查《重返未来：1999》《明日方舟》的官方资讯（公告/活动/新闻）。

        用户问「1999 最近有什么活动」「方舟这周更新了什么」「有没有新前瞻」时用这个。
        keyword 可以填「卡池」「版本」「维护」等词做进一步筛选。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
            category(string): 分类，可选「最新」「公告」「活动」「新闻」。留空按「最新」。
            keyword(string): 额外关键词筛选，例如 卡池/版本/前瞻/维护。留空不过滤。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("games", umo):
            return "游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.games.tool_games_news(event, game, category, keyword)

    @filter.llm_tool(name="games_codes")
    async def tool_games_codes(self, event: AstrMessageEvent, game: str = "") -> str:
        """查游戏兑换码。

        用户问「1999 兑换码」「方舟有兑换码吗」「有没有礼包码」时用这个。
        兑换码由插件从 B站官方动态与公告正文自动提取，查不到就是真的没有，
        不要自己编一个码给用户。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("games", umo):
            return "游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.games.tool_games_codes(event, game)

    @filter.llm_tool(name="games_dynamics")
    async def tool_games_dynamics(self, event: AstrMessageEvent, game: str = "") -> str:
        """查 B站官方号的最新动态。

        用户问「1999 B站发什么了吗」「方舟官方号最近说了啥」时用这个。
        动态是兑换码的来源之一，直播预告通常也在这里。

        Args:
            game(string): 游戏名。重返未来/1999/九九/重返 或 明日方舟/方舟/ak。留空表示两游戏都要。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("games", umo):
            return "游戏资讯模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.games.tool_games_dynamics(event, game)

    # ================= memo：备忘录待办与定时提醒 =================

    @filter.command("备忘", alias={"待办", "todo", "memo"})
    async def cmd_memo(self, event: AstrMessageEvent):
        """备忘录与定时提醒：/备忘 添加 明天 8:00 交作业、/备忘 列表、/备忘 完成 3。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("memo", umo):
            yield event.plain_result("备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。")
            return
        async for _r in self.memo.cmd_memo(event):
            yield _r

    @_llm_request_hook()
    async def inject_memo_hint(self, event: AstrMessageEvent, req) -> None:
        """聊到备忘/待办话题时在 system_prompt 末尾追加幕后提示（幂等 + 全包 try）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("memo", umo):
            return
        return await self.memo.inject_memo_hint(event, req)

    @filter.llm_tool(name="memo_add")
    async def tool_memo_add(
        self,
        event: AstrMessageEvent,
        text: str = "",
        when: str = "",
    ) -> str:
        """记一条备忘/待办，到点自动提醒用户。用户说「提醒我…」「记一下…」时调用。
        Args:
            text(string): 要记的内容，例如「交作业」「给妈妈打电话」
            when(string): 可选提醒时间：21:30 / 明天 8:00 / 30分钟后 / 3天后 / 10月1日 9:00；留空=纯待办不定时提醒
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("memo", umo):
            return "备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.memo.tool_memo_add(event, text, when)

    @filter.llm_tool(name="memo_list")
    async def tool_memo_list(self, event: AstrMessageEvent) -> str:
        """列出当前会话的备忘/待办。用户问「我记了什么」「有什么待办」时调用。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("memo", umo):
            return "备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.memo.tool_memo_list(event)

    @filter.llm_tool(name="memo_complete")
    async def tool_memo_complete(
        self,
        event: AstrMessageEvent,
        memo_id: str = "",
    ) -> str:
        """把当前会话的某条备忘标记为完成。编号来自 memo_list，不要编造。
        Args:
            memo_id(string): 备忘编号（纯数字），例如「3」
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not self._module_enabled("memo", umo):
            return "备忘录模块已关闭，请联系管理员在插件配置或 /套件 里开启。"
        return await self.memo.tool_memo_complete(event, memo_id)
