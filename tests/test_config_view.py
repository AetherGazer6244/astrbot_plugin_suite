"""SectionConfig（modules/config_view.py）语义测试：嵌套分组的扁平 dict 视图。

与 StubAstrBotConfig 同构是硬要求——模块 Core 的 _cfg 助手与测试桩都按
dict.get 语义读写，视图必须完全一致（不做 None/"" 折叠）。
"""

import pytest
from astrbot_plugin_suite.modules.config_view import SectionConfig


class _Root:
    """带 save_config 计数的根配置替身。"""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __setitem__(self, key, value):
        self.data[key] = value

    def save_config(self):
        self.saved += 1


def test_get_reads_section_flat():
    root = _Root({"electric": {"hjnu_cookie": "JSESSIONID=x", "daily_report": True}})
    sec = SectionConfig(root, "electric")
    assert sec.get("hjnu_cookie") == "JSESSIONID=x"
    assert sec.get("daily_report") is True
    assert sec["hjnu_cookie"] == "JSESSIONID=x"


def test_get_default_and_missing_section():
    root = _Root({})
    sec = SectionConfig(root, "games")
    assert sec.get("push_enabled") is None
    assert sec.get("push_enabled", False) is False
    assert sec.get("n", 5) == 5


def test_get_does_not_fold_falsy_values():
    """0 / "" / False 是合法配置值，视图不做折叠（默认值只在键缺失时生效）。"""
    root = _Root({"electric": {"threshold_warn": 0, "http_proxy": "", "daily_report": False}})
    sec = SectionConfig(root, "electric")
    assert sec.get("threshold_warn", 10) == 0
    assert sec.get("http_proxy", "direct") == ""
    assert sec.get("daily_report", True) is False


def test_setitem_writes_into_section_and_save_passthrough():
    root = _Root({"electric": {}})
    sec = SectionConfig(root, "electric")
    sec["hjnu_cookie"] = "JSESSIONID=y"
    assert root.data["electric"]["hjnu_cookie"] == "JSESSIONID=y"
    assert sec["hjnu_cookie"] == "JSESSIONID=y"
    sec.save_config()
    assert root.saved == 1


def test_setitem_creates_missing_section():
    root = _Root({})
    sec = SectionConfig(root, "memo")
    sec["x"] = 1
    assert root.data["memo"] == {"x": 1}


def test_contains():
    root = _Root({"electric": {"a": 1}})
    sec = SectionConfig(root, "electric")
    assert "a" in sec
    assert "b" not in sec


def test_root_without_get_method():
    """根不是配置对象（裸 dict 直接当 root 传）时不炸。"""

    class Bare(dict):
        pass

    root = Bare({"games": {"k": "v"}})
    sec = SectionConfig(root, "games")
    assert sec.get("k") == "v"
    sec["k2"] = 2
    assert root["games"]["k2"] == 2
    sec.save_config()  # 无 save_config 方法也不炸


def test_section_not_dict_is_tolerated():
    root = _Root({"electric": "garbage"})
    sec = SectionConfig(root, "electric")
    assert sec.get("anything", "dft") == "dft"


@pytest.mark.parametrize("section", ["electric", "games", "memo"])
def test_sections_are_independent(section):
    root = _Root({s: {} for s in ("electric", "games", "memo")})
    SectionConfig(root, section)["only"] = section
    for s in ("electric", "games", "memo"):
        assert root.data[s].get("only") == (section if s == section else None)
