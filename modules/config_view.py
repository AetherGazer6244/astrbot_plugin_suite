"""配置视图：把套件根配置的某个嵌套分组暴露成「扁平 dict」语义。

宿主注入的 AstrBotConfig 是整份套件配置（modules / electric / games / memo
四个分组），而各模块 Core 历史上按自己的扁平键读写（如 hjnu_cookie、
bilibili_sessdata）。本类让 Core 保持扁平语义不变，读写落到根配置对应
分组；save_config() 透传给根配置（宿主负责持久化）。

语义刻意与 dict.get 完全一致（不做 None/"" 折叠）——各模块 Core 的
_cfg/_cfg_int 等助手自己处理默认值折叠，测试桩（StubAstrBotConfig）也是
纯 dict 语义，两边必须同构。
"""

_MISSING = object()


class SectionConfig:
    """root 配置里 section 分组的扁平视图。"""

    def __init__(self, root, section: str) -> None:
        self._root = root
        self._section = section

    def _items(self) -> dict:
        try:
            sec = self._root.get(self._section)
        except (AttributeError, TypeError):
            return {}
        return sec if isinstance(sec, dict) else {}

    def get(self, key, default=None):
        value = self._items().get(key, _MISSING)
        if value is _MISSING:
            return default
        return value

    def __getitem__(self, key):
        return self._items()[key]

    def __setitem__(self, key, value) -> None:
        items = dict(self._items())
        items[key] = value
        self._root[self._section] = items

    def __contains__(self, key) -> bool:
        return key in self._items()

    def save_config(self) -> None:
        method = getattr(self._root, "save_config", None)
        if callable(method):
            method()
