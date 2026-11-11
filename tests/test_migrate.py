"""modules/migrate.py：旧单体插件 → 套件的一次性迁移。

红线：
- 幂等：迁移过的旧文件改名 .migrated-*，第二次启动不再动
- 套件里已有的非空值优先（重装/手工配置过时旧值不回写覆盖）
- 数据文件只复制不改原件（回滚底）
"""

import json

from astrbot_plugin_suite.modules import migrate


class _Root:
    def __init__(self, data=None):
        self.data = dict(data or {"electric": {}, "games": {}})
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __setitem__(self, key, value):
        self.data[key] = value

    def save_config(self):
        self.saved += 1


# ================= 配置迁移 =================


def test_migrate_configs_merges_and_renames(tmp_path):
    legacy = {"hjnu_cookie": "JSESSIONID=old", "daily_report": True}
    (tmp_path / "astrbot_plugin_dorm_electric_config.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    root = _Root()
    notes = migrate.migrate_configs(root, tmp_path)
    assert root.data["electric"]["hjnu_cookie"] == "JSESSIONID=old"
    assert root.data["electric"]["daily_report"] is True
    assert root.saved == 1
    assert any("astrbot_plugin_dorm_electric_config.json" in n for n in notes)
    renamed = list(tmp_path.glob("astrbot_plugin_dorm_electric_config.json.migrated-*"))
    assert len(renamed) == 1, "旧配置文件必须改名留底"
    assert json.loads(renamed[0].read_text(encoding="utf-8")) == legacy


def test_migrate_configs_is_idempotent(tmp_path):
    (tmp_path / "astrbot_plugin_dorm_electric_config.json").write_text(
        json.dumps({"hjnu_cookie": "x"}), encoding="utf-8"
    )
    root = _Root()
    migrate.migrate_configs(root, tmp_path)
    saved_after_first = root.saved
    notes = migrate.migrate_configs(root, tmp_path)
    assert notes == []
    assert root.saved == saved_after_first, "第二次迁移不应再写盘"


def test_migrate_configs_keeps_nonempty_existing_values(tmp_path):
    (tmp_path / "astrbot_plugin_dorm_electric_config.json").write_text(
        json.dumps({"hjnu_cookie": "old", "poll_interval_minutes": 20}),
        encoding="utf-8",
    )
    root = _Root({"electric": {"hjnu_cookie": "new", "poll_interval_minutes": None}})
    migrate.migrate_configs(root, tmp_path)
    assert root.data["electric"]["hjnu_cookie"] == "new", "套件已有非空值不许被旧值覆盖"
    assert root.data["electric"]["poll_interval_minutes"] == 20, "空位由旧值补上"


def test_migrate_configs_survives_corrupt_json(tmp_path):
    (tmp_path / "astrbot_plugin_dorm_electric_config.json").write_text(
        "{not json", encoding="utf-8"
    )
    root = _Root()
    notes = migrate.migrate_configs(root, tmp_path)
    assert any("读取失败" in n for n in notes)
    assert root.data["electric"] == {}
    # 损坏文件没有被改名（留着让人看现场）
    assert (tmp_path / "astrbot_plugin_dorm_electric_config.json").exists()


def test_migrate_configs_both_modules(tmp_path):
    (tmp_path / "astrbot_plugin_dorm_electric_config.json").write_text(
        json.dumps({"hjnu_cookie": "x"}), encoding="utf-8"
    )
    (tmp_path / "astrbot_plugin_games_monitor_config.json").write_text(
        json.dumps({"push_enabled": False}), encoding="utf-8"
    )
    root = _Root()
    migrate.migrate_configs(root, tmp_path)
    assert root.data["electric"]["hjnu_cookie"] == "x"
    assert root.data["games"]["push_enabled"] is False


def test_migrate_configs_noop_without_legacy_files(tmp_path):
    root = _Root()
    assert migrate.migrate_configs(root, tmp_path) == []
    assert root.saved == 0


# ================= 数据迁移 =================


def test_migrate_data_copies_and_keeps_original(tmp_path):
    src_dir = tmp_path / "plugin_data" / "astrbot_plugin_dorm_electric"
    src_dir.mkdir(parents=True)
    (src_dir / "history.json").write_text('{"bindings": {}}', encoding="utf-8")
    notes = migrate.migrate_data(tmp_path)
    dst = tmp_path / "plugin_data" / "astrbot_plugin_suite" / "electric" / "history.json"
    assert dst.is_file()
    assert json.loads(dst.read_text(encoding="utf-8")) == {"bindings": {}}
    assert (src_dir / "history.json").is_file(), "原件必须保留（回滚底）"
    assert any("history.json" in n for n in notes)


def test_migrate_data_does_not_overwrite_existing_target(tmp_path):
    src = tmp_path / "plugin_data" / "astrbot_plugin_dorm_electric" / "history.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"v": 1}', encoding="utf-8")
    dst = tmp_path / "plugin_data" / "astrbot_plugin_suite" / "electric" / "history.json"
    dst.parent.mkdir(parents=True)
    dst.write_text('{"v": 2}', encoding="utf-8")
    notes = migrate.migrate_data(tmp_path)
    assert json.loads(dst.read_text(encoding="utf-8")) == {"v": 2}, "目标已存在不许覆盖"
    assert not any("history.json" in n for n in notes)


def test_migrate_data_noop_without_sources(tmp_path):
    assert migrate.migrate_data(tmp_path) == []


def test_migrate_data_covers_both_modules(tmp_path):
    for rel in (
        "plugin_data/astrbot_plugin_dorm_electric/history.json",
        "plugin_data/astrbot_plugin_games_monitor/store.json",
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    notes = migrate.migrate_data(tmp_path)
    assert (tmp_path / "plugin_data/astrbot_plugin_suite/electric/history.json").is_file()
    assert (tmp_path / "plugin_data/astrbot_plugin_suite/games/store.json").is_file()
    assert len(notes) == 2
