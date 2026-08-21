"""
第 13 章的双模式主 Agent 规划器。

文件逻辑
--------
规划器只回答一个问题：在当前可审计证据状态下，下一步应请求什么动作。
DeterministicPlanner 让课程在离线环境稳定运行；OpenAIPlanner 使用 Responses
API 的函数工具调用，让真实模型在受限工具集合中做选择。两种实现都返回同一
个 contracts.Action，之后必须由 main_agent.py 的确定性执行层调用视觉、检索
或规则服务。

技术栈
------
- Python 3.12 Protocol、dataclass、json；
- Pydantic Action/ObservationRequest 判别联合；
- OpenAI Python SDK 与 Responses API function calling（仅 OpenAIPlanner）；
- 第 4 章结构化契约，保证模型输出不能直接伪装成兼容性结论。

调用流程
--------
MainAgentState -> available_action_names()
    -> DeterministicPlanner.plan() 或 OpenAIPlanner.plan()
    -> tool_call_to_action() 校验模型工具参数
    -> Action
    -> LangGraph 执行层，而不是直接执行模型生成的任意代码。

边界
----
规划器不读取图像、不访问 C++、不写 SQLite，也不计算 USB-C 结果。真实 API
密钥只从 OPENAI_API_KEY 读取，绝不写入状态、事件或 TOML 配置。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from realsight.contracts import (
    Action,
    ActionType,
    AskUserPayload,
    GenerateAnswerPayload,
    ObservationRequest,
    RequestViewPayload,
    RetrieveKnowledgePayload,
    RunRulesPayload,
    ViewType,
)
from realsight.workflow.models import MainAgentState


class PlannerError(RuntimeError):
    """规划器配置、模型响应或工具参数不满足课程安全约束时抛出。"""


class Planner(Protocol):
    """主工作流只依赖此协议，因此离线替身与 OpenAI 实现可以互换。"""

    def plan(self, state: MainAgentState) -> Action:
        """基于已验证 checkpoint 返回恰好一个允许的下一步动作。"""


_VISION_FIELDS = (
    "charger_label",
    "charger_max_power_w",
    "charger_protocol",
)
_RULE_FIELDS = ("charger_max_power_w", "charger_protocol")
_TOOL_NAMES = {
    ActionType.REQUEST_VIEW: "request_view",
    ActionType.ASK_USER: "ask_user",
    ActionType.RETRIEVE_KNOWLEDGE: "retrieve_knowledge",
    ActionType.RUN_RULES: "run_rules",
    ActionType.GENERATE_ANSWER: "generate_answer",
}


def _confirmed_charger_fields(state: MainAgentState) -> set[str]:
    """只有 confirmed 充电器字段可作为后续规则输入。"""

    return set(state.belief.confirmed)


def available_action_types(state: MainAgentState) -> tuple[ActionType, ...]:
    """按证据状态暴露最小工具集合，避免模型越过资料或规则边界。"""

    confirmed = _confirmed_charger_fields(state)
    visual_missing = set(_RULE_FIELDS) - confirmed
    choices: list[ActionType] = []
    if visual_missing:
        choices.append(ActionType.REQUEST_VIEW)
    if state.laptop_model_evidence is None or (
        state.lookup_status is not None and not state.laptop_specification_evidence
    ):
        choices.append(ActionType.ASK_USER)
    if (
        state.laptop_model_evidence is not None
        and not state.laptop_specification_evidence
        and state.lookup_status is None
    ):
        choices.append(ActionType.RETRIEVE_KNOWLEDGE)
    if (
        not visual_missing
        and state.laptop_specification_evidence
        and state.compatibility_result is None
    ):
        choices.append(ActionType.RUN_RULES)
    if state.compatibility_result is not None and state.final_answer is None:
        choices.append(ActionType.GENERATE_ANSWER)
    return tuple(choices)


def _action_id(state: MainAgentState, suffix: str) -> str:
    """用会话 ID、迭代号和动作名生成可读且稳定的教学用 ID。"""

    return f"{state.session.session_id}-{suffix}-{state.iteration + 1:03d}"


def _request_action(
    state: MainAgentState,
    *,
    features: tuple[str, ...],
    reason: str,
) -> Action:
    """把受限的视觉字段转换成固定为 back_label 的观察请求。"""

    allowed_features = tuple(field for field in features if field in _VISION_FIELDS)
    if not allowed_features:
        raise PlannerError("request_view requires at least one supported visual field")
    request_id = _action_id(state, "observation")
    request = ObservationRequest(
        request_id=request_id,
        session_id=state.session.session_id,
        target_id=state.target.target_id,
        view_type=ViewType.BACK_LABEL,
        required_features=allowed_features,
        instruction="请将 USB-C 充电器背面标签靠近镜头，保持文字清晰并减少反光。",
        reason=reason,
        correlation_id=request_id,
    )
    return Action(
        action_id=_action_id(state, "request-view"),
        target_id=state.target.target_id,
        action_type=ActionType.REQUEST_VIEW,
        reason=reason,
        payload=RequestViewPayload(request=request),
    )


def _ask_model_action(state: MainAgentState, *, question: str, reason: str) -> Action:
    """生成询问笔记本型号的 Action；Action 仍归属当前主目标充电器。"""

    return Action(
        action_id=_action_id(state, "ask-laptop-model"),
        target_id=state.target.target_id,
        action_type=ActionType.ASK_USER,
        reason=reason,
        payload=AskUserPayload(question=question, expected_field="laptop_model"),
    )


def tool_call_to_action(
    state: MainAgentState,
    tool_name: str,
    arguments: dict[str, Any],
) -> Action:
    """将模型函数调用收敛为既有 Action，并在执行前拒绝越权参数。"""

    allowed = {_TOOL_NAMES[item] for item in available_action_types(state)}
    if tool_name not in allowed:
        raise PlannerError(f"tool {tool_name!r} is not allowed in the current state")
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PlannerError("tool call requires a non-empty reason")

    if tool_name == "request_view":
        requested = arguments.get("features")
        if not isinstance(requested, list) or not all(
            isinstance(item, str) for item in requested
        ):
            raise PlannerError("request_view.features must be a list of strings")
        missing = set(_RULE_FIELDS) - _confirmed_charger_fields(state)
        features = tuple(item for item in requested if item in missing)
        # 标签文本会帮助审计；即使规则字段已经缺少多个，也只请求一张背面标签图。
        if "charger_label" in requested and "charger_label" not in features:
            features = ("charger_label", *features)
        return _request_action(state, features=features, reason=reason)
    if tool_name == "ask_user":
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise PlannerError("ask_user.question must be a non-empty string")
        return _ask_model_action(state, question=question, reason=reason)
    if tool_name == "retrieve_knowledge":
        if state.laptop_model_evidence is None:
            raise PlannerError("cannot retrieve specifications without a laptop model")
        return Action(
            action_id=_action_id(state, "retrieve-specification"),
            target_id=state.target.target_id,
            action_type=ActionType.RETRIEVE_KNOWLEDGE,
            reason=reason,
            payload=RetrieveKnowledgePayload(query=str(state.laptop_model_evidence.value)),
        )
    if tool_name == "run_rules":
        return Action(
            action_id=_action_id(state, "run-rules"),
            target_id=state.target.target_id,
            action_type=ActionType.RUN_RULES,
            reason=reason,
            payload=RunRulesPayload(rule_set="usb_c_compatibility_v1"),
        )
    if tool_name == "generate_answer":
        return Action(
            action_id=_action_id(state, "generate-answer"),
            target_id=state.target.target_id,
            action_type=ActionType.GENERATE_ANSWER,
            reason=reason,
            payload=GenerateAnswerPayload(conclusion_type="evidence_bound_usb_c_result"),
        )
    raise PlannerError(f"unknown planner tool: {tool_name}")


@dataclass(frozen=True, slots=True)
class DeterministicPlanner:
    """离线教学规划器：固定优先级使测试可重复，也展示模型应承担的职责。"""

    def plan(self, state: MainAgentState) -> Action:
        """先补充充电器标签，再询问型号、检索、运行规则和生成受约束说明。"""

        choices = available_action_types(state)
        if not choices:
            raise PlannerError("state is already terminal or has no safe next action")
        if ActionType.REQUEST_VIEW in choices:
            missing = set(_RULE_FIELDS) - _confirmed_charger_fields(state)
            features = tuple(
                field for field in _VISION_FIELDS if field == "charger_label" or field in missing
            )
            return _request_action(
                state,
                features=features,
                reason="需要从充电器背面标签确认最大功率与 USB PD 协议。",
            )
        if ActionType.ASK_USER in choices:
            suffix = (
                "本地资料库未能唯一匹配该型号，请提供机身铭牌上的完整型号。"
                if state.lookup_status is not None
                else "请提供笔记本的完整型号，例如机身底部或系统信息中的型号。"
            )
            return _ask_model_action(
                state,
                question=suffix,
                reason="USB-C 兼容性还需要笔记本的可核对型号。",
            )
        if ActionType.RETRIEVE_KNOWLEDGE in choices:
            return tool_call_to_action(
                state,
                "retrieve_knowledge",
                {"reason": "笔记本型号已确认，需要检索可追溯的本地规格。"},
            )
        if ActionType.RUN_RULES in choices:
            return tool_call_to_action(
                state,
                "run_rules",
                {"reason": "充电器与笔记本的规则输入已经具备。"},
            )
        return tool_call_to_action(
            state,
            "generate_answer",
            {"reason": "规则引擎已给出可解释结果，需要整理为证据化回答。"},
        )


def _tool_schema(tool_name: str) -> dict[str, Any]:
    """为当前允许动作构造最小 JSON Schema，不让模型发明隐藏字段。"""

    common: dict[str, Any] = {
        "type": "object",
        "properties": {"reason": {"type": "string", "minLength": 1}},
        "required": ["reason"],
        "additionalProperties": False,
    }
    if tool_name == "request_view":
        common["properties"]["features"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(_VISION_FIELDS)},
            "minItems": 1,
            "uniqueItems": True,
        }
        common["required"].append("features")
    elif tool_name == "ask_user":
        common["properties"]["question"] = {"type": "string", "minLength": 1}
        common["required"].append("question")
    return {"type": "function", "name": tool_name, "description": tool_name, "parameters": common}


def _state_summary(state: MainAgentState) -> dict[str, Any]:
    """只将必要的结构化状态发给模型，图片、密钥和完整日志不外发。"""

    return {
        "task": "USB-C charger compatibility evidence planning",
        "charger_confirmed_fields": sorted(state.belief.confirmed),
        "charger_probable_fields": sorted(state.belief.probable),
        "charger_conflicts": sorted(state.belief.conflicts),
        "laptop_model_known": state.laptop_model_evidence is not None,
        "catalog_status": state.lookup_status.value if state.lookup_status else None,
        "laptop_specification_fields": sorted(
            item.field for item in state.laptop_specification_evidence
        ),
        "rule_verdict": (
            state.compatibility_result.verdict.value
            if state.compatibility_result is not None
            else None
        ),
        "allowed_tools": [_TOOL_NAMES[item] for item in available_action_types(state)],
    }


@dataclass(slots=True)
class OpenAIPlanner:
    """真实模型规划器：将 Responses API 函数调用转换为受验证的 Action。"""

    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    client: Any | None = None

    def __post_init__(self) -> None:
        """仅在真正启用 OpenAI 模式时创建 SDK 客户端并检查环境变量。"""

        if self.client is not None:
            return
        if not os.environ.get("OPENAI_API_KEY"):
            raise PlannerError("OPENAI_API_KEY is required when agent.provider=openai")
        from openai import OpenAI

        self.client = OpenAI()

    def plan(self, state: MainAgentState) -> Action:
        """调用一次模型，让它在本状态允许的函数工具中选择唯一下一步。"""

        allowed = [_TOOL_NAMES[item] for item in available_action_types(state)]
        if not allowed:
            raise PlannerError("state has no allowed OpenAI planning tool")
        client = self.client
        if client is None:  # 供静态分析和防御性运行时检查使用。
            raise PlannerError("OpenAI client was not initialized")
        response = client.responses.create(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            instructions=(
                "You are the RealSight evidence planner. Choose exactly one available "
                "function. Never claim USB-C compatibility, never invent evidence, and "
                "never request unsupported views or fields."
            ),
            input=json.dumps(_state_summary(state), ensure_ascii=False),
            tools=[_tool_schema(name) for name in allowed],
        )
        function_calls = [
            item
            for item in getattr(response, "output", ())
            if getattr(item, "type", None) == "function_call"
        ]
        if len(function_calls) != 1:
            raise PlannerError("OpenAI planner must return exactly one function call")
        call = function_calls[0]
        try:
            arguments = json.loads(call.arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlannerError("OpenAI planner returned invalid function arguments") from exc
        if not isinstance(arguments, dict):
            raise PlannerError("OpenAI planner function arguments must be an object")
        return tool_call_to_action(state, call.name, arguments)


def planner_from_settings(provider: str, model: str, reasoning_effort: str) -> Planner:
    """应用层根据已经校验的配置选择双模式中的一个实现。"""

    if provider == "deterministic":
        return DeterministicPlanner()
    if provider == "openai":
        return OpenAIPlanner(model=model, reasoning_effort=reasoning_effort)
    raise PlannerError(f"unsupported planner provider: {provider}")


__all__ = [
    "DeterministicPlanner",
    "OpenAIPlanner",
    "Planner",
    "PlannerError",
    "available_action_types",
    "planner_from_settings",
    "tool_call_to_action",
]
