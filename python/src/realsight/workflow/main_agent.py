"""
第 13 章主 Agent 的 LangGraph 完整证据循环。

文件逻辑
--------
本文件把前十二章的组件串成一个可暂停、可恢复的闭环：Planner 选择下一步，
观察恢复后交给 VisionEvidenceAgent，用户型号交给规格目录，随后由纯规则引擎
计算 USB-C 条件，最后生成一段不超出规则结果的说明。MainAgentState 同时保存
充电器与笔记本两个现实对象，避免跨目标混写 Evidence。

技术栈
------
- Python 3.12：不可变 Pydantic 数据、Protocol、json；
- LangGraph：StateGraph、interrupt、Command、checkpoint；
- LangGraph SqliteSaver 可由第 14 章应用层注入；
- 第 11 章视觉证据 Agent、第 12 章本地目录和确定性 USB-C 规则；
- 第 13 章 Planner 协议：可使用离线替身或 OpenAI Responses API。

调用流程
--------
initial_state -> plan -> request_view / ask_user / retrieve / rules / answer
    request_view -> interrupt -> ObservationResume -> VisionEvidenceAgent -> BeliefState
    ask_user    -> interrupt -> LaptopModelResume -> local specification lookup
    retrieve    -> local specification Evidence -> rules -> UsbCCompatibilityResult
    answer      -> evidence-bound text -> completed checkpoint。

边界
----
工作流不会直接传输视频帧、不会实现 OCR、不会查询互联网，也不会把模型输出当作
兼容性结论。C++ 仅通过第 10 章适配器提供 Observation；模型只选择受限动作。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt
from pydantic import TypeAdapter

from realsight.compatibility import (
    CompatibilityVerdict,
    LocalSpecificationCatalog,
    LookupStatus,
    UsbCCompatibilityResult,
    UsbCCompatibilityRules,
)
from realsight.contracts import (
    Action,
    ActionType,
    AskUserPayload,
    BeliefState,
    Evidence,
    EvidenceStatus,
    GenerateAnswerPayload,
    GraphRoute,
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
    RealityObject,
    RequestViewPayload,
    RetrieveKnowledgePayload,
    RunEvent,
    RunEventType,
    RunRulesPayload,
    SessionStatus,
    TaskSession,
    TrackingStatus,
    ViewType,
)
from realsight.governance import GovernanceUsage, GovernanceViolation
from realsight.vision import VisionEvidenceAgent
from realsight.workflow.models import (
    LaptopModelPause,
    LaptopModelResume,
    MainAgentState,
    ObservationPause,
    ObservationResume,
    PauseKind,
    ResumePayload,
)
from realsight.workflow.planner import Planner

# TypeAdapter 把不可信 JSON 恢复载荷变为判别联合，不能跳过 Pydantic 验证。
_RESUME_ADAPTER: TypeAdapter[ResumePayload] = TypeAdapter(ResumePayload)
_RULE_FIELDS = ("charger_max_power_w", "charger_protocol")


@dataclass(frozen=True, slots=True)
class MainAgentDependencies:
    """运行时依赖容器；它绝不进入 checkpoint，因此可放置客户端和服务实例。"""

    planner: Planner
    catalog: LocalSpecificationCatalog
    vision_agent: VisionEvidenceAgent
    rules: UsbCCompatibilityRules
    # deterministic 规划不消耗模型成本；真实 OpenAI 每次规划按一个相对成本单位计。
    planner_cost_units: int = 0

    def __post_init__(self) -> None:
        if self.planner_cost_units < 0:
            raise ValueError("planner_cost_units must not be negative")


def make_checkpoint_serializer() -> JsonPlusSerializer:
    """显式列出会进入 checkpoint 的类型，避免开启不受限的反序列化。"""

    # 课程 MVP 中所有状态都来自这些固定 Pydantic/Enum 类型；运行时 client 不在此列。
    allowed_types = (
        ActionType,
        EvidenceStatus,
        GraphRoute,
        ObservationStatus,
        RunEventType,
        SessionStatus,
        TrackingStatus,
        ViewType,
        LookupStatus,
        CompatibilityVerdict,
        PauseKind,
        TaskSession,
        RealityObject,
        ObservationRequest,
        Observation,
        QualitySignals,
        Evidence,
        BeliefState,
        RequestViewPayload,
        AskUserPayload,
        RetrieveKnowledgePayload,
        RunRulesPayload,
        GenerateAnswerPayload,
        Action,
        RunEvent,
        MainAgentState,
        UsbCCompatibilityResult,
        GovernanceUsage,
    )
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_types)


def graph_config(thread_id: str) -> dict[str, Any]:
    """统一构造 LangGraph 配置，所有恢复都必须使用原始 thread_id。"""

    if not thread_id:
        raise ValueError("thread_id must not be empty")
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}


def initial_state(
    *,
    session_id: str,
    charger_target_id: str,
    laptop_target_id: str,
    intent: str = "判断 USB-C 充电器与指定笔记本的已知兼容条件",
    governance_usage: GovernanceUsage | None = None,
) -> MainAgentState:
    """构造双目标任务的干净起点；规则字段未知而非被错误地填为零。"""

    if charger_target_id == laptop_target_id:
        raise ValueError("charger_target_id and laptop_target_id must be different")
    session = TaskSession(
        session_id=session_id,
        thread_id=session_id,
        intent=intent,
        target_id=charger_target_id,
    )
    return MainAgentState(
        session=session,
        target=RealityObject(
            target_id=charger_target_id,
            category="usb_c_charger",
            tracking_status=TrackingStatus.ACTIVE,
        ),
        belief=BeliefState(
            target_id=charger_target_id,
            unknown=frozenset(_RULE_FIELDS),
        ),
        required_fields=_RULE_FIELDS,
        missing_fields=_RULE_FIELDS,
        laptop_target=RealityObject(
            target_id=laptop_target_id,
            category="laptop",
            tracking_status=TrackingStatus.ACTIVE,
        ),
        governance_usage=governance_usage or GovernanceUsage(),
    )


def get_state(graph: Any, thread_id: str) -> MainAgentState:
    """从 checkpoint 取回并再次验证完整主工作流状态。"""

    snapshot = graph.get_state(graph_config(thread_id))
    if not snapshot.values:
        raise KeyError(f"task thread does not exist: {thread_id}")
    return MainAgentState.model_validate(snapshot.values)


def get_active_interrupt(graph: Any, thread_id: str) -> Interrupt:
    """读取唯一活跃 interrupt；API 恢复时不能相信旧页面缓存的 ID。"""

    snapshot = graph.get_state(graph_config(thread_id))
    active = [item for task in snapshot.tasks for item in task.interrupts]
    if len(active) != 1:
        raise ValueError(
            f"thread must have exactly one active interrupt, got {len(active)}"
        )
    return cast(Interrupt, active[0])


def _session_with_status(session: TaskSession, status: SessionStatus) -> TaskSession:
    """Pydantic 模型不可变，状态转换始终生成一个新会话快照。"""

    return session.model_copy(update={"status": status})


def _append_event(
    state: MainAgentState,
    event_type: RunEventType,
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[RunEvent, ...]:
    """以严格递增 sequence 记录可供第 14 章 WebSocket 推送的业务事实。"""

    sequence = state.events[-1].sequence + 1 if state.events else 1
    event = RunEvent(
        event_id=f"{state.session.session_id}-event-{sequence:03d}",
        session_id=state.session.session_id,
        sequence=sequence,
        event_type=event_type,
        message=message,
        data={} if data is None else data,
        correlation_id=(
            state.pending_request.request_id
            if state.pending_request is not None
            else None
        ),
    )
    return (*state.events, event)


def _canonical_value(value: Any) -> str:
    """比较 Evidence 时保持协议列表的语义顺序稳定，避免假冲突。"""

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        value = sorted(item.casefold() for item in value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_charger_evidence(
    belief: BeliefState,
    evidence_items: tuple[Evidence, ...],
    observed_view: ViewType,
) -> BeliefState:
    """将视觉 Evidence 合并为 confirmed/probable/conflict/ledger 四个互斥区域。"""

    confirmed = dict(belief.confirmed)
    probable = dict(belief.probable)
    unknown = set(belief.unknown)
    conflicts = {field: list(items) for field, items in belief.conflicts.items()}
    ledger = dict(belief.ledger)
    supporting = {
        field: list(ids) for field, ids in belief.supporting_evidence_ids.items()
    }
    observed_views = set(belief.observed_views)
    observed_views.add(observed_view)

    for item in evidence_items:
        if item.target_id != belief.target_id:
            raise ValueError("visual evidence target must equal charger belief target")
        earlier = ledger.get(item.evidence_id)
        if earlier is not None:
            if earlier != item:
                raise ValueError("evidence ID cannot be reused with different data")
            continue
        ledger[item.evidence_id] = item
        field = item.field
        if field in conflicts:
            conflicts[field].append(item)
            continue
        current = confirmed.get(field) or probable.get(field)
        if current is not None and _canonical_value(current.value) != _canonical_value(
            item.value
        ):
            prior_ids = supporting.get(field, [current.evidence_id])
            conflicts[field] = [*(ledger[item_id] for item_id in prior_ids), item]
            confirmed.pop(field, None)
            probable.pop(field, None)
            supporting.pop(field, None)
            unknown.discard(field)
            continue
        if item.status is EvidenceStatus.CONFIRMED:
            confirmed[field] = item
            probable.pop(field, None)
        elif item.status is EvidenceStatus.PROBABLE and field not in confirmed:
            probable[field] = item
        else:
            continue
        unknown.discard(field)
        field_ids = supporting.setdefault(field, [])
        if item.evidence_id not in field_ids:
            field_ids.append(item.evidence_id)

    return BeliefState(
        target_id=belief.target_id,
        confirmed=confirmed,
        probable=probable,
        unknown=frozenset(unknown),
        conflicts={field: tuple(items) for field, items in conflicts.items()},
        observed_views=frozenset(observed_views),
        ledger=ledger,
        supporting_evidence_ids={
            field: tuple(ids) for field, ids in supporting.items()
        },
    )


def _render_final_answer(state: MainAgentState) -> str:
    """只复述规则结果、Evidence ID 与未知边界，不让自然语言层扩大结论。"""

    result = state.compatibility_result
    if result is None:
        raise ValueError("final answer requires compatibility_result")
    evidence = "、".join(result.used_evidence_ids) or "无"
    unknown = "；".join(result.unknown_boundaries) or "没有额外未知边界"
    conditions = "；".join(result.conditions) or "无额外满足条件"
    return (
        f"规则结论：{result.verdict.value}。{result.summary}\n"
        f"使用的证据：{evidence}\n"
        f"条件或限制：{conditions}\n"
        f"仍未验证：{unknown}"
    )


def _router_after_plan(state: MainAgentState) -> str:
    """根据唯一 pending Action 选择下一个节点，模型文本不参与路由。"""

    if state.session.status in {
        SessionStatus.CANCELLED,
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
    }:
        return "end"
    if len(state.pending_actions) != 1:
        raise ValueError("planner must leave exactly one pending action")
    return state.pending_actions[0].action_type.value


def _governance_failure_update(
    state: MainAgentState,
    violation: GovernanceViolation,
) -> dict[str, Any]:
    """把治理拒绝变成可审计终态，不让预算耗尽表现为无限循环或 HTTP 500。"""

    return {
        "session": _session_with_status(state.session, SessionStatus.FAILED),
        "pending_actions": (),
        "pending_request": None,
        "route": None,
        "pause_kind": None,
        "resumed_payload": None,
        "events": _append_event(
            state,
            RunEventType.TASK_FAILED,
            "治理策略拒绝继续执行任务。",
            {
                "code": violation.code,
                "policy_id": state.governance_usage.policy_id,
                "detail": str(violation),
            },
        ),
    }


def _action_capability(action: Action) -> str | None:
    """把业务动作映射为治理 capability；纯用户交互与模板回答无需外部权限。"""

    return {
        ActionType.REQUEST_VIEW: "perception.observe",
        ActionType.RETRIEVE_KNOWLEDGE: "specification.retrieve",
        ActionType.RUN_RULES: "rules.usb_c",
    }.get(action.action_type)


def build_main_agent_graph(
    dependencies: MainAgentDependencies,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> tuple[Any, BaseCheckpointSaver[Any]]:
    """组装主循环；依赖闭包留在运行时，只有 MainAgentState 会写入 checkpoint。"""

    saver = checkpointer or InMemorySaver(serde=make_checkpoint_serializer())

    def plan_node(state: MainAgentState) -> dict[str, Any]:
        """调用规划器并记录模型或离线替身已经选择的受限 Action。"""

        if state.session.status in {
            SessionStatus.CANCELLED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }:
            return {}
        usage = state.governance_usage
        try:
            usage.ensure_iteration(state.iteration)
            # command 是规划轮本身的预算，必须在真实模型调用前检查；否则上限已耗尽时
            # 仍会产生一次不必要的外部调用和费用。
            usage = usage.consume(commands=1)
            if dependencies.planner_cost_units:
                usage = usage.consume(
                    capability="model.plan",
                    external_attempts=1,
                    cost_units=dependencies.planner_cost_units,
                )
            action = dependencies.planner.plan(state)
            capability = _action_capability(action)
            requests_observation = action.action_type is ActionType.REQUEST_VIEW
            usage = usage.consume(
                capability=capability,
                observations=(1 if requests_observation else 0),
                # 在进入 interrupt 前预留本次感知边界调用；自动 gRPC 和客户端手动
                # 采集都属于一次外部观察尝试，超限时不会启动摄像头。
                external_attempts=(1 if requests_observation else 0),
            )
        except GovernanceViolation as violation:
            return _governance_failure_update(state, violation)
        # RealSightGraphState 规定 REQUEST_VIEW 与 pending_request 必须是同一份契约；
        # 先一起写入 checkpoint，下一节点才把会话切换为 waiting_observation。
        if action.action_type is ActionType.REQUEST_VIEW:
            if not isinstance(action.payload, RequestViewPayload):
                raise ValueError("request_view action has an invalid payload")
            pending_request = action.payload.request
        else:
            pending_request = None
        return {
            "governance_usage": usage,
            "pending_actions": (action,),
            "pending_request": pending_request,
            "missing_fields": tuple(
                field
                for field in state.required_fields
                if field not in state.belief.confirmed
            ),
            "events": _append_event(
                state,
                RunEventType.MODEL_DECISION,
                "主 Agent 已选择下一步受限动作。",
                {
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                },
            ),
        }

    def prepare_observation_node(state: MainAgentState) -> dict[str, Any]:
        """先保存请求与等待状态，再进入可安全重放的 interrupt 节点。"""

        action = state.pending_actions[0]
        if not isinstance(action.payload, RequestViewPayload):
            raise ValueError("prepare observation requires RequestViewPayload")
        request = action.payload.request
        return {
            "session": _session_with_status(
                state.session, SessionStatus.WAITING_OBSERVATION
            ),
            "pending_request": request,
            "route": GraphRoute.REQUEST_OBSERVATION,
            "pause_kind": PauseKind.OBSERVATION,
            "events": _append_event(
                state,
                RunEventType.ACTION_REQUIRED,
                "需要一张通过质量门槛的充电器背面标签 Observation。",
                {
                    "request_id": request.request_id,
                    "features": list(request.required_features),
                },
            ),
        }

    def wait_for_observation_node(state: MainAgentState) -> dict[str, Any]:
        """只中断而不做 I/O；恢复时此节点重跑也不会重复 OCR 或 gRPC 调用。"""

        action = state.pending_actions[0]
        pause = ObservationPause(
            session_id=state.session.session_id,
            thread_id=state.session.thread_id,
            action=action,
        )
        resumed = interrupt(pause.model_dump(mode="json"))
        return {"resumed_payload": resumed}

    def apply_observation_node(state: MainAgentState) -> dict[str, Any]:
        """验证恢复 Observation，调用视觉子 Agent，再原子合并可追溯 Evidence。"""

        payload = _RESUME_ADAPTER.validate_python(state.resumed_payload)
        if not isinstance(payload, ObservationResume):
            raise ValueError("observation pause requires observation resume payload")
        request = state.pending_request
        if request is None:
            raise ValueError("observation resume requires pending_request")
        observation = payload.observation
        if observation.request_id != request.request_id:
            raise ValueError("observation request_id does not match pending request")
        if observation.target_id != state.target.target_id:
            raise ValueError("observation target does not match charger target")
        if observation.view_type != request.view_type:
            raise ValueError("observation view does not match requested view")
        if observation.status is not ObservationStatus.ACCEPTED:
            raise ValueError("only accepted observations may resume this workflow")

        try:
            # 感知尝试已在 REQUEST_VIEW 规划时预留；恢复后只为 OCR 扣减一次，
            # 超限时不会启动识别后端。
            usage = state.governance_usage.consume(external_attempts=1)
        except GovernanceViolation as violation:
            return _governance_failure_update(state, violation)

        extraction = dependencies.vision_agent.extract(
            observation, request.required_features
        )
        belief = merge_charger_evidence(
            state.belief, extraction.evidence, observation.view_type
        )
        event_data = {
            "observation_id": observation.observation_id,
            "evidence_ids": [item.evidence_id for item in extraction.evidence],
            "gaps": [gap.code for gap in extraction.gaps],
        }
        events = _append_event(
            state,
            RunEventType.SERVICE_COMPLETED,
            "视觉证据子 Agent 已处理恢复的 Observation。",
            event_data,
        )
        intermediate = state.model_copy(update={"events": events})
        return {
            "session": _session_with_status(state.session, SessionStatus.RUNNING),
            "governance_usage": usage,
            "belief": belief,
            "last_observation": observation,
            "pending_request": None,
            "pending_actions": (),
            "route": None,
            "pause_kind": None,
            "resumed_payload": None,
            "iteration": state.iteration + 1,
            "events": _append_event(
                intermediate,
                RunEventType.BELIEF_UPDATED,
                "已将视觉 Evidence 与证据缺口写入充电器 BeliefState。",
                {"confirmed_fields": sorted(belief.confirmed)},
            ),
        }

    def prepare_laptop_model_node(state: MainAgentState) -> dict[str, Any]:
        """保存询问 Action 后进入用户中断，笔记本目标仍独立于 charger BeliefState。"""

        return {
            "session": _session_with_status(state.session, SessionStatus.WAITING_USER),
            "route": GraphRoute.AWAIT_USER,
            "pause_kind": PauseKind.LAPTOP_MODEL,
            "events": _append_event(
                state,
                RunEventType.ACTION_REQUIRED,
                "需要用户确认笔记本完整型号。",
                {
                    "field": "laptop_model",
                    "laptop_target_id": state.laptop_target.target_id,
                },
            ),
        }

    def wait_for_laptop_model_node(state: MainAgentState) -> dict[str, Any]:
        """中断前不写入用户输入，防止客户端重连导致同一型号重复入账。"""

        pause = LaptopModelPause(
            session_id=state.session.session_id,
            thread_id=state.session.thread_id,
            action=state.pending_actions[0],
            laptop_target_id=state.laptop_target.target_id,
        )
        resumed = interrupt(pause.model_dump(mode="json"))
        return {"resumed_payload": resumed}

    def apply_laptop_model_node(state: MainAgentState) -> dict[str, Any]:
        """把用户输入变成单独的 laptop_model Evidence，而不是直接相信为规格。"""

        payload = _RESUME_ADAPTER.validate_python(state.resumed_payload)
        if not isinstance(payload, LaptopModelResume):
            raise ValueError("laptop model pause requires laptop_model resume payload")
        evidence = Evidence(
            evidence_id=f"{state.session.session_id}-laptop-model-{state.iteration + 1:03d}",
            target_id=state.laptop_target.target_id,
            field="laptop_model",
            value=payload.value,
            source_type="user_input",
            source_id=payload.source_id,
            confidence=1.0,
            status=EvidenceStatus.CONFIRMED,
        )
        return {
            "session": _session_with_status(state.session, SessionStatus.RUNNING),
            "laptop_model_evidence": evidence,
            "laptop_specification_evidence": (),
            "lookup_status": None,
            "lookup_message": None,
            "pending_actions": (),
            "route": None,
            "pause_kind": None,
            "resumed_payload": None,
            "iteration": state.iteration + 1,
            "events": _append_event(
                state,
                RunEventType.BELIEF_UPDATED,
                "已记录用户提供的笔记本型号，等待资料检索验证。",
                {
                    "evidence_id": evidence.evidence_id,
                    "laptop_target_id": evidence.target_id,
                },
            ),
        }

    def retrieve_specification_node(state: MainAgentState) -> dict[str, Any]:
        """调用第 12 章目录；未命中和歧义是下一轮询问的业务状态，不是猜测。"""

        model_evidence = state.laptop_model_evidence
        if model_evidence is None:
            raise ValueError("specification retrieval requires laptop model evidence")
        try:
            usage = state.governance_usage.consume(
                capability="specification.retrieve", external_attempts=1
            )
        except GovernanceViolation as violation:
            return _governance_failure_update(state, violation)
        lookup = dependencies.catalog.lookup(
            model_evidence, laptop_target_id=state.laptop_target.target_id
        )
        return {
            "governance_usage": usage,
            "laptop_specification_evidence": lookup.evidence,
            "lookup_status": lookup.status,
            "lookup_message": lookup.message,
            "pending_actions": (),
            "iteration": state.iteration + 1,
            "events": _append_event(
                state,
                RunEventType.TOOL_CALL_COMPLETED,
                "本地笔记本规格目录已返回可审计检索结果。",
                {
                    "lookup_status": lookup.status.value,
                    "evidence_ids": [item.evidence_id for item in lookup.evidence],
                },
            ),
        }

    def run_rules_node(state: MainAgentState) -> dict[str, Any]:
        """只能调用确定性规则引擎；planner 或 OCR 都不能替代这一节点。"""

        result = dependencies.rules.evaluate(
            charger_target_id=state.target.target_id,
            charger_evidence=tuple(state.belief.ledger.values()),
            laptop_target_id=state.laptop_target.target_id,
            laptop_evidence=state.laptop_specification_evidence,
        )
        return {
            "compatibility_result": result,
            "pending_actions": (),
            "route": None,
            "iteration": state.iteration + 1,
            "events": _append_event(
                state,
                RunEventType.RULE_COMPLETED,
                "USB-C 兼容性规则引擎已完成，不代表真实硬件已充电成功。",
                {
                    "verdict": result.verdict.value,
                    "evidence_ids": list(result.used_evidence_ids),
                },
            ),
        }

    def generate_answer_node(state: MainAgentState) -> dict[str, Any]:
        """由模板受限地呈现规则结果，避免最终文案扩大确定性结论。"""

        result = state.compatibility_result
        if result is None:
            raise ValueError("generate answer requires compatibility result")
        answer = _render_final_answer(state)
        return {
            "session": _session_with_status(state.session, SessionStatus.COMPLETED),
            "pending_actions": (),
            "route": None,
            "final_answer": answer,
            "iteration": state.iteration + 1,
            "events": _append_event(
                state,
                RunEventType.FINAL_ANSWER,
                "已生成带证据来源和未知边界的最终说明。",
                {"verdict": result.verdict.value},
            ),
        }

    workflow = StateGraph(MainAgentState)
    workflow.add_node("plan", plan_node)
    workflow.add_node("prepare_observation", prepare_observation_node)
    workflow.add_node("wait_for_observation", wait_for_observation_node)
    workflow.add_node("apply_observation", apply_observation_node)
    workflow.add_node("prepare_laptop_model", prepare_laptop_model_node)
    workflow.add_node("wait_for_laptop_model", wait_for_laptop_model_node)
    workflow.add_node("apply_laptop_model", apply_laptop_model_node)
    workflow.add_node("retrieve_specification", retrieve_specification_node)
    workflow.add_node("run_rules", run_rules_node)
    workflow.add_node("generate_answer", generate_answer_node)
    workflow.add_edge(START, "plan")
    workflow.add_conditional_edges(
        "plan",
        _router_after_plan,
        {
            "request_view": "prepare_observation",
            "ask_user": "prepare_laptop_model",
            "retrieve_knowledge": "retrieve_specification",
            "run_rules": "run_rules",
            "generate_answer": "generate_answer",
            "end": END,
        },
    )
    workflow.add_edge("prepare_observation", "wait_for_observation")
    workflow.add_edge("wait_for_observation", "apply_observation")
    workflow.add_edge("apply_observation", "plan")
    workflow.add_edge("prepare_laptop_model", "wait_for_laptop_model")
    workflow.add_edge("wait_for_laptop_model", "apply_laptop_model")
    workflow.add_edge("apply_laptop_model", "plan")
    workflow.add_edge("retrieve_specification", "plan")
    workflow.add_edge("run_rules", "plan")
    workflow.add_edge("generate_answer", END)
    return workflow.compile(checkpointer=saver), saver


def resume_main_agent(
    graph: Any,
    *,
    thread_id: str,
    interrupt_id: str,
    payload: dict[str, Any],
) -> MainAgentState:
    """在发送 Command 前验证 thread、interrupt 和载荷，失败时保留原暂停可重试。"""

    state = get_state(graph, thread_id)
    if state.session.status in {SessionStatus.CANCELLED, SessionStatus.FAILED}:
        raise ValueError(f"{state.session.status.value} task cannot be resumed")
    active = get_active_interrupt(graph, thread_id)
    if active.id != interrupt_id:
        raise ValueError("interrupt_id does not match current active interrupt")
    validated = _RESUME_ADAPTER.validate_python(payload)
    if state.pause_kind is PauseKind.OBSERVATION and not isinstance(
        validated, ObservationResume
    ):
        raise ValueError("current interrupt requires an observation payload")
    if state.pause_kind is PauseKind.LAPTOP_MODEL and not isinstance(
        validated, LaptopModelResume
    ):
        raise ValueError("current interrupt requires a laptop_model payload")
    # Command(resume=...) 会消费 interrupt。所有能在 checkpoint 外检查的关联关系
    # 必须先检查，避免错误请求 ID 使用户失去原本可重试的暂停点。
    if isinstance(validated, ObservationResume):
        request = state.pending_request
        observation = validated.observation
        if request is None:
            raise ValueError("observation resume requires pending_request")
        if observation.request_id != request.request_id:
            raise ValueError("observation request_id does not match pending request")
        if observation.target_id != state.target.target_id:
            raise ValueError("observation target does not match charger target")
        if observation.view_type != request.view_type:
            raise ValueError("observation view does not match requested view")
        if observation.status is not ObservationStatus.ACCEPTED:
            raise ValueError("only accepted observations may resume this workflow")
    graph.invoke(
        Command(resume={active.id: validated.model_dump(mode="json")}),
        config=graph_config(thread_id),
    )
    return get_state(graph, thread_id)


def cancel_main_agent(graph: Any, *, thread_id: str, reason: str) -> MainAgentState:
    """把取消写回 checkpoint；恢复入口会拒绝已取消会话。"""

    state = get_state(graph, thread_id)
    if state.session.status in {SessionStatus.COMPLETED, SessionStatus.FAILED}:
        raise ValueError(f"{state.session.status.value} task cannot be cancelled")
    cancelled = state.model_copy(
        update={
            "session": _session_with_status(state.session, SessionStatus.CANCELLED),
            "pending_actions": (),
            "pending_request": None,
            "route": None,
            "pause_kind": None,
            "cancel_reason": reason,
            "events": _append_event(
                state,
                RunEventType.TASK_CANCELLED,
                "任务已被取消；不会再接受恢复输入。",
                {"reason": reason},
            ),
        }
    )
    graph.update_state(graph_config(thread_id), cancelled.model_dump(mode="python"))
    return get_state(graph, thread_id)


__all__ = [
    "MainAgentDependencies",
    "build_main_agent_graph",
    "cancel_main_agent",
    "get_active_interrupt",
    "get_state",
    "graph_config",
    "initial_state",
    "make_checkpoint_serializer",
    "merge_charger_evidence",
    "resume_main_agent",
]
