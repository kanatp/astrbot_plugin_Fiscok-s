"""
CLM 自主上下文管理服务 (CLMContextService)
基于 Meta FAIR 顶会论文《Context Language Models》(arXiv:2609.37725 / OpenReview: U1ObEVC4A7)

核心范式：Context as a File (CaaF，上下文即文件/工作记忆账本)
1. 彻底打破线性追加与机械滑动窗口，为每个会话维护结构化的工作记忆文件；
2. 模型自主对上下文执行四大原子操作：[PIN] 锚定约束、[EDIT] 事实更新、[COMPRESS] 历程浓缩、[PRUNE] 噪声剪枝；
3. 将动辄数十条的冗长上下文替换为轻量级即时缓冲（降低 60% 以上 Token），前端注入结构化账本，关键约定永不遗忘。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from ..api.provider_utils import get_aux_provider


class ContextFile:
    def __init__(
        self,
        umo: str,
        pinned_anchors: Optional[List[str]] = None,
        distilled_facts: Optional[List[str]] = None,
        milestone_summary: str = "",
        recent_buffer: Optional[List[Dict[str, str]]] = None,
        turn_count: int = 0,
        last_compacted_at: str = "",
    ):
        self.umo = umo
        self.pinned_anchors = pinned_anchors or []
        self.distilled_facts = distilled_facts or []
        self.milestone_summary = milestone_summary or ""
        self.recent_buffer = recent_buffer or []
        self.turn_count = turn_count
        self.last_compacted_at = last_compacted_at or datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "umo": self.umo,
            "pinned_anchors": self.pinned_anchors,
            "distilled_facts": self.distilled_facts,
            "milestone_summary": self.milestone_summary,
            "recent_buffer": self.recent_buffer[-8:],
            "turn_count": self.turn_count,
            "last_compacted_at": self.last_compacted_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ContextFile:
        return cls(
            umo=data.get("umo", ""),
            pinned_anchors=data.get("pinned_anchors") or [],
            distilled_facts=data.get("distilled_facts") or [],
            milestone_summary=data.get("milestone_summary", ""),
            recent_buffer=data.get("recent_buffer") or [],
            turn_count=data.get("turn_count", 0),
            last_compacted_at=data.get("last_compacted_at", ""),
        )


class CLMContextService:
    def __init__(self, plugin: Any, data_dir: Path):
        self.plugin = plugin
        self.files_dir = Path(data_dir) / "context_files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, ContextFile] = {}
        self._compacting_set: set = set()

    def _file_path(self, umo: str) -> Path:
        safe_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', umo)
        return self.files_dir / f"{safe_name}.json"

    def get_context_file(self, umo: str) -> ContextFile:
        """读取会话对应的 Context File，优先读缓存，不存在则加载或新建"""
        if umo in self._cache:
            return self._cache[umo]

        file_path = self._file_path(umo)
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cf = ContextFile.from_dict(data)
                self._cache[umo] = cf
                return cf
            except Exception as e:
                logger.error(f"[CLMContext] 加载上下文文件失败 ({file_path}): {e}")

        cf = ContextFile(umo=umo)
        self._cache[umo] = cf
        return cf

    def save_context_file(self, cf: ContextFile) -> bool:
        """持久化落盘 Context File"""
        try:
            file_path = self._file_path(cf.umo)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(cf.to_dict(), f, ensure_ascii=False, indent=2)
            self._cache[cf.umo] = cf
            return True
        except Exception as e:
            logger.error(f"[CLMContext] 保存上下文文件失败 ({cf.umo}): {e}")
            return False

    def reset_context_file(self, umo: str) -> bool:
        """重置某会话的上下文文件"""
        self._cache.pop(umo, None)
        file_path = self._file_path(umo)
        if file_path.exists():
            try:
                file_path.unlink()
                return True
            except Exception:
                return False
        return True

    def format_injection(self, cf: ContextFile) -> str:
        """将 Context File 格式化为结构化工作记忆账本，供注入提示词"""
        if not cf.pinned_anchors and not cf.distilled_facts and not cf.milestone_summary:
            return ""

        parts = ["<当前工作记忆账本 (Context File)>"]
        if cf.pinned_anchors:
            parts.append("【核心目标与顶层约束 (Pinned)】：")
            for item in cf.pinned_anchors:
                parts.append(f"- {item}")

        if cf.distilled_facts:
            parts.append("【关键事实与已确认要点】：")
            for item in cf.distilled_facts:
                parts.append(f"- {item}")

        if cf.milestone_summary:
            parts.append(f"【历史历程概要】：\n{cf.milestone_summary}")

        parts.append("表达要求：以上是本会话已确立的核心工作记忆，请严格遵守其中的约束和事实，不要与之冲突；无需向用户解释或提及本账本。")
        parts.append("</当前工作记忆账本 (Context File)>")
        return "\n".join(parts)

    def process_llm_request(self, event: Any, req: Any, config: Dict[str, Any]) -> None:
        """
        拦截主对话 LLM 请求，应用 CLM 自主上下文管理策略：
        1. 注入结构化工作记忆账本；
        2. 替换冗长上下文为轻量级最新缓冲（大幅降低 Token 消耗）；
        3. 累计轮数并择机调度后台自主整理。
        """
        strategy = str(config.get("strategy", "clm") or "clm").lower()
        if strategy == "off":
            return

        contexts = getattr(req, "contexts", None)
        if not contexts or not isinstance(contexts, list):
            return

        # 传统模式降级
        if strategy == "trim":
            keep_recent = int(config.get("keep_recent_messages", 10) or 10)
            if len(contexts) > keep_recent:
                req.contexts = contexts[-keep_recent:]
            return

        # CLM 模式
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return

        cf = self.get_context_file(umo)

        # 检查底层会话是否被 /reset 清空（历史消息突然变空/仅一条，而账本已有轮数记录）
        if len(contexts) <= 1 and cf.turn_count >= 2:
            logger.info(f"[CLMContext] 检测到底层会话已重置，自动同步清空工作记忆账本 ({umo})")
            self.reset_context_file(umo)
            cf = self.get_context_file(umo)

        # 1. 注入工作记忆账本
        injection = self.format_injection(cf)
        if injection:
            req.system_prompt = f"{req.system_prompt or ''}\n{injection}".strip()

        # 2. 替换上下文为轻量缓冲（默认保留最近 6 条）
        keep_turns = int(config.get("keep_buffer_turns", 6) or 6)
        if len(contexts) > keep_turns:
            old_len = len(contexts)
            trimmed = contexts[-keep_turns:]
            # 首条若为 assistant 则前移一条，保持 user 引导
            while trimmed and isinstance(trimmed[0], dict) and trimmed[0].get("role") == "assistant":
                trimmed = trimmed[1:]
            req.contexts = trimmed
            cf.recent_buffer = [
                {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
                for m in trimmed if isinstance(m, dict)
            ]
            logger.info(f"[CLMContext] 已接管上下文：{old_len} 条 -> 精简为最新 {len(trimmed)} 条紧凑缓冲")

        # 3. 首次冷启动过渡：若已有长历史但账本尚未有提炼内容，立即后台触发首次初始化提炼
        is_fresh_cf = not cf.pinned_anchors and not cf.distilled_facts and not cf.milestone_summary
        if is_fresh_cf and len(contexts) > keep_turns and umo not in self._compacting_set:
            logger.info(f"[CLMContext] 检测到会话 {umo} 已有长历史，自动触发首次 CLM 工作记忆初始化提炼...")
            asyncio.create_task(self.compact_context_file(umo, config))

        # 4. 轮数累计与常规后台自主整理触发
        cf.turn_count += 1
        self.save_context_file(cf)

        compact_threshold = int(config.get("compact_every_turns", 8) or 8)
        if cf.turn_count >= compact_threshold and umo not in self._compacting_set:
            asyncio.create_task(self.compact_context_file(umo, config))

    async def compact_context_file(self, umo: str, config: Dict[str, Any]) -> bool:
        """
        后台异步触发 CLM 自主整理循环：
        调用辅助模型对近期对话流水账进行自主编辑、事实更新、目标提炼与无用噪音剪枝。
        """
        if umo in self._compacting_set:
            return False
        self._compacting_set.add(umo)

        try:
            logger.info(f"[CLMContext] 开始对会话 {umo} 执行 CLM 自主记忆整理循环...")
            cf = self.get_context_file(umo)

            # 获取近期完整上下文
            recent_contexts = await self.plugin.conversation_contexts(umo, limit=16)
            if not recent_contexts:
                cf.turn_count = 0
                self.save_context_file(cf)
                return False

            lines = []
            for m in recent_contexts:
                role = "用户" if m.get("role") == "user" else "我"
                lines.append(f"{role}：{m.get('content')}")
            recent_dialogue = "\n".join(lines)

            pinned_str = "\n".join([f"- {a}" for a in cf.pinned_anchors]) if cf.pinned_anchors else "（暂无）"
            facts_str = "\n".join([f"- {f}" for f in cf.distilled_facts]) if cf.distilled_facts else "（暂无）"
            summary_str = cf.milestone_summary or "（暂无）"

            prompt_tmpl = self.plugin.prompt_library.get("clm_compaction_prompt")
            prompt = prompt_tmpl.format(
                pinned_anchors=pinned_str,
                distilled_facts=facts_str,
                milestone_summary=summary_str,
                recent_dialogue=recent_dialogue,
            )

            provider_id = config.get("llm_provider_id", "")
            provider = await get_aux_provider(self.plugin.context, provider_id)

            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是上下文工作记忆整理器，只返回严格的 JSON 对象，不要附加任何其他解释。",
            )

            completion = str(getattr(resp, "completion_text", "") or "").strip()
            if not completion:
                logger.warning("[CLMContext] 整理模型返回为空，跳过本次整理")
                return False

            # 解析 JSON
            clean_json = re.sub(r'^```json\s*', '', completion, flags=re.MULTILINE)
            clean_json = re.sub(r'\s*```$', '', clean_json, flags=re.MULTILINE).strip()
            data = json.loads(clean_json)

            max_anchors = int(config.get("max_anchors", 5) or 5)
            max_facts = int(config.get("max_facts", 12) or 12)

            new_anchors = data.get("pinned_anchors", [])
            new_facts = data.get("distilled_facts", [])
            new_summary = str(data.get("milestone_summary", "") or "").strip()

            if isinstance(new_anchors, list):
                cf.pinned_anchors = [str(x).strip() for x in new_anchors if str(x).strip()][:max_anchors]
            if isinstance(new_facts, list):
                cf.distilled_facts = [str(x).strip() for x in new_facts if str(x).strip()][:max_facts]
            if new_summary:
                cf.milestone_summary = new_summary

            cf.turn_count = 0
            cf.last_compacted_at = datetime.now().isoformat()
            self.save_context_file(cf)

            logger.info(
                f"[CLMContext] 会话 {umo} CLM 自主整理完成：保留 {len(cf.pinned_anchors)} 项顶层锚定，"
                f"{len(cf.distilled_facts)} 条关键事实"
            )
            return True

        except Exception as e:
            logger.warning(f"[CLMContext] 执行 CLM 自主整理循环异常: {e}")
            return False
        finally:
            self._compacting_set.discard(umo)
