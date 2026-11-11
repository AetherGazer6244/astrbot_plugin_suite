"""旧单体插件 → 套件的一次性迁移（幂等；保留原件可回滚）。

- 配置：data/config/astrbot_plugin_{dorm_electric,games_monitor}_config.json
  里的扁平键 → 套件配置对应分组（electric / games）。迁移后旧文件改名
  *.migrated-<时间戳>（内容原样保留），既防止重复迁移，也留回滚底。
- 数据：data/plugin_data/ 下旧插件的 history.json / store.json → 套件数据
  目录的 electric/ games/ 子目录（只在目标不存在时复制，原件不动）。

所有函数都是纯函数（显式传入路径/配置对象），可离线单测。
"""

import json
import shutil
import time
from pathlib import Path

# (旧配置文件名, 套件配置分组)
LEGACY_CONFIGS: tuple[tuple[str, str], ...] = (
    ("astrbot_plugin_dorm_electric_config.json", "electric"),
    ("astrbot_plugin_games_monitor_config.json", "games"),
)

# (旧数据文件相对 data 目录的路径, 套件内新相对路径)
LEGACY_DATA: tuple[tuple[str, str], ...] = (
    (
        "plugin_data/astrbot_plugin_dorm_electric/history.json",
        "plugin_data/astrbot_plugin_suite/electric/history.json",
    ),
    (
        "plugin_data/astrbot_plugin_games_monitor/store.json",
        "plugin_data/astrbot_plugin_suite/games/store.json",
    ),
)


def migrate_configs(root, config_dir) -> list[str]:
    """把旧插件配置合并进套件配置分组。返回给用户日志看的迁移说明列表。"""
    notes: list[str] = []
    changed = False
    for filename, section in LEGACY_CONFIGS:
        src = Path(config_dir) / filename
        if not src.is_file():
            continue
        try:
            legacy = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            notes.append(f"旧配置 {filename} 读取失败，跳过迁移：{e!r}")
            continue
        if not isinstance(legacy, dict) or not legacy:
            continue
        try:
            current = root.get(section)
        except (AttributeError, TypeError):
            current = None
        current = current if isinstance(current, dict) else {}
        merged = dict(legacy)
        # 套件里已有的非空值优先（重装/已手工配置过时旧值不回写覆盖）
        for key in legacy:
            if key in current and current[key] not in (None, ""):
                merged[key] = current[key]
        root[section] = merged
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            src.rename(src.with_name(f"{filename}.migrated-{stamp}"))
        except OSError as e:
            notes.append(f"旧配置 {filename} 重命名失败（可能已迁移过）：{e!r}")
            continue
        changed = True
        notes.append(
            f"已迁移旧配置 {filename} → 配置分组 {section}"
            f"（{len(merged)} 项，原件保留为 .migrated-*）"
        )
    if changed:
        save = getattr(root, "save_config", None)
        if callable(save):
            save()
    return notes


def migrate_data(data_dir) -> list[str]:
    """把旧插件数据文件复制进套件数据目录（只在目标缺失时）。"""
    notes: list[str] = []
    for rel_src, rel_dst in LEGACY_DATA:
        src = Path(data_dir) / rel_src
        dst = Path(data_dir) / rel_dst
        if not src.is_file() or dst.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            notes.append(f"旧数据 {rel_src} 迁移失败：{e!r}")
            continue
        notes.append(f"已迁移旧数据 {rel_src} → {rel_dst}（原件保留，可回滚）")
    return notes
