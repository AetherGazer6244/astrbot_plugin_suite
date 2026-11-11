"""AI 摘要生成（后台预计算，前台秒取）。

架构（用户决策）：AI 在后台「实时」分析——每轮资讯轮询发现公告变化就调 LLM
预生成要点摘要，写进缓存；前台 /游戏 直接读缓存，一次 LLM 都不调、秒回。
生成失败不缓存，下一轮轮询自动重试，绝不让前台拿到半成品。

本模块保持零 astrbot 依赖：prompt 组装、结果洗涤是纯函数，text_chat 调用
只依赖宿主 Provider 的鸭子类型（text_chat(prompt=...) → LLMResponse），
便于用假 provider 单测。
"""

import asyncio
import re

try:
    from .formatting import fmt_date
    from .providers.base import strip_html
except ImportError:  # 兜底：被以非包方式加载时
    from formatting import fmt_date  # type: ignore
    from providers.base import strip_html  # type: ignore

# 喂给 LLM 的每条公告正文上限（字符）。公告全文动辄上千字，摘要只需要事实与时间，
# 全文灌进去 token 翻倍、要点密度反而下降。
MAX_CHARS_PER_ITEM = 400

# 后台生成的单次超时（秒）。deepseek 系出要点通常 <20s；超时算失败，
# 不缓存、下轮重试。不要设太长——轮询协程虽然在 spawn 后不等它，
# 推送路径的 _ensure_digest 会等这个任务，拖太久推送就钝了。
SUMMARIZE_TIMEOUT_SECONDS = 60.0

# 前台 /游戏 等在途生成任务的时间。缓存命中根本不走这里；
# 只有「刚发完公告就有人来问」的窗口期才会等，等不到就回退原始列表。
DIGEST_WAIT_SECONDS = 20.0

# 推送路径等生成的时间。推送本来就接受几秒~十几秒延迟（用户拍板过），
# 给比前台更长的窗口，宁可推送慢一点也不愿退回纯列表。
PUSH_DIGEST_WAIT_SECONDS = 45.0

# 生成结果防御性洗 URL——提示词里禁了也不全信，模型偶尔会复读链接。
URL_RE = re.compile(r"https?://\S+")

SYSTEM_PROMPT = (
    "你是游戏资讯助手，负责把官方公告提炼成简明要点。"
    "你只依据用户给出的公告内容作答，绝不编造公告、时间或数字。"
)

DIGEST_INSTRUCTIONS = """

请基于以上公告输出一份要点摘要，要求：
1. 中文，3~6 条，每条一行，以「- 」开头
2. 合并同类公告（多条维护/闪断公告合并为一条）
3. 提取具体事实与时间：维护窗口、活动起止时间、联动对象、卡池/新角色名称
4. 只使用上面给出的信息，严禁编造公告内容、时间或数字
5. 不要输出任何链接或网址
6. 如果公告里没有有价值的信息，只输出「近期没有重要公告」
"""


def build_digest_prompt(game_label: str, items, max_chars: int = MAX_CHARS_PER_ITEM) -> str:
    """把公告列表编成编号清单。URL 一律不带——喂给模型的就不能有，输出才不会有。"""
    lines = [f"以下是《{game_label}》官方最近的公告：", ""]
    for idx, item in enumerate(items, 1):
        date = fmt_date(item.published, with_time=False)
        lines.append(f"{idx}. [{item.category}] {wash(item.title)}（{date}）")
        body = wash(strip_html(item.content or item.summary or "", limit=max_chars + 200))
        if body:
            lines.append(f"   正文：{body[:max_chars]}")
    lines.append(DIGEST_INSTRUCTIONS)
    return "\n".join(lines)


def wash(text: str) -> str:
    """洗涤 LLM 输出：去掉 URL、压掉多余空白。保留行结构（要点一行一条）。"""
    flat = URL_RE.sub("", str(text or ""))
    lines = [" ".join(line.split()) for line in flat.splitlines()]
    return "\n".join(line for line in lines if line)


def cache_key(items) -> tuple:
    """摘要缓存键：公告 id 元组。列表没变就不重算，变了自动失效。"""
    return tuple(str(i.item_id) for i in items)


async def summarize(provider, prompt: str, timeout: float = SUMMARIZE_TIMEOUT_SECONDS) -> str:
    """调一次 LLM 生成摘要。空回复与超时都按失败抛出（调用方决定怎么兜底）。"""
    resp = await asyncio.wait_for(
        provider.text_chat(prompt=prompt, session_id=None, system_prompt=SYSTEM_PROMPT),
        timeout=timeout,
    )
    text = wash(str(getattr(resp, "completion_text", "") or ""))
    if not text:
        raise ValueError("LLM 返回了空摘要")
    return text
