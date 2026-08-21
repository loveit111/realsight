"""
RealSight 第 4 章正式契约模型：用 Pydantic 把“约定”变成可执行校验。

这个文件的整体逻辑
------------------
前三章为了降低学习门槛，使用 dataclass 表达 TaskSession、Observation、Evidence 等
领域对象。dataclass 能减少样板代码，但外部 JSON、模型输出、数据库记录进入程序时，
它不会自动拒绝多余字段、错误类型或跨对象矛盾。本文件保留前面已经确定的八个核心
类型名称，并用 Pydantic v2 为它们补上工程级入口校验。

本文件把校验分成三层：
1. 字段校验：ID 不能为空、置信度必须在 0～1、序号必须为正数。
2. 对象校验：失败 Observation 必须说明原因，Action 类型必须匹配 payload。
3. 聚合校验：BeliefState 的四个认知区互斥，GraphState 中所有 target_id 一致。

使用的技术栈
------------
- Python 3.12+ 类型标注：Enum、Literal、Annotated、联合类型和 tuple/frozenset。
- Pydantic v2：BaseModel、Field、ConfigDict、field_validator、model_validator。
- JSON 友好类型：JsonValue 与 model_dump_json()，为第 6 章检查点持久化做准备。
- LangGraph 状态思想：RealSightGraphState 只保存可序列化业务状态，不保存摄像头、
  gRPC channel、数据库连接或模型客户端等运行时资源。

典型调用流程
------------
外部 dict / JSON / Protobuf 适配结果
    -> TaskSession / ObservationRequest / Observation 等 Pydantic 模型
    -> 字段与跨字段校验
    -> Evidence 进入 BeliefState
    -> BeliefState 连同会话和目标组成 RealSightGraphState
    -> LangGraph 节点读取 state，返回少量字段更新
    -> 更新后的 state 再次由 Pydantic 校验并可序列化

阅读建议
--------
先读 StrictContract 和几个简单模型，再读 Action 的判别联合，最后读 BeliefState 与
RealSightGraphState 的 model_validator。验证器是在系统边界保护数据，不负责 OCR、
兼容性计算、节点调度或网络传输。

第 8 章迁移说明
--------------
本文件从课程根目录的 ``contracts/models.py`` 迁入正式 ``src`` 包。模型名称、字段和
验证语义保持不变；迁移只改变模块所有权，不借工程重构修改业务契约。旧路径现在只是
兼容入口，所有新代码都应从 ``realsight.contracts`` 导入。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)


# 这些可复用类型把常见约束集中起来，避免每个字段重复写相同的 Field 参数。
Identifier = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
NonEmptyText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1024),
]
FieldName = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
UnitFloat = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
PositiveSequence = Annotated[StrictInt, Field(ge=1)]


def utc_now() -> datetime:
    """生成带 UTC 时区的当前时间；无时区 datetime 不允许进入正式契约。"""

    return datetime.now(timezone.utc)


class StrictContract(BaseModel):
    """所有正式契约的共同规则：拒绝未知字段，并在赋值/默认值阶段执行校验。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class SessionStatus(str, Enum):
    """一次任务会话的生命周期状态。"""

    RUNNING = "running"
    WAITING_OBSERVATION = "waiting_observation"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TrackingStatus(str, Enum):
    """现实目标在 C++ 跟踪器中的逻辑状态。"""

    ACTIVE = "active"
    LOST = "lost"
    REACQUIRING = "reacquiring"


class ViewType(str, Enum):
    """Agent 可以向感知运行时请求的受控视角。"""

    FULL_OBJECT = "full_object"
    FRONT_LABEL = "front_label"
    BACK_LABEL = "back_label"
    PORT_CLOSEUP = "port_closeup"


class ObservationStatus(str, Enum):
    """感知运行时对一次观察请求的最终处理状态。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


class EvidenceStatus(str, Enum):
    """某个业务字段当前对应的证据状态。"""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class ActionType(str, Enum):
    """系统下一步可以执行或交给外部执行的动作类型。"""

    REQUEST_VIEW = "request_view"
    ASK_USER = "ask_user"
    RETRIEVE_KNOWLEDGE = "retrieve_knowledge"
    RUN_RULES = "run_rules"
    GENERATE_ANSWER = "generate_answer"


class RunEventType(str, Enum):
    """过程事件类型；Action 表示计划，RunEvent 表示已经发生的事实。"""

    TASK_STARTED = "task_started"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMED_OUT = "task_timed_out"
    MODEL_DECISION = "model_decision"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    SERVICE_REQUESTED = "service_requested"
    SERVICE_PROGRESS = "service_progress"
    SERVICE_COMPLETED = "service_completed"
    BELIEF_UPDATED = "belief_updated"
    ACTION_REQUIRED = "action_required"
    RULE_COMPLETED = "rule_completed"
    FINAL_ANSWER = "final_answer"
    TASK_FAILED = "task_failed"


class GraphRoute(str, Enum):
    """第四章最小状态图的三个业务出口。"""

    REQUEST_OBSERVATION = "request_observation"
    AWAIT_USER = "await_user"
    RUN_RULES = "run_rules"


class AwaitingKind(str, Enum):
    """检查点当前正在等待的外部输入类型；第 5 章中断恢复使用。"""

    OBSERVATION = "observation"
    USER_INPUT = "user_input"


class TaskSession(StrictContract):
    """一次可暂停、可恢复的任务；MVP 中 thread_id 与 session_id 一一对应。"""

    schema_version: Literal[1] = 1
    session_id: Identifier
    thread_id: Identifier
    intent: NonEmptyText
    target_id: Identifier
    status: SessionStatus = SessionStatus.RUNNING

    @model_validator(mode="after")
    def thread_matches_session(self) -> TaskSession:
        """固定会话与 LangGraph 线程的映射，避免恢复到另一项任务。"""

        if self.thread_id != self.session_id:
            raise ValueError("thread_id must equal session_id in the course MVP")
        return self


class RealityObject(StrictContract):
    """在多帧和多次观察之间持续存在的现实目标。"""

    schema_version: Literal[1] = 1
    target_id: Identifier
    category: NonEmptyText = "unknown"
    selected: bool = True
    tracking_status: TrackingStatus = TrackingStatus.ACTIVE


class ObservationRequest(StrictContract):
    """Python Agent 发给 C++ 感知运行时的结构化观察要求。"""

    schema_version: Literal[1] = 1
    request_id: Identifier
    session_id: Identifier
    target_id: Identifier
    view_type: ViewType
    required_features: tuple[FieldName, ...]
    instruction: NonEmptyText
    reason: NonEmptyText
    timeout_ms: Annotated[StrictInt, Field(ge=100, le=30_000)] = 5_000
    correlation_id: Identifier | None = None

    @field_validator("required_features")
    @classmethod
    def required_features_are_unique(
        cls,
        features: tuple[str, ...],
    ) -> tuple[str, ...]:
        """拒绝空列表和重复字段，避免 C++ 做重复工作。"""

        if not features:
            raise ValueError("required_features must contain at least one field")
        if len(set(features)) != len(features):
            raise ValueError("required_features must not contain duplicates")
        return features


class QualitySignals(StrictContract):
    """C++ 对关键帧给出的质量指标；0 最差，1 最好，glare 表示抗反光质量。"""

    overall_score: UnitFloat
    sharpness: UnitFloat | None = None
    exposure: UnitFloat | None = None
    glare: UnitFloat | None = None
    target_ratio: UnitFloat | None = None


class Observation(StrictContract):
    """C++ 返回的一次有效或失败观察；它还不是业务 Evidence。"""

    schema_version: Literal[1] = 1
    observation_id: Identifier
    request_id: Identifier
    target_id: Identifier
    view_type: ViewType
    captured_at: AwareDatetime
    quality: QualitySignals
    local_features: tuple[NonEmptyText, ...] = ()
    image_path: NonEmptyText | None = None
    status: ObservationStatus = ObservationStatus.ACCEPTED
    failure_reason: NonEmptyText | None = None
    # 这两个字段把最终关键帧追溯到第 9 章连续帧流；摄像头通常没有 source_position_ms。
    source_frame_sequence: Annotated[StrictInt, Field(ge=0)] | None = None
    source_position_ms: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def status_matches_failure_reason(self) -> Observation:
        """成功观察不能夹带失败原因；拒绝或失败必须说明原因。"""

        if self.status == ObservationStatus.ACCEPTED and self.failure_reason is not None:
            raise ValueError("accepted observation must not contain failure_reason")
        if self.status != ObservationStatus.ACCEPTED and self.failure_reason is None:
            raise ValueError("rejected or failed observation requires failure_reason")
        return self


class Evidence(StrictContract):
    """从观察、用户输入、资料或规则计算中提取出的可追溯业务事实。

    ``source_metadata`` 在第 11 章加入，用于保存 OCR 原文、文字区域和归一化方法等
    可审计上下文。它是可选的增量字段，旧的 Evidence 构造方式保持有效；图片二进制
    仍然只放在 artifact storage，不能塞进这里。
    """

    schema_version: Literal[1] = 1
    evidence_id: Identifier
    target_id: Identifier
    field: FieldName
    value: JsonValue
    source_type: NonEmptyText
    source_id: Identifier
    confidence: UnitFloat
    status: EvidenceStatus = EvidenceStatus.CONFIRMED
    derived_from: tuple[Identifier, ...] = ()
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_evidence_has_a_value(self) -> Evidence:
        """UNKNOWN 可以用 None 表示未知，其余证据必须携带一个具体值。"""

        if self.status != EvidenceStatus.UNKNOWN and self.value is None:
            raise ValueError("known or conflicting evidence must contain a value")
        return self


class BeliefState(StrictContract):
    """一个目标的认知快照：四个状态区互斥，ledger 保存完整证据账本。"""

    schema_version: Literal[1] = 1
    target_id: Identifier
    confirmed: dict[FieldName, Evidence] = Field(default_factory=dict)
    probable: dict[FieldName, Evidence] = Field(default_factory=dict)
    unknown: frozenset[FieldName] = Field(default_factory=frozenset)
    conflicts: dict[FieldName, tuple[Evidence, ...]] = Field(default_factory=dict)
    observed_views: frozenset[ViewType] = Field(default_factory=frozenset)
    ledger: dict[Identifier, Evidence] = Field(default_factory=dict)
    supporting_evidence_ids: dict[FieldName, tuple[Identifier, ...]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def belief_is_internally_consistent(self) -> BeliefState:
        """检查认知区互斥、目标隔离、账本引用和支持证据是否一致。"""

        confirmed_fields = set(self.confirmed)
        probable_fields = set(self.probable)
        unknown_fields = set(self.unknown)
        conflict_fields = set(self.conflicts)
        zones = [confirmed_fields, probable_fields, unknown_fields, conflict_fields]

        # 任意两个状态区都不能包含同一个业务字段。
        for index, left in enumerate(zones):
            for right in zones[index + 1 :]:
                overlap = left & right
                if overlap:
                    raise ValueError(f"belief status zones overlap: {sorted(overlap)}")

        # ledger 的键必须就是证据自身的 ID，并且所有证据都属于当前目标。
        for evidence_id, evidence in self.ledger.items():
            if evidence_id != evidence.evidence_id:
                raise ValueError("ledger key must equal evidence.evidence_id")
            self._assert_target(evidence)

        for field_name, evidence in self.confirmed.items():
            self._assert_active_evidence(field_name, evidence, EvidenceStatus.CONFIRMED)
        for field_name, evidence in self.probable.items():
            self._assert_active_evidence(field_name, evidence, EvidenceStatus.PROBABLE)

        for field_name, evidence_items in self.conflicts.items():
            if len(evidence_items) < 2:
                raise ValueError("a conflict must contain at least two evidence items")
            evidence_ids = [evidence.evidence_id for evidence in evidence_items]
            if len(set(evidence_ids)) != len(evidence_ids):
                raise ValueError("conflict evidence IDs must be unique")
            serialized_values = {
                json.dumps(
                    evidence.value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for evidence in evidence_items
            }
            if len(serialized_values) < 2:
                raise ValueError("conflict evidence must contain different values")
            for evidence in evidence_items:
                if evidence.field != field_name:
                    raise ValueError("conflict key must equal evidence.field")
                self._assert_target(evidence)
                self._assert_in_ledger(evidence)

        # supporting_evidence_ids 只用于 confirmed/probable 字段，并且引用必须可解析。
        active_supported_fields = confirmed_fields | probable_fields
        if set(self.supporting_evidence_ids) - active_supported_fields:
            raise ValueError("supporting evidence may only reference confirmed/probable fields")
        for field_name in active_supported_fields:
            evidence_ids = self.supporting_evidence_ids.get(field_name, ())
            if not evidence_ids:
                raise ValueError(f"active field {field_name} requires supporting evidence")
            if len(set(evidence_ids)) != len(evidence_ids):
                raise ValueError("supporting evidence IDs must be unique")
            active_evidence = self.confirmed.get(field_name) or self.probable[field_name]
            if active_evidence.evidence_id not in evidence_ids:
                raise ValueError("active evidence must be listed as supporting evidence")
            for evidence_id in evidence_ids:
                evidence = self.ledger.get(evidence_id)
                if evidence is None or evidence.field != field_name:
                    raise ValueError("supporting evidence reference is missing or mismatched")
        return self

    def _assert_target(self, evidence: Evidence) -> None:
        """内部辅助校验：证据不能跨目标写入。"""

        if evidence.target_id != self.target_id:
            raise ValueError("evidence target does not match belief target")

    def _assert_in_ledger(self, evidence: Evidence) -> None:
        """内部辅助校验：活跃证据必须能在总账中找到完全相同的记录。"""

        if self.ledger.get(evidence.evidence_id) != evidence:
            raise ValueError("active evidence must exist unchanged in ledger")

    def _assert_active_evidence(
        self,
        field_name: str,
        evidence: Evidence,
        expected_status: EvidenceStatus,
    ) -> None:
        """内部辅助校验：状态区的键、状态和账本引用必须三者一致。"""

        if evidence.field != field_name:
            raise ValueError("belief field key must equal evidence.field")
        if evidence.status != expected_status:
            raise ValueError("evidence status does not match its belief zone")
        self._assert_target(evidence)
        self._assert_in_ledger(evidence)


class RequestViewPayload(StrictContract):
    """REQUEST_VIEW 动作的专用参数。"""

    kind: Literal["request_view"] = "request_view"
    request: ObservationRequest


class AskUserPayload(StrictContract):
    """ASK_USER 动作的专用参数。"""

    kind: Literal["ask_user"] = "ask_user"
    question: NonEmptyText
    expected_field: FieldName


class RetrieveKnowledgePayload(StrictContract):
    """RETRIEVE_KNOWLEDGE 动作的专用参数。"""

    kind: Literal["retrieve_knowledge"] = "retrieve_knowledge"
    query: NonEmptyText


class RunRulesPayload(StrictContract):
    """RUN_RULES 动作的专用参数。"""

    kind: Literal["run_rules"] = "run_rules"
    rule_set: Identifier


class GenerateAnswerPayload(StrictContract):
    """GENERATE_ANSWER 动作的专用参数。"""

    kind: Literal["generate_answer"] = "generate_answer"
    conclusion_type: Identifier


# discriminator 让 Pydantic 先看 payload.kind，再选择正确的 payload 模型。
ActionPayload = Annotated[
    RequestViewPayload
    | AskUserPayload
    | RetrieveKnowledgePayload
    | RunRulesPayload
    | GenerateAnswerPayload,
    Field(discriminator="kind"),
]


class Action(StrictContract):
    """系统计划执行的下一步；action_type 与 payload.kind 必须表达同一动作。"""

    schema_version: Literal[1] = 1
    action_id: Identifier
    target_id: Identifier
    action_type: ActionType
    reason: NonEmptyText
    payload: ActionPayload

    @model_validator(mode="after")
    def action_type_matches_payload(self) -> Action:
        """防止出现“类型写 ask_user、参数却是 request_view”的危险组合。"""

        if self.action_type.value != self.payload.kind:
            raise ValueError("action_type must match payload.kind")
        if isinstance(self.payload, RequestViewPayload):
            if self.payload.request.target_id != self.target_id:
                raise ValueError("observation request target must match action target")
        return self


class RunEvent(StrictContract):
    """可写入日志并在第 14 章通过 WebSocket 推送的过程事件。"""

    schema_version: Literal[1] = 1
    event_id: Identifier
    session_id: Identifier
    sequence: PositiveSequence
    event_type: RunEventType
    message: NonEmptyText
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_id: Identifier | None = None


class RealSightGraphState(StrictContract):
    """LangGraph 的可检查点业务状态；不允许放入连接、服务对象或回调函数。"""

    schema_version: Literal[1] = 1
    session: TaskSession
    target: RealityObject
    belief: BeliefState
    required_fields: tuple[FieldName, ...]
    missing_fields: tuple[FieldName, ...] = ()
    pending_request: ObservationRequest | None = None
    pending_actions: tuple[Action, ...] = ()
    last_observation: Observation | None = None
    route: GraphRoute | None = None
    events: tuple[RunEvent, ...] = ()
    iteration: Annotated[StrictInt, Field(ge=0)] = 0

    @field_validator("required_fields", "missing_fields")
    @classmethod
    def field_lists_are_unique(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        """状态中的字段列表保持有序且不重复，便于产生稳定决策。"""

        if len(set(names)) != len(names):
            raise ValueError("state field lists must not contain duplicates")
        return names

    @model_validator(mode="after")
    def aggregate_ids_and_sequences_match(self) -> RealSightGraphState:
        """检查会话、目标、认知、请求、动作、观察和事件之间的引用完整性。"""

        target_id = self.session.target_id
        if self.target.target_id != target_id or self.belief.target_id != target_id:
            raise ValueError("session, target and belief must use the same target_id")
        if not self.required_fields:
            raise ValueError("required_fields must not be empty")
        if set(self.missing_fields) - set(self.required_fields):
            raise ValueError("missing_fields must be a subset of required_fields")

        if self.pending_request is not None:
            if self.pending_request.target_id != target_id:
                raise ValueError("pending request target does not match graph target")
            if self.pending_request.session_id != self.session.session_id:
                raise ValueError("pending request session does not match graph session")
        for action in self.pending_actions:
            if action.target_id != target_id:
                raise ValueError("pending action target does not match graph target")
            if isinstance(action.payload, RequestViewPayload):
                request = action.payload.request
                if request.session_id != self.session.session_id:
                    raise ValueError("request action session does not match graph session")
                if self.pending_request != request:
                    raise ValueError(
                        "request action payload must equal the graph pending_request"
                    )
        if self.last_observation is not None:
            if self.last_observation.target_id != target_id:
                raise ValueError("last observation target does not match graph target")

        request_actions = [
            action
            for action in self.pending_actions
            if action.action_type == ActionType.REQUEST_VIEW
        ]
        if self.pending_request is not None and len(request_actions) != 1:
            raise ValueError("pending_request requires exactly one REQUEST_VIEW action")
        if self.pending_request is None and request_actions:
            raise ValueError("REQUEST_VIEW action requires pending_request")
        if self.route == GraphRoute.REQUEST_OBSERVATION and self.pending_request is None:
            raise ValueError("request_observation route requires pending_request")
        if self.route == GraphRoute.AWAIT_USER:
            if not any(
                action.action_type == ActionType.ASK_USER
                for action in self.pending_actions
            ):
                raise ValueError("await_user route requires ASK_USER action")
        if self.route == GraphRoute.RUN_RULES:
            if not any(
                action.action_type == ActionType.RUN_RULES
                for action in self.pending_actions
            ):
                raise ValueError("run_rules route requires RUN_RULES action")

        previous_sequence = 0
        for event in self.events:
            if event.session_id != self.session.session_id:
                raise ValueError("event session does not match graph session")
            if event.sequence <= previous_sequence:
                raise ValueError("event sequence must be strictly increasing")
            previous_sequence = event.sequence
        return self


class ActiveObservationState(RealSightGraphState):
    """第 5 章主动观察图状态；放在稳定模块中，避免 checkpoint 记录 __main__。"""

    awaiting_kind: AwaitingKind | None = None
    expected_user_field: FieldName | None = None
    resumed_evidence: tuple[Evidence, ...] = ()
    completed_interrupts: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def waiting_metadata_is_consistent(self) -> ActiveObservationState:
        """等待类型、会话状态、请求和预期字段必须描述同一 checkpoint。"""

        if self.awaiting_kind == AwaitingKind.OBSERVATION:
            if self.session.status != SessionStatus.WAITING_OBSERVATION:
                raise ValueError("observation wait requires WAITING_OBSERVATION session")
            if self.pending_request is None:
                raise ValueError("observation wait requires pending_request")
            if self.expected_user_field is not None:
                raise ValueError("observation wait must not set expected_user_field")
        elif self.awaiting_kind == AwaitingKind.USER_INPUT:
            if self.session.status != SessionStatus.WAITING_USER:
                raise ValueError("user wait requires WAITING_USER session")
            if self.pending_request is not None:
                raise ValueError("user wait must not keep pending_request")
            if self.expected_user_field is None:
                raise ValueError("user wait requires expected_user_field")
        elif self.expected_user_field is not None:
            raise ValueError("expected_user_field requires user wait")
        return self


# 显式公共接口防止 ``import *`` 意外暴露 pydantic、datetime 等实现依赖。
__all__ = [
    "Action",
    "ActionPayload",
    "ActionType",
    "ActiveObservationState",
    "AskUserPayload",
    "AwaitingKind",
    "BeliefState",
    "Evidence",
    "EvidenceStatus",
    "FieldName",
    "GenerateAnswerPayload",
    "GraphRoute",
    "Identifier",
    "NonEmptyText",
    "Observation",
    "ObservationRequest",
    "ObservationStatus",
    "PositiveSequence",
    "QualitySignals",
    "RealityObject",
    "RealSightGraphState",
    "RequestViewPayload",
    "RetrieveKnowledgePayload",
    "RunEvent",
    "RunEventType",
    "RunRulesPayload",
    "SessionStatus",
    "StrictContract",
    "TaskSession",
    "TrackingStatus",
    "UnitFloat",
    "ViewType",
    "utc_now",
]
