"""
第 5 章 Demo：用 LangGraph interrupt 实现可暂停、可校验、可恢复的主动观察流程。

这个文件做什么
------------
第 4 章已经能判断下一步应“请求观察”或“询问用户”，但图运行到那里就结束了。
本文件把这些出口改造成真正的动态中断：工作流先保存 ObservationRequest/Action，
随后调用 interrupt() 暂停。外部程序完成拍摄或拿到用户回答后，使用同一个
thread_id 和 Command(resume=...) 恢复，工作流从 checkpoint 继续执行。

Demo 连续完成三次暂停：
1. 请求充电器背面标签，恢复时提交 Observation 与标签 Evidence。
2. 请求 USB-C 接口特写，恢复时提交 Observation 与端口 Evidence。
3. 询问笔记本型号，恢复时提交用户字段值。
三次输入齐全后，图产生 RUN_RULES Action，但仍不提前实现第 12 章兼容性规则。

使用的技术栈
------------
- LangGraph StateGraph：节点、条件边、循环、interrupt()、Command(resume=...)。
- LangGraph InMemorySaver：在当前 Python 进程内保存 thread checkpoint，供教学测试；
  它不是第 6 章的 SQLite 持久化，也不能跨进程恢复。
- Pydantic v2：校验暂停载荷、恢复输入、Observation/Evidence 关联与聚合状态。
- 第 4 章正式契约：TaskSession、ObservationRequest、Observation、Evidence、
  BeliefState、Action、RunEvent 和 RealSightGraphState。

调用流程
--------
build_initial_state()
    -> graph.invoke(initial_state, config={thread_id})
    -> assess_evidence：计算缺失字段
    -> prepare_observation：把请求与动作写入 State
    -> wait_for_observation：interrupt(JSON payload)，图暂停并保存 checkpoint
    -> graph.invoke(Command(resume=observation_result), 同一 config)
    -> wait_for_observation 从头重跑；interrupt 返回 resume 值
    -> Pydantic 校验 request_id/target_id/view/evidence source
    -> apply_resumed_evidence：合并证据并回到 assess_evidence
    -> 重复接口特写和用户型号中断
    -> prepare_rules -> END

关键设计边界
------------
- interrupt 所在等待节点在 interrupt 之前不执行数据库写入、网络调用或计数累加；
  因为恢复时该节点会从开头重新执行。
- 暂停 payload 只含 JSON 可序列化数据，不含 Pydantic 实例、函数、连接或摄像头。
- Observation 不是 Evidence；恢复可以带空 Evidence，图会继续请求缺失字段。
- 恢复输入不可信。错误输入抛出 ValidationError/ValueError 后，原 interrupt 仍可重试。
- 同一个 TaskSession 只使用一个 thread_id；换 thread_id 不是“新答案”，而是另一线程。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, interrupt
from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)


# 直接运行 examples 下的文件时，把项目根加入导入路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.models import (  # noqa: E402
    Action,
    ActionType,
    ActiveObservationState,
    AskUserPayload,
    AwaitingKind,
    BeliefState,
    Evidence,
    EvidenceStatus,
    FieldName,
    GraphRoute,
    Identifier,
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
    RealityObject,
    RequestViewPayload,
    RunEvent,
    RunEventType,
    RunRulesPayload,
    SessionStatus,
    StrictContract,
    TaskSession,
    TrackingStatus,
    ViewType,
    utc_now,
)

# 复用第 4 章已经验证过的 Evidence/Belief 创建函数，避免重写初始化逻辑。
try:
    from .ch04_state_and_contracts import build_belief, make_evidence
except ImportError:
    from ch04_state_and_contracts import build_belief, make_evidence


class ObservationPausePayload(StrictContract):
    """interrupt 暴露给外部调度器的观察请求。"""

    kind: Literal["observation_required"] = "observation_required"
    session_id: Identifier
    thread_id: Identifier
    action: Action


class UserPausePayload(StrictContract):
    """interrupt 暴露给用户界面的字段询问。"""

    kind: Literal["user_input_required"] = "user_input_required"
    session_id: Identifier
    thread_id: Identifier
    action: Action


PausePayload = Annotated[
    ObservationPausePayload | UserPausePayload,
    Field(discriminator="kind"),
]
PAUSE_ADAPTER = TypeAdapter(PausePayload)


class ObservationResume(StrictContract):
    """外部感知流程恢复图时提交的观察结果与可选 Evidence。"""

    kind: Literal["observation_result"] = "observation_result"
    observation: Observation
    evidence: tuple[Evidence, ...] = ()

    @field_validator("evidence")
    @classmethod
    def evidence_ids_are_unique(
        cls,
        evidence_items: tuple[Evidence, ...],
    ) -> tuple[Evidence, ...]:
        """同一次恢复不能重复提交相同 evidence_id。"""

        evidence_ids = [item.evidence_id for item in evidence_items]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("observation resume evidence IDs must be unique")
        return evidence_items


class UserResume(StrictContract):
    """用户恢复图时提交的单个业务字段。"""

    kind: Literal["user_input"] = "user_input"
    target_id: Identifier
    field: FieldName
    value: JsonValue
    source_id: Identifier

    @model_validator(mode="after")
    def user_value_is_present(self) -> UserResume:
        """None 代表仍未知，不能伪装成已经回答；0/False 仍是有效值。"""

        if self.value is None:
            raise ValueError("user resume value must not be null")
        return self


ResumePayload = Annotated[
    ObservationResume | UserResume,
    Field(discriminator="kind"),
]
RESUME_ADAPTER = TypeAdapter(ResumePayload)
IDENTIFIER_ADAPTER = TypeAdapter(Identifier)


# LangGraph 4.1 的严格 msgpack 模式只反序列化内置安全类型和显式白名单类型。
# 本章只允许当前 checkpoint 确实会保存的领域模型/枚举，避免开启 allow-all。
CHECKPOINT_ALLOWED_TYPES = (
    SessionStatus,
    TrackingStatus,
    ViewType,
    ObservationStatus,
    EvidenceStatus,
    ActionType,
    RunEventType,
    GraphRoute,
    AwaitingKind,
    TaskSession,
    RealityObject,
    ObservationRequest,
    QualitySignals,
    Observation,
    Evidence,
    BeliefState,
    RequestViewPayload,
    AskUserPayload,
    RunRulesPayload,
    Action,
    RunEvent,
    ActiveObservationState,
)


# required_fields 的顺序也是主动观察优先级：先标签，再接口，最后询问设备型号。
REQUIRED_FIELDS = (
    "charger_label",
    "charger_port_type",
    "laptop_model",
)

# 每个可观察字段都有明确视角和用户引导，不让模型自由编造摄像头指令。
OBSERVATION_SPECS: dict[str, dict[str, Any]] = {
    "charger_label": {
        "view_type": ViewType.BACK_LABEL,
        "instruction": "请将充电器背面标签移近摄像头，并保持文字清晰稳定",
        "reason": "需要核对充电器铭牌与标识",
    },
    "charger_port_type": {
        "view_type": ViewType.PORT_CLOSEUP,
        "instruction": "请展示充电器输出接口特写，确保接口轮廓完整可见",
        "reason": "需要确认输出端是否为 USB-C",
    },
}


def replace_session_status(
    session: TaskSession,
    status: SessionStatus,
) -> TaskSession:
    """创建状态更新后的新 TaskSession，不绕过 Pydantic 验证。"""

    values = session.model_dump(mode="python")
    values["status"] = status
    return TaskSession.model_validate(values)


def append_event(
    state: ActiveObservationState,
    event_type: RunEventType,
    message: str,
    data: dict[str, JsonValue] | None = None,
) -> tuple[RunEvent, ...]:
    """按严格递增 sequence 追加 RunEvent，返回新 tuple。"""

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


def build_initial_state(
    *,
    session_id: str = "session-ch05-001",
    target_id: str = "charger-ch05-001",
) -> ActiveObservationState:
    """创建第五章初始状态：功率与协议已知，标签、接口和笔记本型号未知。"""

    initial_evidence = (
        make_evidence(
            evidence_id=f"{target_id}-power",
            target_id=target_id,
            field="charger_max_power_w",
            value=65,
            source_type="user_note",
            source_id=f"{session_id}-note",
        ),
        make_evidence(
            evidence_id=f"{target_id}-protocol",
            target_id=target_id,
            field="charger_protocol",
            value="usb_pd",
            source_type="user_note",
            source_id=f"{session_id}-note",
        ),
    )
    started = RunEvent(
        event_id=f"{session_id}-event-001",
        session_id=session_id,
        sequence=1,
        event_type=RunEventType.TASK_STARTED,
        message="主动观察任务开始",
        data={"target_id": target_id},
    )
    return ActiveObservationState(
        session=TaskSession(
            session_id=session_id,
            thread_id=session_id,
            intent="补齐 USB-C 兼容性证据",
            target_id=target_id,
            status=SessionStatus.RUNNING,
        ),
        target=RealityObject(target_id=target_id, category="usb_c_charger"),
        belief=build_belief(target_id, initial_evidence),
        required_fields=REQUIRED_FIELDS,
        events=(started,),
    )


def assess_evidence_node(state: ActiveObservationState) -> dict[str, Any]:
    """计算所有未确认或存在冲突的必要字段。"""

    missing = tuple(
        field_name
        for field_name in state.required_fields
        if field_name not in state.belief.confirmed
        or field_name in state.belief.conflicts
    )
    return {"missing_fields": missing}


def route_after_assessment(
    state: ActiveObservationState,
) -> Literal["observe", "ask_user", "rules"]:
    """视觉字段优先主动观察，其余字段询问用户，无缺口则进入规则阶段。"""

    if any(field_name in OBSERVATION_SPECS for field_name in state.missing_fields):
        return "observe"
    if state.missing_fields:
        return "ask_user"
    return "rules"


def prepare_observation_node(state: ActiveObservationState) -> dict[str, Any]:
    """先把请求与 Action 提交为一个图步骤，下一节点才真正 interrupt。"""

    field_name = next(
        field_name
        for field_name in state.missing_fields
        if field_name in OBSERVATION_SPECS
    )
    spec = OBSERVATION_SPECS[field_name]
    request = ObservationRequest(
        request_id=f"{state.session.session_id}-obsreq-{state.iteration + 1:03d}",
        session_id=state.session.session_id,
        target_id=state.target.target_id,
        view_type=spec["view_type"],
        required_features=(field_name,),
        instruction=spec["instruction"],
        reason=spec["reason"],
        timeout_ms=10_000,
        correlation_id=f"active-plan-{state.iteration + 1:03d}",
    )
    action = Action(
        action_id=f"{state.session.session_id}-view-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.REQUEST_VIEW,
        reason=f"缺少可通过 {request.view_type.value} 补齐的 {field_name}",
        payload=RequestViewPayload(request=request),
    )

    # append_event 读取 state.pending_request；此时请求尚未写入旧 state，故显式给 data。
    events = append_event(
        state,
        RunEventType.ACTION_REQUIRED,
        f"需要新的 {request.view_type.value} 观察",
        {
            "action_id": action.action_id,
            "request_id": request.request_id,
            "required_features": list(request.required_features),
        },
    )
    return {
        "session": replace_session_status(
            state.session,
            SessionStatus.WAITING_OBSERVATION,
        ),
        "pending_request": request,
        "pending_actions": (action,),
        "route": GraphRoute.REQUEST_OBSERVATION,
        "awaiting_kind": AwaitingKind.OBSERVATION,
        "expected_user_field": None,
        "events": events,
    }


def wait_for_observation_node(state: ActiveObservationState) -> dict[str, Any]:
    """暂停并校验外部 ObservationResume；interrupt 前不得执行非幂等副作用。"""

    if state.pending_request is None or len(state.pending_actions) != 1:
        raise ValueError("observation wait requires one prepared request action")

    pause = ObservationPausePayload(
        session_id=state.session.session_id,
        thread_id=state.session.thread_id,
        action=state.pending_actions[0],
    )

    # 第一次运行在这里暂停；恢复时节点从开头重跑，并让 raw_resume 得到 Command 的值。
    raw_resume = interrupt(pause.model_dump(mode="json"))
    submission = validate_observation_resume(state, raw_resume)
    request = state.pending_request
    observation = submission.observation

    events = append_event(
        state,
        RunEventType.SERVICE_COMPLETED,
        f"观察 {observation.observation_id} 已恢复到工作流",
        {
            "observation_id": observation.observation_id,
            "evidence_count": len(submission.evidence),
            "request_id": request.request_id,
        },
    )
    return {
        "last_observation": observation,
        "resumed_evidence": submission.evidence,
        "completed_interrupts": state.completed_interrupts + 1,
        "events": events,
    }


def prepare_user_input_node(state: ActiveObservationState) -> dict[str, Any]:
    """为第一个非视觉缺口生成 ASK_USER Action，并先提交到 checkpoint。"""

    field_name = next(
        field_name
        for field_name in state.missing_fields
        if field_name not in OBSERVATION_SPECS
    )
    question = (
        "请提供需要充电的笔记本电脑准确型号"
        if field_name == "laptop_model"
        else f"请提供字段 {field_name}"
    )
    action = Action(
        action_id=f"{state.session.session_id}-user-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.ASK_USER,
        reason=f"字段 {field_name} 不能从当前充电器画面可靠推断",
        payload=AskUserPayload(question=question, expected_field=field_name),
    )
    events = append_event(
        state,
        RunEventType.ACTION_REQUIRED,
        f"等待用户提供 {field_name}",
        {"action_id": action.action_id, "expected_field": field_name},
    )
    return {
        "session": replace_session_status(state.session, SessionStatus.WAITING_USER),
        "pending_request": None,
        "pending_actions": (action,),
        "route": GraphRoute.AWAIT_USER,
        "awaiting_kind": AwaitingKind.USER_INPUT,
        "expected_user_field": field_name,
        "events": events,
    }


def wait_for_user_input_node(state: ActiveObservationState) -> dict[str, Any]:
    """暂停并把用户回答转换为带来源的 Evidence。"""

    if state.expected_user_field is None or len(state.pending_actions) != 1:
        raise ValueError("user wait requires one prepared ASK_USER action")
    pause = UserPausePayload(
        session_id=state.session.session_id,
        thread_id=state.session.thread_id,
        action=state.pending_actions[0],
    )

    raw_resume = interrupt(pause.model_dump(mode="json"))
    submission = validate_user_resume(state, raw_resume)

    evidence = Evidence(
        evidence_id=(
            f"{state.session.session_id}-user-evidence-{state.iteration + 1:03d}"
        ),
        target_id=submission.target_id,
        field=submission.field,
        value=submission.value,
        source_type="user_input",
        source_id=submission.source_id,
        confidence=1.0,
        status=EvidenceStatus.CONFIRMED,
    )
    events = append_event(
        state,
        RunEventType.TOOL_CALL_COMPLETED,
        f"用户输入 {submission.field} 已恢复到工作流",
        {"field": submission.field, "source_id": submission.source_id},
    )
    return {
        "resumed_evidence": (evidence,),
        "completed_interrupts": state.completed_interrupts + 1,
        "events": events,
    }


def merge_resumed_evidence(
    belief: BeliefState,
    evidence_items: tuple[Evidence, ...],
    observed_view: ViewType | None,
) -> BeliefState:
    """合并 confirmed/probable Evidence；不同值形成显式 conflict，不静默覆盖。"""

    confirmed = dict(belief.confirmed)
    probable = dict(belief.probable)
    unknown = set(belief.unknown)
    conflicts = {field: list(items) for field, items in belief.conflicts.items()}
    ledger = dict(belief.ledger)
    supporting = {
        field: list(evidence_ids)
        for field, evidence_ids in belief.supporting_evidence_ids.items()
    }
    observed_views = set(belief.observed_views)
    if observed_view is not None:
        observed_views.add(observed_view)

    for evidence in evidence_items:
        if evidence.target_id != belief.target_id:
            raise ValueError("resumed evidence target does not match belief target")

        previous_by_id = ledger.get(evidence.evidence_id)
        if previous_by_id is not None:
            if previous_by_id == evidence:
                continue
            raise ValueError(f"evidence ID already contains different data: {evidence.evidence_id}")
        ledger[evidence.evidence_id] = evidence
        field_name = evidence.field

        # 已处于冲突的字段保持冲突，新的独立来源只追加到账本和冲突集合。
        if field_name in conflicts:
            conflicts[field_name].append(evidence)
            continue

        existing = confirmed.get(field_name) or probable.get(field_name)
        if existing is not None and existing.value != evidence.value:
            prior_ids = supporting.get(field_name, [existing.evidence_id])
            prior_evidence = [ledger[evidence_id] for evidence_id in prior_ids]
            conflicts[field_name] = [*prior_evidence, evidence]
            confirmed.pop(field_name, None)
            probable.pop(field_name, None)
            supporting.pop(field_name, None)
            unknown.discard(field_name)
            continue

        if evidence.status == EvidenceStatus.CONFIRMED:
            confirmed[field_name] = evidence
            probable.pop(field_name, None)
        elif evidence.status == EvidenceStatus.PROBABLE and field_name not in confirmed:
            probable[field_name] = evidence
        else:
            # confirmed 字段不会被后来的 probable 证据降级，但新证据仍保留在 ledger。
            continue

        unknown.discard(field_name)
        evidence_ids = supporting.setdefault(field_name, [])
        if evidence.evidence_id not in evidence_ids:
            evidence_ids.append(evidence.evidence_id)

    return BeliefState(
        target_id=belief.target_id,
        confirmed=confirmed,
        probable=probable,
        unknown=frozenset(unknown),
        conflicts={field: tuple(items) for field, items in conflicts.items()},
        observed_views=frozenset(observed_views),
        ledger=ledger,
        supporting_evidence_ids={
            field: tuple(evidence_ids)
            for field, evidence_ids in supporting.items()
        },
    )


def apply_resumed_evidence_node(state: ActiveObservationState) -> dict[str, Any]:
    """把恢复证据原子合并到 BeliefState，清空一次性等待字段，然后重新规划。"""

    observed_view = (
        state.last_observation.view_type
        if state.awaiting_kind == AwaitingKind.OBSERVATION
        and state.last_observation is not None
        else None
    )
    belief = merge_resumed_evidence(
        state.belief,
        state.resumed_evidence,
        observed_view,
    )
    events = append_event(
        state,
        RunEventType.BELIEF_UPDATED,
        "恢复输入已合并到 BeliefState",
        {
            "evidence_ids": [item.evidence_id for item in state.resumed_evidence],
            "observed_view": observed_view.value if observed_view is not None else None,
        },
    )
    return {
        "session": replace_session_status(state.session, SessionStatus.RUNNING),
        "belief": belief,
        "pending_request": None,
        "pending_actions": (),
        "route": None,
        "awaiting_kind": None,
        "expected_user_field": None,
        "resumed_evidence": (),
        "missing_fields": (),
        "iteration": state.iteration + 1,
        "events": events,
    }


def prepare_rules_node(state: ActiveObservationState) -> dict[str, Any]:
    """证据齐全后生成 RUN_RULES Action，并正常结束本章工作流。"""

    action = Action(
        action_id=f"{state.session.session_id}-rules-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.RUN_RULES,
        reason="主动观察与用户补充已覆盖所有必要字段",
        payload=RunRulesPayload(rule_set="usb_c_compatibility_v1"),
    )
    events = append_event(
        state,
        RunEventType.ACTION_REQUIRED,
        "证据齐全，准备运行确定性兼容规则",
        {"action_id": action.action_id, "rule_set": "usb_c_compatibility_v1"},
    )
    return {
        "session": replace_session_status(state.session, SessionStatus.RUNNING),
        "pending_request": None,
        "pending_actions": (action,),
        "route": GraphRoute.RUN_RULES,
        "awaiting_kind": None,
        "expected_user_field": None,
        "events": events,
    }


def build_active_observation_graph(
    checkpointer: BaseCheckpointSaver | None = None,
) -> tuple[Any, BaseCheckpointSaver]:
    """组装循环状态图并启用 checkpointer；interrupt 没有 checkpointer 无法恢复。"""

    if checkpointer is None:
        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES,
        )
        saver = InMemorySaver(serde=serializer)
    else:
        saver = checkpointer
    workflow = StateGraph(ActiveObservationState)
    workflow.add_node("assess_evidence", assess_evidence_node)
    workflow.add_node("prepare_observation", prepare_observation_node)
    workflow.add_node("wait_for_observation", wait_for_observation_node)
    workflow.add_node("prepare_user_input", prepare_user_input_node)
    workflow.add_node("wait_for_user_input", wait_for_user_input_node)
    workflow.add_node("apply_resumed_evidence", apply_resumed_evidence_node)
    workflow.add_node("prepare_rules", prepare_rules_node)

    workflow.add_edge(START, "assess_evidence")
    workflow.add_conditional_edges(
        "assess_evidence",
        route_after_assessment,
        {
            "observe": "prepare_observation",
            "ask_user": "prepare_user_input",
            "rules": "prepare_rules",
        },
    )
    workflow.add_edge("prepare_observation", "wait_for_observation")
    workflow.add_edge("wait_for_observation", "apply_resumed_evidence")
    workflow.add_edge("prepare_user_input", "wait_for_user_input")
    workflow.add_edge("wait_for_user_input", "apply_resumed_evidence")
    workflow.add_edge("apply_resumed_evidence", "assess_evidence")
    workflow.add_edge("prepare_rules", END)
    return workflow.compile(checkpointer=saver), saver


def graph_config(thread_id: str) -> dict[str, Any]:
    """构造所有 invoke/get_state 共用的 thread 配置。"""

    validated_thread_id = IDENTIFIER_ADAPTER.validate_python(thread_id)
    return {
        "configurable": {"thread_id": validated_thread_id},
        "recursion_limit": 50,
    }


def extract_pause(result: dict[str, Any]) -> tuple[Interrupt, PausePayload]:
    """从 v1 invoke 结果读取唯一 interrupt，并验证其 JSON payload。"""

    interrupts = result.get("__interrupt__", ())
    if len(interrupts) != 1:
        raise ValueError(f"expected exactly one interrupt, got {len(interrupts)}")
    interrupt_info = interrupts[0]
    pause = PAUSE_ADAPTER.validate_python(interrupt_info.value)
    return interrupt_info, pause


def load_checkpoint_state(graph: Any, config: dict[str, Any]) -> ActiveObservationState:
    """读取当前 thread 的 checkpoint，并再次执行完整 Pydantic 聚合校验。"""

    snapshot = graph.get_state(config)
    return ActiveObservationState.model_validate(snapshot.values)


def validate_observation_resume(
    state: ActiveObservationState,
    raw_resume: Any,
) -> ObservationResume:
    """结合当前 checkpoint 校验观察恢复值，不只检查 payload 自身结构。"""

    submission = RESUME_ADAPTER.validate_python(raw_resume)
    if not isinstance(submission, ObservationResume):
        raise ValueError("observation interrupt requires observation_result resume payload")
    if state.pending_request is None:
        raise ValueError("observation resume requires pending_request")

    request = state.pending_request
    observation = submission.observation
    if observation.request_id != request.request_id:
        raise ValueError("observation request_id does not match pending request")
    if observation.target_id != state.target.target_id:
        raise ValueError("observation target_id does not match graph target")
    if observation.view_type != request.view_type:
        raise ValueError("observation view_type does not match requested view")
    if observation.status != ObservationStatus.ACCEPTED:
        raise ValueError("only accepted observation can resume evidence workflow")

    requested_fields = set(request.required_features)
    for evidence in submission.evidence:
        if evidence.target_id != state.target.target_id:
            raise ValueError("observation evidence target does not match graph target")
        if evidence.source_id != observation.observation_id:
            raise ValueError("observation evidence source_id must equal observation_id")
        if evidence.field not in requested_fields:
            raise ValueError("observation evidence field was not requested")
        if evidence.status not in {
            EvidenceStatus.CONFIRMED,
            EvidenceStatus.PROBABLE,
        }:
            raise ValueError("observation resume only accepts confirmed/probable evidence")
    return submission


def validate_user_resume(
    state: ActiveObservationState,
    raw_resume: Any,
) -> UserResume:
    """结合 expected_user_field 与 target_id 校验用户恢复值。"""

    submission = RESUME_ADAPTER.validate_python(raw_resume)
    if not isinstance(submission, UserResume):
        raise ValueError("user interrupt requires user_input resume payload")
    if submission.target_id != state.target.target_id:
        raise ValueError("user input target_id does not match graph target")
    if submission.field != state.expected_user_field:
        raise ValueError("user input field does not match expected field")
    return submission


def resume_active_observation(
    graph: Any,
    config: dict[str, Any],
    resume_payload: dict[str, Any],
    *,
    interrupt_id: str,
) -> dict[str, Any]:
    """在恢复前验证 thread、interrupt ID 与 payload，再定向恢复当前暂停。"""

    thread_id = config.get("configurable", {}).get("thread_id")
    if not isinstance(thread_id, str):
        raise ValueError("resume config requires a string thread_id")

    snapshot = graph.get_state(config)
    if not snapshot.values:
        raise ValueError(f"no checkpoint exists for thread_id: {thread_id}")
    state = ActiveObservationState.model_validate(snapshot.values)
    if state.session.thread_id != thread_id:
        raise ValueError("checkpoint session does not match requested thread_id")
    if not snapshot.next:
        raise ValueError(f"thread has no pending interrupt: {thread_id}")
    if snapshot.next[0] not in {"wait_for_observation", "wait_for_user_input"}:
        raise ValueError(f"thread is not waiting for resumable input: {thread_id}")

    validated_interrupt_id = IDENTIFIER_ADAPTER.validate_python(interrupt_id)
    active_interrupts = [
        interrupt_info
        for task in snapshot.tasks
        for interrupt_info in task.interrupts
    ]
    if len(active_interrupts) != 1:
        raise ValueError(
            f"thread must have exactly one active interrupt, got {len(active_interrupts)}"
        )
    active_interrupt = active_interrupts[0]
    if active_interrupt.id != validated_interrupt_id:
        raise ValueError("interrupt_id does not match the current thread interrupt")

    # 先在 checkpoint 外校验。若这里失败，Command 尚未发送，interrupt 不会被消费。
    if state.awaiting_kind == AwaitingKind.OBSERVATION:
        validated = validate_observation_resume(state, resume_payload)
    elif state.awaiting_kind == AwaitingKind.USER_INPUT:
        validated = validate_user_resume(state, resume_payload)
    else:
        raise ValueError("checkpoint has no declared awaiting_kind")

    # 节点恢复后会再次执行同一校验，防止调用者绕过应用层入口直接拼装状态。
    normalized_payload = validated.model_dump(mode="json")
    return graph.invoke(
        Command(resume={active_interrupt.id: normalized_payload}),
        config=config,
    )


def make_observation_resume(
    request: ObservationRequest,
    *,
    observation_id: str,
    field: str,
    value: JsonValue,
    confidence: float = 0.98,
) -> dict[str, JsonValue]:
    """构造可 JSON 传输的模拟观察恢复值，供 Demo 与测试使用。"""

    observation = Observation(
        observation_id=observation_id,
        request_id=request.request_id,
        target_id=request.target_id,
        view_type=request.view_type,
        captured_at=utc_now(),
        quality=QualitySignals(
            overall_score=0.94,
            sharpness=0.93,
            exposure=0.90,
            glare=0.92,
            target_ratio=0.76,
        ),
        local_features=(field,),
        image_path=f"artifacts/{observation_id}.jpg",
        status=ObservationStatus.ACCEPTED,
    )
    evidence = Evidence(
        evidence_id=f"{observation_id}-{field}",
        target_id=request.target_id,
        field=field,
        value=value,
        source_type="vision_extraction",
        source_id=observation_id,
        confidence=confidence,
        status=EvidenceStatus.CONFIRMED,
    )
    resume = ObservationResume(observation=observation, evidence=(evidence,))
    return resume.model_dump(mode="json")


def print_pause(
    step: str,
    interrupt_info: Interrupt,
    pause: PausePayload,
    state: ActiveObservationState,
) -> None:
    """打印暂停点、interrupt ID、下一节点和已保存业务状态。"""

    action = pause.action
    summary = {
        "step": step,
        "interrupt_id": interrupt_info.id,
        "pause_kind": pause.kind,
        "thread_id": pause.thread_id,
        "session_status": state.session.status.value,
        "route": state.route.value if state.route else None,
        "action_type": action.action_type.value,
        "missing_fields": list(state.missing_fields),
        "completed_interrupts": state.completed_interrupts,
    }
    if isinstance(action.payload, RequestViewPayload):
        summary["request"] = {
            "request_id": action.payload.request.request_id,
            "view_type": action.payload.request.view_type.value,
            "required_features": list(action.payload.request.required_features),
        }
    elif isinstance(action.payload, AskUserPayload):
        summary["question"] = action.payload.question
        summary["expected_field"] = action.payload.expected_field
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def run_demo() -> None:
    """连续暂停/恢复三次，并展示同一 thread checkpoint 的状态演进。"""

    graph, _ = build_active_observation_graph()
    initial_state = build_initial_state()
    config = graph_config(initial_state.session.thread_id)

    print("=== RealSight Chapter 5: active observation and interrupts ===")

    # 第一次运行：请求背面标签并暂停。
    result = graph.invoke(initial_state, config=config)
    interrupt_info, pause = extract_pause(result)
    state = load_checkpoint_state(graph, config)
    print_pause("1-back-label", interrupt_info, pause, state)
    assert state.pending_request is not None

    # 恢复标签观察；图自动循环并在接口特写处再次暂停。
    label_resume = make_observation_resume(
        state.pending_request,
        observation_id="observation-label-001",
        field="charger_label",
        value="USB-C PD 65W",
    )
    result = resume_active_observation(
        graph,
        config,
        label_resume,
        interrupt_id=interrupt_info.id,
    )
    interrupt_info, pause = extract_pause(result)
    state = load_checkpoint_state(graph, config)
    print_pause("2-port-closeup", interrupt_info, pause, state)
    assert state.pending_request is not None

    # 恢复接口观察；图自动循环并在询问笔记本型号处第三次暂停。
    port_resume = make_observation_resume(
        state.pending_request,
        observation_id="observation-port-001",
        field="charger_port_type",
        value="usb_c",
    )
    result = resume_active_observation(
        graph,
        config,
        port_resume,
        interrupt_id=interrupt_info.id,
    )
    interrupt_info, pause = extract_pause(result)
    state = load_checkpoint_state(graph, config)
    print_pause("3-laptop-model", interrupt_info, pause, state)

    # 最后提交用户输入；这次不会再 interrupt，而是正常到达 RUN_RULES 出口。
    user_resume = UserResume(
        target_id=state.target.target_id,
        field="laptop_model",
        value="ExampleBook Pro 14",
        source_id="user-message-005",
    ).model_dump(mode="json")
    final_result = resume_active_observation(
        graph,
        config,
        user_resume,
        interrupt_id=interrupt_info.id,
    )
    final_state = ActiveObservationState.model_validate(final_result)
    final_snapshot = graph.get_state(config)

    summary = {
        "step": "4-ready-for-rules",
        "thread_id": config["configurable"]["thread_id"],
        "route": final_state.route.value if final_state.route else None,
        "action_type": final_state.pending_actions[0].action_type.value,
        "confirmed_fields": sorted(final_state.belief.confirmed),
        "observed_views": sorted(view.value for view in final_state.belief.observed_views),
        "completed_interrupts": final_state.completed_interrupts,
        "checkpoint_next": list(final_snapshot.next),
        "event_count": len(final_state.events),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run_demo()
