"""AI 要点摘要：后台预生成，前台秒取（从 service.py 迁出，含 v1.2.0 落盘）。

架构（用户拍板）：AI 在后台实时分析——轮询发现公告变化就调 LLM 写缓存，
前台 /游戏 与推送只读缓存。生成失败不缓存，下一轮自动重试。

v1.2.0 起**摘要落盘**（store 的 digests 组）：重启后缓存不再清零，
用户不用等下一轮轮询重新生成；key 是「最新 N 条公告的 id 元组」，
落盘前已含排序归一（见 _digest_of 的注释），重启后同一批公告直接命中。

Mixin 形态：摘要逻辑依赖 self 的配置读取/存储/缓存字段，但不带任何
@filter 装饰器——宿主只按「装饰器函数的定义模块」归属 handler，
Mixin 不影响注册面（注册面测试锁死这一约束）。
"""

import asyncio
import logging

logger = logging.getLogger("astrbot.plugin.games_monitor")

try:
    from .summarizer import build_digest_prompt, cache_key, summarize
except ImportError:  # 兜底：被以非包方式加载时
    from summarizer import build_digest_prompt, cache_key, summarize  # type: ignore

try:
    from .providers import game_display
except ImportError:
    from providers import game_display  # type: ignore


class DigestMixin:
    """AI 要点摘要的生成与缓存。宿主 GamesCore 需提供：

    - self.context（宿主 Context，取对话模型）
    - self.store（Store 或 None）
    - self._cfg_bool/_cfg_int（配置读取）
    - self._digest_cache / self._digest_tasks（由 __init__ 建好）
    """

    def _digest_enabled(self) -> bool:
        return self._cfg_bool("ai_summary_enabled", True)

    def _digest_max_items(self) -> int:
        return max(3, min(20, self._cfg_int("ai_summary_max_items", 8)))

    def _digest_provider(self):
        """后台生成统一用宿主默认配置档当前选中的对话模型。

        后台任务没有会话上下文，会话级偏好模型在这里不适用；
        想换摘要模型 = 在 WebUI 默认配置档里切换模型。
        """
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            return None
        try:
            return getter(None)
        except Exception as e:
            logger.warning(f"取对话模型失败：{e!r}")
            return None

    def _restore_digests(self) -> None:
        """把落盘的摘要装回内存缓存（启动时调用一次）。

        key 一致才命中：落盘时的公告列表与现在不同的话，_digest_of 会
        自然判 miss 并触发重新生成，不会给出过期摘要。
        """
        if not self.store:
            return
        try:
            for key, entry in self.store.all_digests().items():
                if entry is not None and key not in self._digest_cache:
                    self._digest_cache[key] = entry
        except Exception as e:
            logger.warning(f"恢复落盘摘要失败（忽略，等轮询重新生成）：{e!r}")

    def _digest_of(self, key: str, items: list) -> str | None:
        """读缓存：id 元组完全一致才算命中，公告列表变了就当没有。

        ⚠️ 排序必须在这里做（而不是指望调用方排过）：轮询传进来的 items 是
        数据源原始顺序，视图/推送传的是时间倒序——真跑渲染抓到过两边 key
        不一致导致缓存永远 miss、LLM 被重复调用一倍的 bug。
        """
        entry = self._digest_cache.get(key)
        if not entry:
            return None
        picked = sorted(items, key=lambda i: i.published, reverse=True)
        picked = picked[: self._digest_max_items()]
        return entry[1] if entry[0] == cache_key(picked) else None

    def _spawn_digest(self, key: str, items: list) -> None:
        """后台生成。每游戏同时最多一个在途任务，重复触发直接复用。"""
        existing = self._digest_tasks.get(key)
        if existing and not existing.done():
            return
        self._digest_tasks[key] = asyncio.create_task(self._generate_digest(key, items))

    def _maybe_refresh_digest(self, key: str, items: list) -> None:
        """轮询钩子：缓存过期（公告变化/首启/上次失败）就 spawn 后台生成，绝不阻塞轮询。"""
        if not self._digest_enabled() or not items:
            return
        if self._digest_of(key, items) is not None:
            return
        self._spawn_digest(key, items)

    async def _generate_digest(self, key: str, items: list) -> None:
        picked = sorted(items, key=lambda i: i.published, reverse=True)
        picked = picked[: self._digest_max_items()]
        provider = self._digest_provider()
        if provider is None:
            logger.info("AI 摘要跳过：没有可用的对话模型")
            return
        prompt = build_digest_prompt(game_display(key), picked)
        try:
            digest = await summarize(provider, prompt)
        except Exception as e:
            # 失败不缓存 → 下一轮轮询自动重试；前台/推送各自兜底
            logger.warning(f"{game_display(key)} AI 摘要生成失败：{e!r}")
            return
        self._digest_cache[key] = (cache_key(picked), digest)
        # 落盘（去抖即可：重启丢了也就是多等一轮轮询重新生成，不值得 force）
        if self.store:
            try:
                self.store.set_digest(key, cache_key(picked), digest)
                self.store.save()
            except Exception as e:
                logger.warning(f"{game_display(key)} AI 摘要落盘失败（内存缓存仍在）：{e!r}")
        logger.info(f"{game_display(key)} AI 摘要已更新（{len(picked)} 条公告）")

    async def _ensure_digest(self, key: str, items: list, wait_seconds: float) -> str | None:
        """查询/推送路径：缓存命中直接回；没命中就在途任务上有限时等待。

        等不到（超时/失败/没配模型）返回 None，调用方回退原始列表。
        超时不取消生成任务本身（shield）——它继续在后台跑完写缓存，
        用户「稍后再发一次 /游戏」就能拿到。
        """
        if not self._digest_enabled():
            return None
        picked = sorted(items, key=lambda i: i.published, reverse=True)
        picked = picked[: self._digest_max_items()]
        cached = self._digest_of(key, picked)
        if cached is not None:
            return cached
        self._spawn_digest(key, picked)
        task = self._digest_tasks.get(key)
        if task is None:
            return None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        except Exception:
            return None
        return self._digest_of(key, picked)
