"""真机真跑（套件三模块）：把**用户真正看到的渲染**打出来。

不复用单测里的假源，也不重写一遍渲染逻辑——直接驱动套件根 main.py 的
包装器方法，打印出来的就是聊天窗口里的原文。电费插件在 v1.0.7 / v1.0.8 /
v1.1.0 三次「只有真跑才看得见的列表方向/页码/重复渲染」事故后定下的红线：
列表方向与合并逻辑必须真跑渲染验证。

三模块的「真」程度不同：
- games   真数据源（官网 / B站直连），摘要用假 provider 打通调度链路
- electric 桩渲染：假 Hjnu 源按学校真实返回形状造数据（本机不打学校接口），
  走的全是真实代码路径——绑定向导、房间分页、双费种关联、历史快照
- memo    本地全链路：存储、解析、扫描推送（FakeContext 收推）

用法：
    python tools/smoke.py                 # 全量
    python tools/smoke.py --module memo   # 只跑一个模块（electric/games/memo/quick）
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PKG_PARENT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PKG_PARENT
sys.path.insert(0, str(PKG_PARENT))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

import conftest  # noqa: F401  - 注入 astrbot 桩
from astrbot_plugin_suite.modules.electric.providers.base import BalanceResult
from astrbot_plugin_suite.modules.electric.storage import Store as ElectricStore
from astrbot_plugin_suite.modules.games.storage import Store
from astrbot_plugin_suite.modules.memo.storage import MemoStore
from conftest import (
    FakeContext,
    FakeEvent,
    FakeProvider,
    StubAstrBotConfig,
    make_games_plugin,
    make_memo_plugin,
    make_plugin,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UMO = "aiocqhttp:FriendMessage:999"


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ================= electric：桩渲染（真代码路径 + 假学校数据） =================

AC_AID = "0030000000004301"
ELEC_AID = "0030000000014501"


class FakeHjnu:
    """按学校真实返回形状造数据的假源（形状依据 AGENTS.md 实测记录）。"""

    def __init__(self):
        self.calls = 0

    async def list_areas(self, aid):
        # 学校数据坑：两项 area 值相同，只有校本部有完整楼栋
        return [{"area": "1", "areaname": "1#"}, {"area": "1", "areaname": "校本部"}]

    async def list_buildings(self, aid, area):
        if area.get("areaname") != "校本部":
            return [{"building": "春雪楼2", "buildingid": "b1"}]
        return [
            {"building": "春雪楼1", "buildingid": "b0"},
            {"building": "春雪楼2", "buildingid": "b1"},
        ]

    async def list_floors(self, aid, area, building):
        if building.get("building") == "春雪楼2":
            tops = [2, 3, 4, 5, 6, 7, 8, 9]
        else:
            tops = [1, 2, 3, 4, 5]
        return [{"floor": f"{n}层", "floorid": f"f{n}"} for n in tops]

    async def list_rooms(self, aid, area, building, floor):
        name = str(floor.get("floor", ""))
        n = int("".join(ch for ch in name if ch.isdigit()) or 8)
        if aid == ELEC_AID:
            # 宿舍电费 aid 下的房间名是「春雪楼A817」式
            return [
                {"room": f"春雪楼A{n}{i:02d}", "roomid": 600 + i} for i in range(1, 31)
            ]
        return [{"room": f"A-{n}-{i:02d}", "roomid": 600 + i} for i in range(1, 36)]

    async def fetch(self, binding):
        self.calls += 1
        aid = str(binding.get("params", {}).get("aid", binding.get("aid", "")))
        if aid == ELEC_AID:
            return BalanceResult(ok=True, value=8.88, raw="余额：8.88 元", unit="元")
        return BalanceResult(ok=True, value=12.34, raw="剩余电量 12.34 度")

    async def query_room(self, params):
        return BalanceResult(ok=True, value=9.99, raw="剩余电量 9.99 度")

    async def close(self):
        return None


async def run_cmd(plugin, attr, *words):
    """驱动套件根的包装器：async-generator 收集全部输出。

    宿主会把消息按空格切词后按位置填进包装器参数（框架参数绑定），桩环境
    没有这一层——这里照宿主语义模拟：签名里 self/event 之后的参数依次取
    words[2:]（words[0]=/指令，words[1]=子指令）。/备忘、/游戏 的包装器
    无额外参数，不受影响。
    """
    import inspect

    text = " ".join(words)
    ev = FakeEvent(text, umo=UMO)
    func = getattr(type(plugin), attr)
    params = [
        p
        for p in inspect.signature(func).parameters
        if p not in ("self", "event")
    ]
    kwargs = dict(zip(params, words[2:], strict=False))
    outs = []
    async for chunk in getattr(plugin, attr)(ev, **kwargs):
        outs.append(chunk)
    return "\n".join(outs)


async def electric_main(tmp):
    config = StubAstrBotConfig(
        {
            "fee_items": {AC_AID: "清美时雨春雪空调（校本部）", ELEC_AID: "宿舍电费（校本部）"},
            "threshold_warn": 10,
            "threshold_critical": 5,
        }
    )
    plugin = make_plugin(
        config=config,
        store=ElectricStore(tmp / "electric.json"),
        hjnu=FakeHjnu(),
        context=FakeContext(),
    )
    steps = [
        ("cmd_help", ("/电费", "帮助")),
        ("cmd_status", ("/电费", "状态")),
        ("cmd_bind", ("/电费", "绑定")),
        ("cmd_area", ("/电费", "校区", "2")),
        ("cmd_building", ("/电费", "楼栋", "2")),
        ("cmd_floor", ("/电费", "楼层", "7")),
        ("cmd_room", ("/电费", "房间")),
        ("cmd_room", ("/电费", "房间")),
        ("cmd_room", ("/电费", "房间", "17")),
        ("cmd_bind", ("/电费", "绑定", "1")),
        ("cmd_query", ("/电费", "查询")),
        ("cmd_history", ("/电费", "历史")),
        ("cmd_status", ("/电费", "状态")),
        ("cmd_log", ("/电费", "日志")),
        ("cmd_threshold", ("/电费", "预警线")),
    ]
    for attr, args in steps:
        banner("→ " + " ".join(args))
        print(await run_cmd(plugin, attr, *args))
    banner("electric 完成：假源被调用次数 = " + str(plugin.hjnu.calls))
    await plugin.terminate()


# ================= games：真数据源 =================

GAMES_ROUTES = [
    ("游戏",),
    ("游戏", "帮助"),
    ("游戏", "1999"),
    ("游戏", "方舟"),
    ("游戏", "列表"),
    ("游戏", "1999", "卡池"),
    ("游戏", "方舟", "版本"),
    ("游戏", "方舟", "兑换"),
    ("游戏", "原神"),
]


async def run_one(plugin, args):
    ev = FakeEvent(" ".join(args))
    async for _ in plugin.cmd_games(ev):
        pass
    return ev.result


async def games_main(tmp):
    provider = FakeProvider(text="- 【假模型要点】第一条\n- 【假模型要点】第二条")
    plugin = make_games_plugin(
        config=StubAstrBotConfig({"user_agent": UA}),
        context=FakeContext(provider=provider),
    )
    plugin.store = Store(tmp / "games.json")
    plugin._build_sources()
    try:
        banner("→ /游戏 订阅 全部（真跑：只写入内存 store）")
        print(await run_one(plugin, ("游戏", "订阅", "全部")))

        banner("→ 轮询推送预览（新公告会推到内存会话）")
        plugin.context.sent.clear()
        await plugin._poll_news()
        await plugin._poll_dynamics()
        for _umo, text in plugin.context.sent:
            print(text)
            print("-" * 40)
        if not plugin.context.sent:
            print("（本轮没有新内容——正常，取决于上次轮询后有没有新发布）")

        for args in GAMES_ROUTES:
            banner("→ " + " ".join(args))
            print(await run_one(plugin, args))

        banner(
            "→ AI 工具 games_dynamics / games_codes\n"
            + await plugin.tool_games_dynamics(FakeEvent("游戏"))
            + "\n"
            + "-" * 40
            + "\n"
            + await plugin.tool_games_codes(FakeEvent("游戏"))
        )
        banner(
            f"games 完成：摘要缓存 = "
            f"{ {k: len(v[1]) for k, v in plugin._digest_cache.items()} }；"
            f"假模型被调用 {len(provider.calls)} 次"
        )
    finally:
        if plugin._digest_tasks:
            await asyncio.gather(*plugin._digest_tasks.values(), return_exceptions=True)
        for src in list(plugin.sources.values()):
            await src.close()
        if plugin.bili:
            await plugin.bili.close()


# ================= memo：本地全链路 =================


async def memo_main(tmp):
    plugin = make_memo_plugin(
        config=StubAstrBotConfig(
            {"memo_scan_seconds": 60, "memo_max_per_session": 20}
        ),
        store=MemoStore(tmp / "memo.json"),
        context=FakeContext(),
    )
    steps = [
        ("/备忘",),
        ("/备忘", "交作业"),
        ("/备忘", "明天", "8:00", "交作业"),
        ("/备忘", "列表"),
        ("/备忘", "完成", "1"),
        ("/备忘", "删除", "2"),
        ("/备忘", "30分钟后", "起锅"),
        ("/备忘", "列表"),
    ]
    for args in steps:
        banner("→ " + " ".join(args))
        print(await run_cmd(plugin, "cmd_memo", *args))

    banner("→ 到期扫描推送（30分钟后 的那条把 due 改到过去再扫）")
    item = plugin.store.data["items"][-1]
    import time as _time

    item["due"] = int(_time.time()) - 5
    await plugin._scan()
    for session, text in plugin.context.sent:
        print(f"[推送至 {session}]")
        print(text)
    print(f"→ 已提醒标记：{[(i['id'], i['reminded']) for i in plugin.store.data['items']]}")

    banner("→ AI 工具 memo_add / memo_list / memo_complete")
    ev = FakeEvent("提醒我给妈妈打电话", umo=UMO)
    print(await plugin.tool_memo_add(ev, text="给妈妈打电话", when="21:30"))
    print("-" * 40)
    print(await plugin.tool_memo_list(ev))
    print("-" * 40)
    print(await plugin.tool_memo_complete(ev, memo_id="4"))

    banner("memo 完成：扫描推送 1 条，验证 ⏰ 文案与「发送成功才记账」")
    await plugin.terminate()


# ================= quick：快捷数字菜单（真代码路径） =================


async def quick_main(tmp):
    """快捷数字菜单真跑：/菜单 出菜单 → 回数字直达。

    走的是套件根 main.py 的包装器（真实注册面），打印出来的就是
    聊天窗口里用户看到的原文。
    """
    from astrbot_plugin_suite.main import SuitePlugin

    plugin = SuitePlugin(
        FakeContext(),
        StubAstrBotConfig({"modules": {}, "electric": {}, "games": {}, "memo": {}}),
    )
    # 电费侧接上假学校源，让「查电费余额」真的能出数
    plugin.electric.store = ElectricStore(tmp / "quick_electric.json")
    plugin.electric.hjnu = FakeHjnu()
    # 绑一个房间（与 electric_main 走同一条绑定链路的产物）
    plugin.electric.store.set_binding(
        UMO,
        {
            "room_label": "校本部/春雪楼2/8层/A-8-17",
            "fees": {
                "ac": {
                    "provider": "hjnu",
                    "params": {"aid": AC_AID},
                },
                "elec": {
                    "provider": "hjnu",
                    "params": {"aid": ELEC_AID},
                },
            },
            "history_by_fee": {},
        },
    )

    async def run_cmd(text):
        ev = FakeEvent(text, umo=UMO)
        async for _chunk in plugin.cmd_menu(ev):
            pass
        return ev.result

    async def run_digit(text):
        ev = FakeEvent(text, umo=UMO)
        async for _chunk in plugin.on_quick_digit_reply(ev):
            pass
        return ev.result

    banner("→ /菜单（出菜单）")
    print(await run_cmd("/菜单"))

    banner("→ 回 1（查电费余额）")
    print(await run_digit("1"))

    banner("→ 回 2（电费历史）")
    print(await run_digit("2"))

    banner("→ 回 3（电费自检）")
    print(await run_digit("3"))

    banner("→ 回 9（越界：没有这个选项）")
    print(await run_digit("9"))

    banner("→ 回 481526（6 位确认码：菜单不接管）")
    print(repr(await run_digit("481526")))

    banner("→ 回 0（取消菜单）")
    print(await run_digit("0"))

    banner("→ 取消后再回 1（不再接管）")
    print(repr(await run_digit("1")))


# ================= 入口 =================


async def main(modules):
    tmp = Path(tempfile.mkdtemp(prefix="suite_smoke_"))
    runners = {
        "electric": electric_main,
        "games": games_main,
        "memo": memo_main,
        "quick": quick_main,
    }
    try:
        for name, runner in runners.items():
            if modules and name not in modules:
                continue
            banner(f"########## {name} ##########")
            try:
                await runner(tmp)
            except Exception as e:
                print(f"✗ {name} 真跑失败：{e!r}")
                import traceback

                traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = None
    modules = set()
    while argv:
        if argv[0] == "--out" and len(argv) > 1:
            out = Path(argv[1])
            argv = argv[2:]
        elif argv[0] == "--module" and len(argv) > 1:
            modules.add(argv[1])
            argv = argv[2:]
        else:
            break
    if out:
        import io

        buf = io.StringIO()
        sys.stdout = buf
        asyncio.run(main(modules or None))
        sys.stdout = sys.__stdout__
        out.write_text(buf.getvalue(), encoding="utf-8")
        print(f"written: {out}  ({len(buf.getvalue())} chars)")
    else:
        asyncio.run(main(modules or None))
