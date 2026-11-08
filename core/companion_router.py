"""主动陪伴协议 v1 的服务端路由器（兼容 sequenceVersion=1）。

桌面端约每 15 秒 POST /suzu/companion/sync：上行事件与执行回执，下行待执行指令。
参考 erdi_pet/bridge/companion_router.py，按本插件现状适配：
- 认证身份由桥接提供，绝不信任请求体里的身份
- Provider 由调用方传入；六秒内没结果就保持安静，不编造问候
- 事件、指令、回执、预算全部内存驻留，单进程有效
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from .behavior_sequence import (
    PROACTIVE_SEQUENCE_NOTE,
    environment_reference,
    fresh_environment,
    sequence_actions,
    sequence_text,
    validate_execution,
    validate_sequence,
)

PROTOCOL_VERSION = 1

EVENT_TYPES = {
    "lifecycle.started",
    "activity.idle",
    "activity.resumed",
    "interaction.hover",
    "interaction.click",
    "interaction.moved",
    "chat.started",
    "chat.finished",
    "context.app_changed",
    "ambient.opportunity",
    "external.signal",
}

# 数值越小越优先；只对 30 秒内的新事件做决策，旧事件仍确认但不补做
EVENT_PRIORITY = {
    "interaction.click": 0,
    "activity.resumed": 1,
    "lifecycle.started": 2,
    "external.signal": 3,
    "interaction.hover": 4,
    "ambient.opportunity": 5,
    "context.app_changed": 5,
    "interaction.moved": 6,
}

# 允许向 Provider 要发言的事件；普通点击也可以，但悬停只做注视
PROVIDER_EVENTS = {
    "activity.resumed",
    "lifecycle.started",
    "ambient.opportunity",
    "context.app_changed",
    "external.signal",
    "interaction.click",
}

RECEIPT_STATUS = ("accepted", "suppressed", "rejected", "cancelled")

PROACTIVE_PROMPT = (
    "系统主动陪伴机会（不是用户消息）。请结合提供的屏幕参考内容、事件与会话，"
    "自主选择动作和简短发言；可以保持安静。只有提供了屏幕资料才能据此回应，"
    "不要重复打扰。输出 JSON：{\"sequenceVersion\":1,\"sequence\":[...]}，"
    "sequence 为空表示保持安静。\n"
)

# 第二阶段（把发言草稿整理成行为序列）的内置提示词，可在插件配置里覆盖。
DEFAULT_PLANNER_PROMPT = (
    "你是行为计划生成器，不与人对话。输入包含一次主动陪伴事件、桌面能力表和一段已写好的发言草稿。"
    "请把草稿整理成一次行为序列：先做合适的动作，再说话；也可以只做动作。"
    "必须只输出一个 JSON 对象，禁止任何自然语言、解释、前后缀或 markdown 代码块。"
    '格式：{"sequenceVersion":1,"sequence":[...]}；不想说话也不想做动作时输出 '
    '{"sequenceVersion":1,"sequence":[]}。'
    "动作只能从能力表里选，名字不得自创；草稿内容可以精简，但不要新增事实或改人称。"
)

MAX_SCOPES = 128
MAX_EVENTS = 512
MAX_COMMANDS = 128
MAX_HISTORY = 128
MAX_RECEIPTS = 512
MAX_SAVED = 512


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def normalize_capabilities(raw: Any) -> Dict[str, Any]:
    """把客户端能力收敛成带边界的安全结构"""
    raw = raw if isinstance(raw, dict) else {}
    result: Dict[str, Any] = {}
    for key in ("motions", "intents", "expressions"):
        value = raw.get(key)
        result[key] = (
            [name[:80] for name in value[:128] if isinstance(name, str)]
            if isinstance(value, list)
            else []
        )
    features = raw.get("features")
    result["features"] = (
        {
            key: features.get(key) is True
            for key in ("attention", "proactive", "behaviorSequence")
        }
        if isinstance(features, dict)
        else {}
    )
    result["semantic"] = []
    semantic = raw.get("semantic")
    for item in semantic[:64] if isinstance(semantic, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and _finite(item.get("min"))
            and _finite(item.get("max"))
            and item["min"] <= item["max"]
        ):
            result["semantic"].append(
                {"name": item["name"][:80], "min": item["min"], "max": item["max"]}
            )
    return result


def validate_actions(raw: Any, capabilities: Dict[str, Any]) -> Tuple[List[dict], List[str]]:
    """按客户端声明的能力过滤动作；未声明的动作直接丢弃并给出说明"""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["actions 必须为数组"]
    actions: List[dict] = []
    warnings: List[str] = []
    for index, action in enumerate(raw[:8]):
        accepted = None
        if isinstance(action, dict):
            kind = action.get("type")
            if kind in ("motion", "intent"):
                allowed = capabilities.get(
                    "motions" if kind == "motion" else "intents", []
                )
                mode = action.get("mode", "interrupt")
                if (
                    isinstance(action.get("name"), str)
                    and action["name"] in allowed
                    and mode in ("interrupt", "queue")
                ):
                    accepted = {"type": kind, "name": action["name"], "mode": mode}
            elif kind == "expression":
                name = action.get("name")
                if "name" in action and (
                    name is None
                    or (isinstance(name, str) and name in capabilities.get("expressions", []))
                ):
                    accepted = {"type": kind, "name": name}
            elif kind == "pose" and isinstance(action.get("values"), dict) and action["values"]:
                controls = {item["name"]: item for item in capabilities.get("semantic", [])}
                values = {}
                for name, value in action["values"].items():
                    if name not in controls or not _finite(value):
                        break
                    values[name] = max(controls[name]["min"], min(controls[name]["max"], value))
                else:
                    accepted = {"type": kind, "values": values}
            elif (
                kind == "attention"
                and capabilities.get("features", {}).get("attention") is True
                and {"gaze_x", "gaze_y"}.issubset(
                    {item["name"] for item in capabilities.get("semantic", [])}
                )
            ):
                target = action.get("target")
                hold = action.get("holdMs", 6000)
                strength = action.get("strength", 0.3)
                if target in ("pointer", "viewer") and _finite(hold) and _finite(strength):
                    accepted = {
                        "type": kind,
                        "target": target,
                        "holdMs": max(500, min(15000, hold)),
                        "strength": max(0.05, min(0.6, strength)),
                    }
            elif kind in ("interrupt", "reset"):
                accepted = {"type": kind}
            if accepted is not None and kind in ("expression", "pose") and "holdMs" in action:
                if _finite(action["holdMs"]):
                    accepted["holdMs"] = max(0, min(600000, action["holdMs"]))
                else:
                    accepted = None
        if accepted is None:
            warnings.append(f"忽略无效动作 {index}")
        else:
            actions.append(accepted)
    if len(raw) > 8:
        warnings.append("超过 8 项的动作已忽略")
    return actions, warnings


class CompanionRouter:
    """单进程参考路由器：去重、按设备实例排队、默认轻动作与可选 Provider 决策。"""

    def __init__(
        self,
        *,
        now: Optional[Callable[[], int]] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.now = now or (lambda: int(time.time() * 1000))
        self.log = log or (lambda message: None)
        self.scopes: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
        self.budgets: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()

    @staticmethod
    def _key(identity: Any, session_id: Any, instance_id: Any) -> Tuple[str, str, str]:
        valid = (
            isinstance(identity, str)
            and 1 <= len(identity) <= 100
            and all(
                isinstance(value, str) and 1 <= len(value) <= 100
                for value in (session_id, instance_id)
            )
        )
        if not valid:
            raise ValueError("认证身份、sessionId 与 instanceId 均为必填")
        return identity, session_id, instance_id

    def _scope(self, key: Tuple[str, str, str]) -> Dict[str, Any]:
        scope = self.scopes.get(key)
        if scope is None:
            scope = {
                "events": OrderedDict(),
                "commands": OrderedDict(),
                "history": OrderedDict(),
                "receipts": OrderedDict(),
                "saved": OrderedDict(),
                "capabilities": {},
                "lock": asyncio.Lock(),
            }
            self.scopes[key] = scope
        self.scopes.move_to_end(key)
        while len(self.scopes) > MAX_SCOPES:
            self.scopes.popitem(last=False)
        return scope

    # ---------------- 对外能力 ----------------

    def enqueue(
        self,
        *,
        authenticated_identity: str,
        session_id: str,
        instance_id: str,
        actions: Any = None,
        text: str = "",
        sequence: Any = None,
        trigger_event_id: Optional[str] = None,
        ttl_ms: int = 30000,
    ) -> Dict[str, Any]:
        """把一条主动指令排入队列；桌面端下一次同步时取走。"""
        key = self._key(authenticated_identity, session_id, instance_id)
        scope = self._scope(key)
        if not scope["capabilities"]:
            raise ValueError("桌面端尚未同步能力")
        if sequence is not None:
            if not scope["capabilities"].get("features", {}).get("behaviorSequence"):
                raise ValueError("桌面端不支持行为序列")
            sequence = validate_sequence(sequence, scope["capabilities"], proactive=True)
            actions, text = sequence_actions(sequence), sequence_text(sequence)
        checked, warnings = validate_actions(actions, scope["capabilities"])
        if (
            warnings
            or len(checked) > 4
            or any(item["type"] in ("reset", "interrupt") for item in checked)
        ):
            raise ValueError("主动动作不合法")
        if not isinstance(text, str) or len(text) > 400 or (not text.strip() and not checked):
            raise ValueError("主动话语不合法")
        for action in checked:
            if "holdMs" in action:
                action["holdMs"] = min(15000, action["holdMs"])
        now = self.now()
        command: Dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "id": str(uuid4()),
            "sessionId": session_id,
            "instanceId": instance_id,
            "createdAt": now,
            "expiresAt": now + max(1000, min(60000, int(ttl_ms or 30000))),
            "kind": "perform",
            "text": text.strip(),
            "actions": checked,
        }
        if sequence is not None:
            command.update(sequenceVersion=1, sequence=sequence)
        if trigger_event_id:
            command["triggerEventId"] = trigger_event_id
        scope["commands"][command["id"]] = command
        while len(scope["commands"]) > MAX_COMMANDS:
            scope["commands"].popitem(last=False)
        return command

    def cancel(
        self,
        *,
        authenticated_identity: str,
        session_id: str,
        instance_id: str,
        target_id: str,
    ) -> Dict[str, Any]:
        """取消该实例内尚未回执的主动指令；不会影响用户聊天。"""
        key = self._key(authenticated_identity, session_id, instance_id)
        scope = self._scope(key)
        if target_id not in scope["commands"] and target_id not in scope["history"]:
            raise ValueError("目标指令不属于该桌面实例")
        now = self.now()
        command: Dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "id": str(uuid4()),
            "sessionId": session_id,
            "instanceId": instance_id,
            "createdAt": now,
            "expiresAt": now + 30000,
            "kind": "cancel",
            "targetId": target_id,
        }
        original = scope["commands"].pop(target_id, None)
        if original:
            scope["history"][target_id] = original
            while len(scope["history"]) > MAX_HISTORY:
                scope["history"].popitem(last=False)
        scope["commands"][command["id"]] = command
        return command

    def locate(self, authenticated_identity: str) -> Optional[Dict[str, str]]:
        """返回该身份最近同步的 sessionId / instanceId，供其他工具入队时使用。"""
        for key in reversed(self.scopes.keys()):
            if key[0] == authenticated_identity:
                return {"sessionId": key[1], "instanceId": key[2]}
        return None

    def drop_pending(self, authenticated_identity: str, session_id: str) -> int:
        """用户聊天优先：丢弃该会话尚未下发的主动指令。"""
        dropped = 0
        for key, scope in list(self.scopes.items()):
            if key[0] != authenticated_identity or key[1] != session_id:
                continue
            while scope["commands"]:
                command_id, original = scope["commands"].popitem(last=False)
                scope["history"][command_id] = original
                dropped += 1
            while len(scope["history"]) > MAX_HISTORY:
                scope["history"].popitem(last=False)
        return dropped

    async def sync(
        self,
        body: Dict[str, Any],
        *,
        authenticated_identity: str,
        provider: Any = None,
        system_prompt: str = "",
        contexts: Optional[List[dict]] = None,
        image_urls: Optional[List[str]] = None,
        decide: Optional[Callable[..., Awaitable[Optional[dict]]]] = None,
        save_proactive: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
        planner_provider: Any = None,
        planner_prompt: str = "",
    ) -> Dict[str, Any]:
        """处理一次同步：确认事件/回执，按事件与预算决定是否下发新指令。"""
        if not isinstance(body, dict):
            raise ValueError("请求体必须为对象")
        if body.get("protocolVersion") != PROTOCOL_VERSION:
            raise ValueError("不支持的 protocolVersion")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError("requestId 格式不合法")
        events = body.get("events")
        receipts = body.get("receipts")
        if not isinstance(events, list) or len(events) > 32:
            raise ValueError("events 必须为最多 32 项的数组")
        if not isinstance(receipts, list) or len(receipts) > 32:
            raise ValueError("receipts 必须为最多 32 项的数组")

        session_id = body.get("sessionId")
        instance_id = body.get("instanceId")
        key = self._key(authenticated_identity, session_id, instance_id)
        scope = self._scope(key)

        async with scope["lock"]:
            now = self.now()
            scope["capabilities"] = normalize_capabilities(body.get("capabilities"))

            ack_receipts: List[str] = []
            for receipt in receipts:
                if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str):
                    continue
                command_id = receipt["id"]
                if receipt.get("status") not in RECEIPT_STATUS:
                    continue
                original = scope["commands"].get(command_id) or scope["history"].get(command_id)
                spoken = ""
                if original and receipt["status"] == "accepted":
                    if "sequence" in original:
                        # 序列要等执行回执里的 spokenText，取消前未执行的发言不能算说过。
                        execution = receipt.get("execution")
                        if execution is not None:
                            try:
                                spoken = validate_execution(
                                    execution, original["sequence"]
                                )["spokenText"]
                            except ValueError:
                                spoken = ""
                    else:
                        spoken = original.get("text", "")
                if spoken and command_id not in scope["saved"] and save_proactive:
                    try:
                        await save_proactive(
                            f"{authenticated_identity}/{session_id}", command_id, spoken
                        )
                        scope["saved"][command_id] = now
                        while len(scope["saved"]) > MAX_SAVED:
                            scope["saved"].popitem(last=False)
                    except Exception as exc:
                        self.log(f"保存主动话语失败: {type(exc).__name__}: {exc}")
                # 接收回执与执行回执是两条独立消息，同 id 再次上报不能被丢弃。
                if original or command_id in scope["receipts"]:
                    scope["receipts"][command_id] = now
                if original:
                    scope["history"][command_id] = original
                    while len(scope["history"]) > MAX_HISTORY:
                        scope["history"].popitem(last=False)
                scope["commands"].pop(command_id, None)
                ack_receipts.append(command_id)
            while len(scope["receipts"]) > MAX_RECEIPTS:
                scope["receipts"].popitem(last=False)

            new_events: List[dict] = []
            ack_events: List[str] = []
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                    continue
                event_id = event["id"]
                ack_events.append(event_id)
                if event_id in scope["events"]:
                    continue
                scope["events"][event_id] = now
                if (
                    event.get("type") in EVENT_TYPES
                    and _finite(event.get("at"))
                    and -5000 <= now - event["at"] <= 30000
                ):
                    new_events.append(event)
            while len(scope["events"]) > MAX_EVENTS:
                scope["events"].popitem(last=False)

            state = body.get("state") if isinstance(body.get("state"), dict) else {}
            blocked = bool(
                state.get("busy")
                or state.get("quiet")
                or state.get("paused")
                or (state.get("idleSeconds", 120) or 0) >= 120
            )
            budget = self._budget(key[:2])
            budget["speech"] = [at for at in budget["speech"] if now - at < 3600000]
            environment = fresh_environment(body.get("environment"), now)

            if new_events and not blocked:
                await self._maybe_queue(
                    key,
                    scope,
                    new_events,
                    state,
                    budget,
                    environment=environment,
                    provider=provider,
                    system_prompt=system_prompt,
                    contexts=contexts,
                    image_urls=image_urls if environment else None,
                    decide=decide,
                    planner_provider=planner_provider,
                    planner_prompt=planner_prompt,
                )

            pending = [
                command
                for command in scope["commands"].values()
                if command["expiresAt"] > self.now()
            ]
            # 未收到终态回执前会反复返回同一条指令；客户端按 ID 去重。
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": request_id,
                "ackEventIds": ack_events,
                "ackReceiptIds": ack_receipts,
                "commands": pending[:8],
            }

    # ---------------- 内部实现 ----------------

    def _budget(self, budget_key: Tuple[str, str]) -> Dict[str, Any]:
        budget = self.budgets.get(budget_key)
        if budget is None:
            budget = {
                "lastAction": -(10 ** 15),
                "lastAttention": -(10 ** 15),
                "lastSpeech": -(10 ** 15),
                "speech": [],
            }
            self.budgets[budget_key] = budget
        self.budgets.move_to_end(budget_key)
        while len(self.budgets) > MAX_SCOPES:
            self.budgets.popitem(last=False)
        return budget

    def _can_speak(self, state: Dict[str, Any], budget: Dict[str, Any], now: int) -> bool:
        return bool(
            state.get("allowSpeech")
            and state.get("speechRemaining", 0) > 0
            and state.get("speechCooldownRemainingMs", 1) == 0
            and state.get("postChatGraceRemainingMs", 0) == 0
            and (state.get("idleSeconds", 0) or 0) >= 2
            and now - budget["lastSpeech"] >= 600000
            and len(budget["speech"]) < 4
        )

    async def _maybe_queue(
        self,
        key: Tuple[str, str, str],
        scope: Dict[str, Any],
        new_events: List[dict],
        state: Dict[str, Any],
        budget: Dict[str, Any],
        *,
        environment: Optional[Dict[str, Any]],
        provider: Any,
        system_prompt: str,
        contexts: Optional[List[dict]],
        image_urls: Optional[List[str]],
        decide: Optional[Callable[..., Awaitable[Optional[dict]]]],
        planner_provider: Any = None,
        planner_prompt: str = "",
    ) -> None:
        candidates = [event for event in new_events if event["type"] in EVENT_PRIORITY]
        if not candidates:
            return
        event = min(candidates, key=lambda item: EVENT_PRIORITY[item["type"]])
        attention_only = event["type"] == "interaction.hover"
        now = self.now()
        cooldown = 10000 if attention_only else 45000
        last = budget["lastAttention" if attention_only else "lastAction"]
        if now - last < cooldown:
            return
        if not attention_only and state.get("actionCooldownRemainingMs", 0) > 0:
            return

        capabilities = scope["capabilities"]
        actions: List[dict] = []
        if (
            capabilities.get("features", {}).get("attention") is True
            and state.get("attention")
        ):
            actions.append(
                {
                    "type": "attention",
                    "target": "pointer" if attention_only else "viewer",
                    "holdMs": 6000,
                    "strength": 0.3,
                }
            )
        if not attention_only:
            if "smile" in capabilities.get("expressions", []):
                actions.append({"type": "expression", "name": "smile", "holdMs": 3000})
            motion = "gentleSway" if event["type"] == "ambient.opportunity" else "yesNod"
            if motion in capabilities.get("motions", []):
                actions.append({"type": "motion", "name": motion, "mode": "interrupt"})

        decision: Dict[str, Any] = {"text": "", "actions": actions}
        can_speak = self._can_speak(state, budget, now)
        if decide:
            # 自定义 Agent/规则同样拿到经过时效检查的屏幕上下文。
            try:
                custom = await asyncio.wait_for(
                    decide(
                        event=event,
                        state=state,
                        capabilities=capabilities,
                        environment=environment,
                        identity=key[0],
                        session_id=key[1],
                    ),
                    timeout=6,
                )
            except Exception as exc:
                self.log(f"主动事件决策失败: {type(exc).__name__}")
                custom = None
            if isinstance(custom, dict):
                decision = custom
        elif (
            provider
            and not attention_only
            and event["type"] in PROVIDER_EVENTS
        ):
            generated = await self._ask_provider(
                provider,
                event,
                state,
                capabilities,
                can_speak,
                environment,
                system_prompt,
                contexts,
                image_urls,
                key,
                planner_provider=planner_provider,
                planner_prompt=planner_prompt,
            )
            if generated is not None:
                decision.update(generated)

        text = decision.get("text", "")
        if not isinstance(text, str):
            text = ""
        text = text.strip()[:400]
        if text and (
            not state.get("allowSpeech")
            or state.get("speechRemaining", 0) <= 0
            or state.get("speechCooldownRemainingMs", 1) > 0
        ):
            text = ""

        sequence = decision.get("sequence")
        if sequence is not None and not text:
            sequence = [step for step in sequence if step["type"] != "say"]
        supports_sequence = (
            capabilities.get("features", {}).get("behaviorSequence") is True
        )
        if sequence is None and supports_sequence:
            sequence = [
                {
                    "type": "action",
                    "action": action,
                    "waitForCompletion": action["type"] in ("motion", "intent"),
                }
                for action in decision.get("actions", [])
            ]
            if text:
                hold = max(1600, min(15000, len(text) * 180))
                sequence.append({"type": "say", "text": text, "holdMs": hold})
        if not supports_sequence:
            sequence = None
        if not (text or decision.get("actions") or sequence):
            return
        try:
            self.enqueue(
                authenticated_identity=key[0],
                session_id=key[1],
                instance_id=key[2],
                actions=decision.get("actions", []),
                text=text,
                sequence=sequence or None,
                trigger_event_id=event["id"],
            )
        except ValueError as exc:
            self.log(f"主动指令入队被拒: {exc}")
            return
        budget["lastAttention" if attention_only else "lastAction"] = self.now()
        if text:
            budget["lastSpeech"] = self.now()
            budget["speech"].append(self.now())

    async def _ask_provider(
        self,
        provider: Any,
        event: dict,
        state: Dict[str, Any],
        capabilities: Dict[str, Any],
        can_speak: bool,
        environment: Optional[Dict[str, Any]],
        system_prompt: str,
        contexts: Optional[List[dict]],
        image_urls: Optional[List[str]],
        key: Tuple[str, str, str],
        planner_provider: Any = None,
        planner_prompt: str = "",
    ) -> Optional[Dict[str, Any]]:
        """两次调用：先按人设生成发言草稿，再交给独立提示词把草稿规划成 JSON 序列。"""
        draft = ""
        if can_speak:
            prompt = PROACTIVE_PROMPT + json.dumps(
                {"event": event, "can_speak": True, "desktop_capabilities": capabilities},
                ensure_ascii=False,
            )
            if environment:
                prompt += environment_reference(environment)
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        contexts=contexts or [],
                        system_prompt=system_prompt,
                        image_urls=image_urls or None,
                        session_id=f"{key[0]}/{key[1]}",
                    ),
                    timeout=6,
                )
            except Exception as exc:
                hint = "（已附带截图，Provider 可能不支持图片）" if image_urls else ""
                self.log(f"主动陪伴发言生成失败: {type(exc).__name__}: {exc}{hint}")
            else:
                completion = getattr(response, "completion_text", "")
                if not isinstance(completion, str):
                    completion = response if isinstance(response, str) else ""
                draft = completion.strip()[:400]
            if not draft:
                # 第一阶段已经决定保持安静，不再进入规划。
                return {"text": ""}

        planner = planner_provider or provider
        instruction = (
            (planner_prompt or "").strip() or DEFAULT_PLANNER_PROMPT
        ) + "\n" + PROACTIVE_SEQUENCE_NOTE
        payload = {
            "event": event,
            "can_speak": can_speak,
            "desktop_capabilities": capabilities,
            "draft": draft,
        }
        try:
            response = await asyncio.wait_for(
                planner.text_chat(
                    prompt="决策输入：" + json.dumps(payload, ensure_ascii=False),
                    contexts=contexts or [],
                    system_prompt=instruction,
                    session_id=f"{key[0]}/{key[1]}",
                ),
                timeout=6,
            )
        except Exception as exc:
            self.log(f"主动陪伴规划调用失败: {type(exc).__name__}: {exc}")
            return {"text": draft} if draft else None

        completion = getattr(response, "completion_text", "")
        if not isinstance(completion, str):
            completion = response if isinstance(response, str) else ""
        completion = completion.strip()
        if completion.startswith("```json\n") and completion.endswith("```"):
            completion = completion[8:-3].strip()
        elif completion.startswith("```\n") and completion.endswith("```"):
            completion = completion[4:-3].strip()
        try:
            generated = json.loads(completion)
        except ValueError:
            # 模型偶尔会在 JSON 前后带上解释性文字，尝试截取最外层对象再解析一次。
            start, end = completion.find("{"), completion.rfind("}")
            generated = None
            if start >= 0 and end > start:
                try:
                    generated = json.loads(completion[start : end + 1])
                except ValueError:
                    generated = None
            if generated is None:
                self.log(
                    f"主动陪伴计划输出不是合法 JSON（前 240 字）: {completion[:240]}"
                )
                return {"text": draft} if draft else None
        if not isinstance(generated, dict):
            self.log(
                f"主动陪伴计划输出不是 JSON 对象（前 240 字）: {completion[:240]}"
            )
            return {"text": draft} if draft else None
        if "sequence" in generated:
            if generated.get("sequenceVersion") != 1:
                self.log(
                    f"主动陪伴序列版本不支持: {generated.get('sequenceVersion')!r}"
                )
                return {"text": draft} if draft else None
            if not generated["sequence"]:
                return {"text": "", "actions": []}
            try:
                sequence = validate_sequence(
                    generated["sequence"], capabilities, proactive=True
                )
            except ValueError as exc:
                self.log(
                    f"主动陪伴序列校验失败: {exc}；"
                    f"motions={capabilities.get('motions')}, "
                    f"intents={capabilities.get('intents')}, "
                    f"expressions={capabilities.get('expressions')}；"
                    f"输出: {completion[:240]}"
                )
                return {"text": draft} if draft else None
            if not can_speak:
                sequence = [step for step in sequence if step["type"] != "say"]
            return {
                "text": sequence_text(sequence),
                "actions": sequence_actions(sequence),
                "sequence": sequence,
            }
        spoken = generated.get("reply", "")
        if not isinstance(spoken, str) or len(spoken) > 400:
            self.log(f"主动陪伴计划回复字段不合法（前 240 字）: {completion[:240]}")
            return {"text": draft} if draft else None
        result: Dict[str, Any] = {"text": spoken.strip() if can_speak else ""}
        if "actions" in generated:
            result["actions"] = generated["actions"]
        return result
