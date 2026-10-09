'''
辅助调用（情绪分析 / 表情包描述）的 Provider 工具：
- 解析目标 Provider（未配置时回退到第一个可用 Provider）
- 克隆一个"关闭思考"的独立 Provider 实例，与主对话实例隔离，避免竞态
'''
import copy

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

_aux_provider_cache: dict = {}


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


def _apply_thinking_off(config: dict, family: str) -> None:
    """按家族在克隆配置中关闭思考模式。"""
    if family == "anthropic":
        config["anth_thinking_config"] = {"type": "", "budget": 0, "effort": ""}
    elif family == "gemini":
        config["gm_thinking_config"] = {"budget": 0}
    else:
        merged = dict(config.get("custom_extra_body") or {})
        merged.update(_DEFAULT_THINKING_OFF_EXTRA_BODY)
        config["custom_extra_body"] = merged
        config["ollama_disable_thinking"] = True


async def get_aux_provider(context: Context, provider_id: str = ""):
    """
    为辅助任务获取一个关闭思考的独立 Provider 实例（带缓存）。

    克隆失败时回退到原始 Provider，保证辅助功能不因克隆异常而中断。
    """
    base = resolve_provider(context, provider_id)
    if base is None:
        return None

    if not _PROVIDER_CLS_OK:
        return base

    family = _detect_family(base)
    cache_key = (id(base), family)
    cached = _aux_provider_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        config = copy.deepcopy(base.provider_config)
        _apply_thinking_off(config, family)
        meta = provider_cls_map.get(config.get("type", ""))
        cls_type = getattr(meta, "cls_type", None) if meta else None
        if cls_type is None:
            return base

        inst = cls_type(config, getattr(base, "provider_settings", {}))
        if hasattr(inst, "initialize"):
            await inst.initialize()

        _aux_provider_cache[cache_key] = inst
        logger.info(
            f"[Fiscok's][provider] 已为辅助调用创建关闭思考的独立 Provider: "
            f"{config.get('id')}({config.get('type')}, family={family})"
        )
        return inst
    except Exception as e:
        logger.warning(f"[Fiscok's][provider] 创建辅助 Provider 失败，回退原 Provider: {e}")
        return base


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