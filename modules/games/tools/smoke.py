"""真机真跑：直接打三个数据源，把**用户真正看到的渲染**打出来。

不复用单测里的假源，也不重写一遍渲染逻辑——直接驱动 cmd_games，
这样打印出来的就是聊天窗口里的原文。电费插件在 v1.0.7 / v1.0.8 / v1.1.0
三次「只有真跑才看得见的列表方向/页码/重复渲染」事故后定下的红线：
列表方向与合并逻辑必须真数据渲染验证。

用法：
    python tools/smoke.py            # 全量
    python tools/smoke.py 游戏 卡池   # 只跑某条指令（参数原样透传）
"""

import asyncio
import sys
from pathlib import Path

PKG_PARENT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PKG_PARENT / "astrbot_plugin_games_monitor"
sys.path.insert(0, str(PKG_PARENT))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

import conftest  # noqa: F401  - 注入 astrbot 桩
from astrbot_plugin_games_monitor.storage import Store
from conftest import (
    FakeContext,
    FakeEvent,
    StubAstrBotConfig,
    make_plugin,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 与 README 里列的用法一一对应
ROUTES = [
    ("游戏",),
    ("游戏", "帮助"),
    ("游戏", "1999"),
    ("游戏", "方舟"),
    ("游戏", "1999", "卡池"),
    ("游戏", "方舟", "卡池"),
    ("游戏", "方舟", "版本"),
    ("游戏", "1999", "兑换"),
    ("游戏", "方舟", "兑换"),
    ("游戏", "原神"),  # 认不出的游戏名
]


async def run_one(plugin, args):
    ev = FakeEvent(" ".join(args))
    async for _ in plugin.cmd_games(ev):
        pass
    return ev.result


async def main(only=None):
    plugin = make_plugin(config=StubAstrBotConfig({"user_agent": UA}), context=FakeContext())
    # 真跑不该污染真实存储：用临时文件接住订阅与码库，跑完删掉
    plugin.store = Store(PLUGIN_DIR / ".smoke.json")
    plugin._build_sources()
    routes = [only] if only else ROUTES
    try:
        # 指令覆盖不到的两条（只走轮询/工具），单独打一遍
        if not only:
            print("\n" + "=" * 72)
            print("→ /游戏 订阅 全部（真跑：只写入内存 store）")
            print("=" * 72)
            print(await run_one(plugin, ("游戏", "订阅", "全部")))

            print("\n" + "=" * 72)
            print("→ 轮询推送预览（新公告会推到内存会话）")
            print("=" * 72)
            plugin.context.sent.clear()
            await plugin._poll_news()
            await plugin._poll_dynamics()
            for _umo, text in plugin.context.sent:
                print(text)
                print("-" * 40)
            if not plugin.context.sent:
                print("（本轮没有新内容——正常，取决于上次轮询后有没有新发布）")

            print("\n" + "=" * 72)
            print("→ AI 工具 games_dynamics")
            print("=" * 72)
            print(await plugin.tool_games_dynamics(FakeEvent("游戏")))

            print("\n" + "=" * 72)
            print("→ AI 工具 games_codes")
            print("=" * 72)
            print(await plugin.tool_games_codes(FakeEvent("游戏")))

        for args in routes:
            print("\n" + "=" * 72)
            print("→ " + " ".join(args))
            print("=" * 72)
            print(await run_one(plugin, args))
    finally:
        (PLUGIN_DIR / ".smoke.json").unlink(missing_ok=True)
        for src in list(plugin.sources.values()):
            await src.close()
        if plugin.bili:
            await plugin.bili.close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = None
    # Windows 控制台是 GBK，直接 print 中文会被 mangle 成乱码，
    # 也就看不出真实的渲染结果。给了 --out 就直接写 UTF-8 文件。
    while argv:
        if argv[0] == "--out" and len(argv) > 1:
            out = Path(argv[1])
            argv = argv[2:]
        elif argv[0] == "--only" and len(argv) > 1:
            argv = argv[1].split()
        else:
            break
    if out:
        import io

        buf = io.StringIO()
        sys.stdout = buf
        asyncio.run(main(argv or None))
        sys.stdout = sys.__stdout__
        out.write_text(buf.getvalue(), encoding="utf-8")
        print(f"written: {out}  ({len(buf.getvalue())} chars)")
    else:
        asyncio.run(main(argv or None))
