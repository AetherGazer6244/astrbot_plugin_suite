"""卡池/版本关键词分类与活动时间提取（纯函数，零 astrbot 依赖）。

词表按真实官方说法维护——方舟叫「寻访/唤醒/新干员」，1999 叫「卡池/新神秘学家」，
两边混在同一个词表里，谁命中算谁的。新说法随发现随补，不算故障。
"""

import re
from datetime import datetime

try:
    from .providers.base import fold
except ImportError:  # 兜底：被以非包方式加载时
    from providers.base import fold  # type: ignore

# 卡池相关。命中即认为这条公告讲的是卡池/UP 池。
GACHA_KEYWORDS: tuple[str, ...] = (
    "卡池",
    "寻访",
    "唤醒",
    "招募",
    "新干员",
    "新神秘学家",
    "up池",
    "up 池",
    "up主",
    "实装",
    "新增干员",
    "新增神秘学家",
    "概率公示",
    "寻访记录",
)

# 卡池相关的限时活动/事件关键词。卡池公告本身稀疏（两三个月一波），
# 关键词没命中时用它兜底给「最近的活动」，比长期空态有用。
ACTIVITY_KEYWORDS: tuple[str, ...] = (
    "限时活动",
    "活动预告",
    "活动开启",
    "活动进行",
    "开启活动",
    "side story",
    "sidestory",
    "主题曲",
    "特别企划",
    "联名",
    "联动",
    "签到",
    "挑战",
)

# 版本/维护/前瞻相关。
VERSION_KEYWORDS: tuple[str, ...] = (
    "版本更新",
    "更新维护",
    "维护公告",
    "停机维护",
    "闪断更新",
    "更新公告",
    "前瞻",
    "制作组通讯",
    "制作组播报",
    "特别企划",
    "版本预告",
    "开发组",
    "新版本",
    "公测",
    "上线时间",
)

# 时间段的原子：日期 + 可选时间。年份可选，冒号允许全角。
# 每个原子固定产出 5 个捕获组（年/月/日/时/分），RANGE_RE 靠这个下标切片还原左右两段——
# 所以外面必须用非捕获组包裹，多一个括号就会把下标整体错位（v1.0.0 真跑才抓到）。
_DATE = r"(?:(\d{4})\s*年)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
_TIME = r"\s*(\d{1,2})\s*[:：]\s*(\d{2})"
_ATOM = rf"{_DATE}(?:{_TIME})?"
# 分隔符：波浪号/短横/中文「至到」。中文词要求两侧都是日期原子，不会误伤正文。
_SEP = r"\s*(?:-|~|～|–|—|至|到)\s*"

RANGE_RE = re.compile(rf"(?:{_ATOM}){_SEP}(?:{_ATOM})")
# 同一天的纯时间区间：方舟维护公告真实写法「2026年10月16日16:00 ~ 16:10」。
# 右边没有日期，所以要单开一个模式，不能塞进 RANGE_RE。
SAMEDAY_RANGE_RE = re.compile(rf"{_DATE}{_TIME}{_SEP}(\d{{1,2}})\s*[:：]\s*(\d{{2}})")
SINGLE_RE = re.compile(rf"{_DATE}(?:{_TIME})?")

# 时间段的引导词分两组——**必须分开**。维护窗口（「维护时间：2026年9月24日05:00前后」）
# 不是活动时间，混在一张表里会让 /游戏 X 卡池 把停服时间当成卡池时间推给用户。
ACTIVITY_CUES: tuple[str, ...] = (
    "活动时间",
    "开启时间",
    "开放时间",
    "举办时间",
    "持续时间",
    "起止时间",
    "开启期间",
    "限时活动",
    "开放",
    "开启",
    "持续",
    "时间：",
    "时间:",
)

MAINTENANCE_CUES: tuple[str, ...] = (
    "维护时间",
    "更新时间",
    "闪断",
    "停机",
    "维护",
    # 刻意**不含** "时间："——「活动时间：」也含这两个字，
    # 放进来会让维护视图把活动时间也认领走。兜底的单点时间在活动那组里。
)

# 默认两组都认（调用方不确定场景时用这个）
CUE_WORDS: tuple[str, ...] = tuple(dict.fromkeys(ACTIVITY_CUES + MAINTENANCE_CUES))

# 维护类标题：卡池/活动视图要把它们排除掉，那些归 /游戏 X 版本 管
MAINTENANCE_TITLE_RE = re.compile(r"(维护|闪断|停机|更新公告|故障|补偿)")


def classify(text: str, keywords: tuple[str, ...]) -> bool:
    """文本是否命中任一关键词（大小写不敏感）。"""
    if not text:
        return False
    haystack = str(text).lower()
    return any(k.lower() in haystack for k in keywords)


def is_gacha(text: str) -> bool:
    return classify(text, GACHA_KEYWORDS)


def is_version(text: str) -> bool:
    return classify(text, VERSION_KEYWORDS)


def _fmt(atom: tuple) -> str:
    """把正则捕获组还原成 "M月D日 HH:MM" / "M月D日"。"""
    year, month, day, hour, minute = atom
    head = f"{int(month)}月{int(day)}日"
    if hour and minute:
        head += f" {int(hour):02d}:{minute}"
    if year:
        head = f"{int(year)}年" + head
    return head


def _atoms(m) -> tuple:
    """从 RANGE_RE 的匹配里切出左/右两段（各 5 组：年/月/日/时/分）。"""
    groups = m.groups()
    return tuple(groups[:5]), tuple(groups[5:10])


def _atom_ts(atom: tuple, year_hint: int) -> float | None:
    """把捕获组还原成 Unix 秒。年份缺失用 year_hint 补。非法日期（2月30日等）返回 None。"""
    year, month, day, hour, minute = atom
    try:
        dt = datetime(int(year) if year else year_hint, int(month), int(day))
        if hour and minute:
            dt = dt.replace(hour=int(hour), minute=int(minute))
        return dt.timestamp()
    except ValueError:
        return None


# 时间段合理性窗口（真实度校验，v1.3.0）：解析出来的时间落在
# [发布-30天, 发布+2年] 之外就丢弃——发布前 30 天的宽容度涵盖「预告稿」，
# +2 年封顶拦住「把『10月16日』配错年份」这类解析事故，宁可少给不给错的。
RANGE_PAST_GRACE_SECONDS = 30 * 86400
RANGE_FUTURE_CAP_SECONDS = 730 * 86400


def _plausible(ts: float | None, published: float) -> bool:
    if ts is None:
        return False  # 非法日期（2月30日）不输出
    if published <= 0:
        return True  # 没有发布时间可对照时不过滤（旧数据兼容）
    return (published - RANGE_PAST_GRACE_SECONDS) <= ts <= (published + RANGE_FUTURE_CAP_SECONDS)


def extract_time_ranges(
    text: str,
    limit: int = 2,
    require_cue: bool = True,
    cues: tuple[str, ...] | None = None,
    published: float = 0.0,
) -> list[str]:
    """从正文抽出时间段，返回形如 "10月16日 16:00 - 10月30日 03:59" 的列表。

    cues 决定认哪些引导词：卡池视图传 ACTIVITY_CUES（别把停服时间当活动时间），
    版本视图传 MAINTENANCE_CUES，不传则两组都认。
    require_cue=False 时不要求引导词，供已经确定场景的调用方使用。
    published 是公告发布时刻：超出合理性窗口的解析结果直接丢弃
    （真实度校验，v1.3.0）——错配年份的时间宁可不给，不能硬塞给用户。
    """
    if not text:
        return []
    cue_set = cues if cues is not None else CUE_WORDS
    blob = str(text)
    year_hint = datetime.fromtimestamp(published).year if published > 0 else datetime.now().year
    found: list[str] = []
    for m in RANGE_RE.finditer(blob):
        if require_cue and not _near_cue(blob, m.start(), m.end(), cue_set):
            continue
        left, right = _atoms(m)
        if published > 0 and not (
            _plausible(_atom_ts(left, year_hint), published)
            and _plausible(_atom_ts(right, year_hint), published)
        ):
            continue
        rendered = f"{_fmt(left)} - {_fmt(right)}"
        if rendered not in found:
            found.append(rendered)
        if limit and len(found) >= limit:
            return found
    # 同一天的时间区间（维护公告）：「2026年10月16日16:00 ~ 16:10」
    for m in SAMEDAY_RANGE_RE.finditer(blob):
        if require_cue and not _near_cue(blob, m.start(), m.end(), cue_set):
            continue
        head_atom = tuple(m.groups()[:5])
        if published > 0 and not _plausible(_atom_ts(head_atom, year_hint), published):
            continue
        head = _fmt(head_atom)
        rendered = f"{head} - {int(m.group(6)):02d}:{m.group(7)}"
        if rendered not in found:
            found.append(rendered)
        if limit and len(found) >= limit:
            return found
    if found or limit == 0:
        return found
    # 没有成对的时间段就退到单个时间点（维护公告常见：「将于2026年10月16日16:00更新」）
    for m in SINGLE_RE.finditer(blob):
        if require_cue and not _near_cue(blob, m.start(), m.end(), cue_set):
            continue
        atom = tuple(m.groups())
        if published > 0 and not _plausible(_atom_ts(atom, year_hint), published):
            continue
        rendered = _fmt(atom)
        if rendered not in found:
            found.append(rendered)
        if len(found) >= limit:
            break
    return found


def _near_cue(blob: str, start: int, end: int, cues: tuple[str, ...], window: int = 24) -> bool:
    """时间段前后 window 个字符内是否有引导词。

    前后都看：方舟维护公告的写法是「服务器将于{时间}期间进行闪断更新」，
    引导词（闪断）在时间之后，只看前面会漏掉这类最常见的维护通知。
    """
    head = blob[max(0, start - window) : start]
    tail = blob[end : end + window]
    return any(cue in head or cue in tail for cue in cues)


def pick(items, keywords: tuple[str, ...], limit: int = 5):
    """从 NewsItem 列表里挑出命中关键词的条目（按发布时间倒序，最多 limit 条）。

    **先只看标题，全都不命中才退到全文**。真跑发现的坑：整篇公告正文里
    到处是「开启」「联动」「联名」，拿全文匹配会把「1999制作组播报」这种
    根本不是活动的东西选进活动列表（它们只是正文里提了一嘴）。
    标题匹配才是"这条公告讲的是这件事"的证据。
    """
    title_hit = [i for i in items if classify(i.title, keywords)]
    pool = title_hit or [i for i in items if classify(i.text_blob, keywords)]
    pool = sorted(pool, key=lambda i: i.published, reverse=True)
    return pool[:limit]


# ==================== 指令路由 ====================

# /游戏 的二级指令与别名。值内部按「显示名 → 用户可能写的各种说法」组织。
# ⚠️ 别名一律先经 fold() 归一再比对，因此 "Help"/"HELP"/"帮助 " 都能命中。
SUB_COMMANDS: dict[str, tuple[str, ...]] = {
    "帮助": ("帮助", "help", "菜单", "?", "？"),
    "总结": ("总结", "摘要", "要点", "summary", "digest"),
    "列表": ("列表", "原始", "详情", "list", "raw"),
    "卡池": ("卡池", "卡", "gacha", "up池", "up"),
    "兑换": ("兑换", "兑换码", "码", "code", "cdk"),
    "版本": ("版本", "更新", "维护", "version"),
    "订阅": ("订阅", "推送", "sub", "subscribe"),
    "静音": ("静音", "免打扰", "闭嘴", "mute"),
}

# 订阅的第二级参数
SUB_ALL = ("全部", "全", "所有", "all", "both")
SUB_OFF = ("关闭", "取消", "退订", "off", "none", "no")

# 别名按长度倒序扫，避免 "码" 抢在 "兑换码" 前面先命中。
_FLAT_SUB_ALIASES: list[tuple[str, str]] = sorted(
    ((alias, sub) for sub, aliases in SUB_COMMANDS.items() for alias in aliases),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

_ALL_SUB_ALIASES = {fold(alias) for alias, _sub in _FLAT_SUB_ALIASES}


def parse_route(text: str) -> tuple[str, str]:
    """把 "/游戏 1999 卡池" 的参数部分拆成 (子指令, 剩余游戏名)。

    - 没认出子指令 → 子指令为空串，走默认「最新资讯」视图
    - 认出了但没剩游戏名 → 游戏名为空，调用方按「两游戏合并」处理
    - 不报错：认不出游戏名不是用户的错，交给上层给一句提示即可

    ⚠️ 必须先按空白切词、再逐词 fold：fold 会吃掉空格（游戏名里的空格是噪声），
    先整体 fold 会把 "1999 卡池" 压成一个词，子指令就认不出来了。
    """
    tokens = [t for t in (fold(w) for w in str(text or "").split()) if t]
    if not tokens:
        return "", ""
    for token in tokens:
        for alias, sub in _FLAT_SUB_ALIASES:
            if alias in token:
                # 子命令可能和游戏名挤在一个词里（"方舟版本"），
                # 所以是「摘掉别名」而不是「丢掉整个词」
                leftover = token.replace(alias, "", 1).strip()
                if leftover in _ALL_SUB_ALIASES:
                    leftover = ""  # "版本更新" 摘完剩 "更新"，那还是子命令
                rest = [leftover] + [t for t in tokens if t is not token]
                return sub, " ".join(x for x in rest if x)
    return "", " ".join(tokens)


def is_subscribe_all(text: str) -> bool:
    return fold(text) in {fold(a) for a in SUB_ALL}


def is_subscribe_off(text: str) -> bool:
    return fold(text) in {fold(a) for a in SUB_OFF}

