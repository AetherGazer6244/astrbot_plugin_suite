"""纯渲染函数：资讯列表、卡池、兑换码、动态、空态。

零 astrbot 依赖、零状态，所有展示口径集中在这里，方便对着真数据逐字校对
（电费插件在 v1.0.7/v1.0.8/v1.1.0 三次真跑渲染中各抓到一个只有真跑才看得见的
列表方向 bug——渲染口径必须能被单测钉住）。
"""

import datetime
import time

try:
    from .codes import expiry_note
    from .providers import game_display
    from .providers.base import strip_html
except ImportError:  # 兜底：被以非包方式加载时
    from codes import expiry_note  # type: ignore
    from providers import game_display  # type: ignore
    from providers.base import strip_html  # type: ignore

# 用户决策（v1.1.0）：聊天输出不放裸网址——AI 摘要负责提炼有效信息，
# 原始列表只留分类/标题/时间。AI 工具喂给模型的内容（summary_of）保留链接，
# 那是内部链路，用户问「给我链接」时模型仍能给。
EMPTY_CODE_TEXT = "没有兑换码QWQ"
EMPTY_NEWS_TEXT = "暂时没有查到相关资讯，稍后再试试。"
EMPTY_DYNAMIC_TEXT = "该官方号暂时没有可读取的动态。"

CATEGORY_ICON = {"公告": "📢", "活动": "🎉", "新闻": "📰", "资讯": "📄", "视频": "🎬"}
# 渠道图标（v1.3.0 真实度标注）：非官网渠道的条目在列表里标明来源渠道名，
# 用户一眼看出「这条是官网公告还是B站搬运」。官网条目 category 已足够。
SOURCE_ICON = {"B站动态": "📺", "B站视频": "🎬"}


def fmt_date(ts: float, with_time: bool = True) -> str:
    """Unix 秒 → "MM-DD" 或 "MM-DD HH:MM"。ts 异常时不炸。"""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    tm = time.localtime(value)
    if with_time:
        return time.strftime("%m-%d %H:%M", tm)
    return time.strftime("%m-%d", tm)


def fmt_ago(ts: float, now: float | None = None) -> str:
    """相对时间，用于「3 小时前」。超过一周直接给日期。"""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    now = now if now is not None else time.time()
    delta = now - value
    if delta < 0:
        return fmt_date(value)
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)} 天前"
    return fmt_date(value, with_time=False)


def one_line(text: str, limit: int = 80) -> str:
    """压成单行摘要（HTML 已在 provider 层剥过，这里只处理换行与超长）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat


def news_lines(items, with_summary: bool = True) -> list[str]:
    """资讯列表渲染。每行：分类图标 + 标题 + 日期（不含链接，用户决策）。

    非官网渠道（B站动态/视频）的条目把渠道名标进行首（真实度标注）：
    「📺 [B站动态·动态] …」太啰嗦，直接换渠道图标 + [渠道·分类]。
    """
    lines = []
    for item in items:
        src = getattr(item, "source", "") or ""
        if src in SOURCE_ICON:
            icon = SOURCE_ICON[src]
            label = f"{src}"
        else:
            icon = CATEGORY_ICON.get(item.category, "📄")
            label = item.category
        head = f"{icon} [{label}] {one_line(item.title, 40)}"
        date = fmt_date(item.published)
        lines.append(head)
        if with_summary and item.summary:
            lines.append(f"    {one_line(item.summary, 80)}")
        if date:
            lines.append(f"    {date}")
    return lines


def news_text(items, header: str, with_summary: bool = True) -> str:
    body = news_lines(items, with_summary=with_summary)
    if not body:
        return f"{header}\n{EMPTY_NEWS_TEXT}"
    return "\n".join([header, ""] + body)


def gacha_lines(items, time_ranges: dict[str, list[str]]) -> list[str]:
    """卡池视图：命中卡池词条的公告 + 抽出的活动时间段。

    time_ranges 按 item_id 索引，抽不到时间段时不硬凑——宁可少一行也不要给错时间。
    """
    lines = []
    for item in items:
        icon = CATEGORY_ICON.get(item.category, "📄")
        lines.append(f"{icon} {one_line(item.title, 44)}")
        for rng in time_ranges.get(item.item_id) or []:
            lines.append(f"    🕒 {rng}")
        if item.summary:
            lines.append(f"    {one_line(item.summary, 80)}")
        date = fmt_date(item.published)
        if date:
            lines.append(f"    {date}")
    return lines


def code_lines(records, ttl_hours: int = 24) -> list[str]:
    lines = []
    for rec in records:
        lines.append(f"🎁 {rec.get('code')}")
        note = expiry_note(rec, ttl_hours)
        source = "、".join(rec.get("sources") or [rec.get("source") or ""])
        tail = [p for p in (note, f"来源：{source}" if source else "") if p]
        lines.append("    " + " | ".join(tail))
    return lines


def codes_text(records, game_label: str, ttl_hours: int = 24) -> str:
    header = f"🎁 {game_label} 兑换码"
    if not records:
        return f"{header}\n{EMPTY_CODE_TEXT}"
    return "\n".join([header, ""] + code_lines(records, ttl_hours))


def dynamic_lines(items) -> list[str]:
    """动态渲染。链接一律不放（用户决策）；没标题也没正文的条目整条跳过——
    B站对未登录请求会把纯图片动态的文字裁掉（实测 major.opus 返回 null），
    这种条目去掉链接后什么都剩不下，渲染出来只是空壳噪音。
    """
    lines = []
    for item in items:
        title = one_line(item.title, 40) if item.title else ""
        body = one_line(item.text, 100)
        if not title and not body:
            continue
        head = f"📣 [{item.kind}] {title}" if title else f"📣 [{item.kind}]"
        lines.append(head)
        if body:
            lines.append(f"    {body}")
        ago = fmt_ago(item.published)
        if ago:
            lines.append(f"    {ago}")
    return lines


def dynamics_text(items, game_label: str) -> str:
    header = f"📣 {game_label} B站官方动态"
    if not items:
        return f"{header}\n{EMPTY_DYNAMIC_TEXT}"
    return "\n".join([header, ""] + dynamic_lines(items))


def push_text(items) -> str:
    """轮询发现新条目时的兜底推送文案（AI 摘要不可用时才用，紧凑优先）。"""
    lines = []
    for item in items:
        icon = CATEGORY_ICON.get(item.category, "📄")
        lines.append(f"{icon} {one_line(item.title, 44)}")
        date = fmt_date(item.published)
        if date:
            lines.append(f"    {date}")
    return "\n".join(lines)


def summary_of(item, limit: int = 80) -> str:
    """给 AI 工具用的摘要：标题 + 纯文本摘要 + 链接。"""
    parts = [one_line(item.title, 60)]
    body = strip_html(item.content or item.summary, limit=limit)
    if body:
        parts.append(one_line(body, limit))
    parts.append(item.url)
    return "\n".join(parts)


def help_text(enabled, poll_min: int, dynamics_min: int, push_on: bool) -> str:
    """帮助文案。轮询频率等读配置显示，免得写了默认值和实际不符。"""
    game_line = (
        "、".join(game_display(k) for k in enabled) if enabled else "（未启用任何数据源）"
    )
    push_line = "已开启" if push_on else "未开启（用 /游戏 订阅 开启本会话推送）"
    return "\n".join(
        [
            "🎮 游戏资讯监控",
            "",
            f"· 数据源：{game_line}",
            f"· 资讯轮询：{poll_min} 分钟｜动态轮询：{dynamics_min} 分钟",
            "· AI 摘要：后台自动生成，/游戏 秒出资讯要点",
            f"· 群订阅推送：{push_line}",
            "",
            "指令：",
            "  /游戏            两游戏 AI 要点摘要",
            "  /游戏 <游戏名>   指定游戏的 AI 摘要",
            "  /游戏 列表        原始公告列表（不带 AI，可加游戏名）",
            "  /游戏 <游戏名> 卡池   卡池公告 + 活动时间",
            "  /游戏 <游戏名> 兑换   兑换码（别名：码/兑换码）",
            "  /游戏 <游戏名> 版本   版本更新与维护公告",
            "  /游戏 订阅 <游戏|全部|关闭>   开关本会话推送（群内仅管理员）",
            "  /游戏 静音 [小时]   暂时不推送（无参看状态；0/解除=恢复；群内仅管理员）",
            "  /游戏 帮助       本说明",
            "",
            "游戏名可以写：重返未来/1999/九九/重返、明日方舟/方舟/ak。",
            "也可以直接问我：「1999 最近有什么卡池」「方舟兑换码」。",
        ]
    )


def subscribe_text(action: str, detail: str) -> str:
    return f"🔔 {action}\n{detail}"


def mute_text(action: str, detail: str) -> str:
    return f"🔕 {action}\n{detail}"


def format_mute_until(until: float) -> str:
    """静音截止时间戳 → 「MM-DD HH:MM」。纯函数便于单测。"""
    if until <= 0:
        return ""
    return datetime.datetime.fromtimestamp(until).strftime("%m-%d %H:%M")


def scope_text(subs) -> str:
    """订阅状态视图。"""
    if not subs:
        return "本会话未订阅任何推送。\n发送 /游戏 订阅 全部 开启，或 /游戏 订阅 <游戏名> 单独订阅。"
    games = "、".join(game_display(g) for g in subs.get("games") or []) or "（无）"
    dyn = "开" if subs.get("dynamics") else "关"
    return "\n".join(
        [
            "🔔 本会话订阅状态",
            f"· 游戏：{games}",
            f"· B站动态推送：{dyn}",
            "",
            "发送 /游戏 订阅 关闭 可取消。",
        ]
    )
