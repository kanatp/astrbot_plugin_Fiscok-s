'''
辅助调用（情绪分析 / 记忆抽取 / CLM 整理 / 日程规划 / 任务推进等）的 Provider 工具：
- 解析目标 Provider（未配置时回退到第一个可用 Provider）
- 自适应克隆独立 Provider 实例，与主对话实例隔离，避免竞态
- 智能适配思考模型（Thinking / Reasoning）：
  1. 识别纯推理模型（DeepSeek-R1、QwQ 等）不强行注入关闭参数，交由包装器自动滤除思考标签；
  2. 针对 o1/o3-mini 等不支持 "none" 的模型，采用最低思考档位 "low"；
  3. 提供自适应异常重试（Fallback）：若上游端点因思考参数报错 400，自动平滑回退到原始 Provider；
  4. 自动剔除 <think>...</think> 标签，确保下游 JSON 解析与文本处理 100% 干净自洽。
'''
from __future__ import annotations

import copy
import re
from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context

try:
    from astrbot.core.provider.register import provider_cls_map
    _PROVIDER_CLS_OK = True
except Exception:  # pragma: no cover - 宿主结构变动时的兜底
    provider_cls_map = {}
    _PROVIDER_CLS_OK = False

# 关闭思考时默认附加到请求体的参数（OpenAI 兼容家族）
_DEFAULT_THINKING_OFF_EXTRA_BODY = {"reasoning_effort": "none"}
# 这些网关用 enable_thinking 控制思考链，reasoning_effort 对它们无效
_ENABLE_THINKING_OFF_HOSTS = ("siliconflow", "dashscope", "aliyuncs")

# 思考标签正则（用于清洗模型回复中的思考过程）
_THINKING_BLOCK_RE = re.compile(
    r'<(?:think|thinking|thought)>.*?</(?:think|thinking|thought)>'
    r'|```thought\s*.*?```',
    re.DOTALL | re.IGNORECASE,
)

# 思考参数被上游拒绝的常见错误特征词
_THINKING_PARAM_ERRORS = (
    "reasoning_effort",
    "reasoning",
    "thinking",
    "enable_thinking",
    "budget",
    "extra_body",
    "unsupported parameter",
    "unknown parameter",
    "invalid parameter",
    "invalid value",
    "400",
)

_aux_provider_cache: dict = {}


class AuxProviderWrapper:
    """
    自适应辅助 Provider 包装器：
    1. 代理底层的 text_chat 调用；
    2. 若因关闭思考参数被上游 API 拒绝 (HTTP 400 / Unsupported parameter)，自动平滑降级至原 Provider 重试；
    3. 自动过滤掉输出中的 <think>...</think> 思考内容，确保下游 JSON 或文本处理不受干扰。
    """

    def __init__(self, base_provider: Any, cloned_provider: Optional[Any] = None, thinking_disabled: bool = True):
        self._base = base_provider
        self._cloned = cloned_provider
        self._thinking_disabled = thinking_disabled and (cloned_provider is not None)

    def __getattr__(self, name: str) -> Any:
        if self._cloned is not None and hasattr(self._cloned, name):
            return getattr(self._cloned, name)
        return getattr(self._base, name)

    async def text_chat(self, *args, **kwargs) -> Any:
        active = self._cloned if (self._cloned and self._thinking_disabled) else self._base
        try:
            resp = await active.text_chat(*args, **kwargs)
            return self._clean_response(resp)
        except Exception as exc:
            err_str = str(exc).lower()
            if self._cloned and self._thinking_disabled and any(k in err_str for k in _THINKING_PARAM_ERRORS):
                logger.warning(
                    f"[Fiscok's][provider] 辅助模型不支持关闭思考模式 (错误: {exc})，"
                    f"已自动平滑降级至原 Provider（保留思考链并自动过滤 <think> 标签）"
                )
                self._thinking_disabled = False
                resp = await self._base.text_chat(*args, **kwargs)
                return self._clean_response(resp)
            raise

    @staticmethod
    def _clean_response(resp: Any) -> Any:
        if resp and hasattr(resp, "completion_text"):
            text = str(resp.completion_text or "")
            cleaned = _THINKING_BLOCK_RE.sub("", text).strip()
            cleaned = re.sub(r'</?(?:think|thinking|thought)>', '', cleaned).strip()
            if cleaned:
                resp.completion_text = cleaned
        return resp

    async def terminate(self) -> None:
        if self._cloned and hasattr(self._cloned, "terminate"):
            res = self._cloned.terminate()
            if hasattr(res, "__await__"):
                await res


def resolve_provider(context: Context, provider_id: str = ""):
    """按 id 获取 Provider；未配置或未找到时回退到第一个可用的文本 Provider。"""
    provider = context.get_provider_by_id(provider_id) if provider_id else None
    if provider is None:
        all_providers = context.get_all_providers()
        provider = all_providers[0] if all_providers else None
    return provider


def _detect_family(base) -> str:
    """根据 Provider 实例的类继承链判断家族，避免直接导入各源码模块。"""
    mro_names = {c.__name__ for c in type(base).__mro__}
    if "ProviderAnthropic" in mro_names:
        return "anthropic"
    if "ProviderGoogleGenAI" in mro_names:
        return "gemini"
    type_name = str(getattr(base, "provider_config", {}).get("type", "")).lower()
    if "anthropic" in type_name:
        return "anthropic"
    if "googlegenai" in type_name or "gemini" in type_name:
        return "gemini"
    return "openai"


def _apply_thinking_off(config: dict, family: str) -> bool:
    """
    按家族在克隆配置中尝试关闭思考模式。
    返回 True 表示已尝试注入关闭思考参数，False 表示该模型为纯推理模型不适合关闭。
    """
    model = str(config.get("model") or "").lower()

    # 1. 纯推理模型（不支持关闭思考）：DeepSeek-R1、QwQ 等，跳过关闭注入，交由包装器滤除标签
    if any(k in model for k in ("deepseek-r1", "deepseek-reasoner", "qwq")):
        logger.info(f"[Fiscok's][provider] 检测到纯推理模型 {model}，跳过注入关闭思考参数，交由包装器自动滤除思考链")
        return False

    # 2. Anthropic 家族
    if family == "anthropic":
        config["anth_thinking_config"] = {"type": "", "budget": 0, "effort": ""}
        return True

    # 3. Gemini 家族
    if family == "gemini":
        config["gm_thinking_config"] = {"budget": 0}
        return True

    # 4. OpenAI 家族与第三方兼容端点
    merged = dict(config.get("custom_extra_body") or {})

    # o1 / o3 家族不支持 "none"，最低支持 "low"
    if any(k in model for k in ("o1", "o3")):
        merged["reasoning_effort"] = "low"
    else:
        merged.update(_DEFAULT_THINKING_OFF_EXTRA_BODY)

    api_base = str(config.get("api_base") or "").lower()
    if any(host in api_base for host in _ENABLE_THINKING_OFF_HOSTS):
        merged["enable_thinking"] = False

    config["custom_extra_body"] = merged
    config["ollama_disable_thinking"] = True
    return True


async def get_aux_provider(context: Context, provider_id: str = ""):
    """
    为辅助任务获取一个自适应 Provider 包装器（带缓存）。

    特性：
    - 针对支持关闭思考的模型，克隆出独立实例以关闭思考并与主对话隔离；
    - 针对纯推理模型（如 DeepSeek-R1、o1/o3），自动适配最低思考档位或保留思考；
    - 运行时自动捕获 400 思考参数异常并平滑降级到基础 Provider；
    - 统一滤除 <think> 思考链，确保下游解析安全。
    """
    base = resolve_provider(context, provider_id)
    if base is None:
        return None

    family = _detect_family(base)
    cache_key = (id(base), family)
    cached = _aux_provider_cache.get(cache_key)
    if cached is not None:
        return cached

    if not _PROVIDER_CLS_OK:
        wrapper = AuxProviderWrapper(base_provider=base, cloned_provider=None, thinking_disabled=False)
        _aux_provider_cache[cache_key] = wrapper
        return wrapper

    try:
        config = copy.deepcopy(base.provider_config)
        can_disable = _apply_thinking_off(config, family)

        meta = provider_cls_map.get(config.get("type", ""))
        cls_type = getattr(meta, "cls_type", None) if meta else None
        if cls_type is None:
            wrapper = AuxProviderWrapper(base_provider=base, cloned_provider=None, thinking_disabled=False)
            _aux_provider_cache[cache_key] = wrapper
            return wrapper

        inst = cls_type(config, getattr(base, "provider_settings", {}))
        if hasattr(inst, "initialize"):
            await inst.initialize()

        wrapper = AuxProviderWrapper(base_provider=base, cloned_provider=inst, thinking_disabled=can_disable)
        _aux_provider_cache[cache_key] = wrapper
        logger.info(
            f"[Fiscok's][provider] 已为辅助调用创建自适应 Provider: "
            f"{config.get('id')}({config.get('type')}, model={config.get('model')}, thinking_off={can_disable})"
        )
        return wrapper
    except Exception as e:
        logger.warning(f"[Fiscok's][provider] 创建辅助 Provider 失败，回退原 Provider: {e}")
        wrapper = AuxProviderWrapper(base_provider=base, cloned_provider=None, thinking_disabled=False)
        _aux_provider_cache[cache_key] = wrapper
        return wrapper


async def close_aux_providers() -> None:
    """关闭并清空缓存的辅助 Provider（插件卸载时调用）。"""
    for inst in list(_aux_provider_cache.values()):
        try:
            close = getattr(inst, "terminate", None)
            if close:
                res = close()
                if hasattr(res, "__await__"):
                    await res
        except Exception:
            pass
    _aux_provider_cache.clear()
