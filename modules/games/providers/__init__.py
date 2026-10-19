"""资讯数据源包：结构定义、游戏名归一、注册表。

新增游戏 = 在 providers/ 下加一个实现 GameSource 的模块，在 GAMES 里注册一条，
再在 _conf_schema.json 里加一个 <key>_enabled 开关。main.py 不需要改。
"""

import unicodedata  # noqa: F401 - re-export 兼容

from .arknights import ArknightsSource
from .base import CodeHit, GameSource, NewsItem, SourceError, fold, strip_html
from .bilibili import BilibiliClient, BilibiliDynamics, BilibiliLogin, DynamicItem
from .http_client import HttpClient
from .re1999 import Re1999Source

# 游戏名 → 内部 key。键先经 _fold 归一（全角转半角、转小写、去空白与分隔符）
# 再查表，这样「重返未来 1999」「RE1999」「重返未来1999」都能命中。
GAME_ALIASES: dict[str, tuple[str, ...]] = {
    "re1999": (
        "重返未来1999",
        "重返未来",
        "重返1999",
        "1999",
        "九九",
        "重返",
        "re1999",
        "reverse1999",
    ),
    "arknights": ("明日方舟", "方舟", "ak", "arknights", "ark"),
}

GAME_DISPLAY: dict[str, str] = {
    "re1999": "重返未来：1999",
    "arknights": "明日方舟",
}

# 注册表：key → 实现类。新增游戏在这里加一行即可。
GAMES: dict[str, type] = {
    "arknights": ArknightsSource,
    "re1999": Re1999Source,
}

# 默认 B站官方号 UID（经官网外链核验；方舟官方号 = 161775300，161432940 是同名的个人号）
DEFAULT_UIDS: dict[str, str] = {
    "re1999": "1197454103",
    "arknights": "161775300",
}


_FOLDED_ALIASES: dict[str, str] = {
    fold(alias): key for key, aliases in GAME_ALIASES.items() for alias in aliases
}


def normalize_game(text: str) -> str | None:
    """把用户随手写的游戏名归一到内部 key；认不出返回 None（不是错误）。

    先整串精确匹配，再退到「包含」匹配——用户说「1999的卡池」这类带后缀的
    也能认出来。包含匹配按别名长度从长到短试，避免短别名抢在长别名前面命中。
    """
    folded = fold(text)
    if not folded:
        return None
    hit = _FOLDED_ALIASES.get(folded)
    if hit:
        return hit
    for alias in sorted(_FOLDED_ALIASES, key=len, reverse=True):
        if alias in folded:
            return _FOLDED_ALIASES[alias]
    return None


def game_display(key: str) -> str:
    return GAME_DISPLAY.get(key, key)


def enabled_keys(config) -> list[str]:
    """按配置筛出启用的游戏 key（顺序稳定，便于展示）。"""
    out = []
    for key in GAMES:
        try:
            on = config.get(f"{key}_enabled", True)
        except (AttributeError, TypeError):
            on = True
        if isinstance(on, str):
            on = on.strip().lower() not in ("", "0", "false", "no", "off")
        if on:
            out.append(key)
    return out


def resolve_uids(config) -> dict[str, str]:
    """读配置里的 UID 覆盖，缺省回落到 DEFAULT_UIDS。"""
    out = dict(DEFAULT_UIDS)
    try:
        raw = config.get("bilibili_uids", None)
    except (AttributeError, TypeError):
        raw = None
    if isinstance(raw, dict):
        for key, value in raw.items():
            text = str(value or "").strip()
            if key in out and text.isdigit():
                out[key] = text
    return out


__all__ = [
    "DEFAULT_UIDS",
    "GAMES",
    "GAME_ALIASES",
    "GAME_DISPLAY",
    "ArknightsSource",
    "BilibiliClient",
    "BilibiliDynamics",
    "BilibiliLogin",
    "CodeHit",
    "DynamicItem",
    "GameSource",
    "HttpClient",
    "NewsItem",
    "Re1999Source",
    "SourceError",
    "enabled_keys",
    "fold",
    "game_display",
    "normalize_game",
    "resolve_uids",
    "strip_html",
]
