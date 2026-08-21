"""
RealSight 各章节共用的“领域数据模型”。

这个文件解决什么问题
--------------------
后面的示例需要在多个模块之间传递“任务、现实目标、观察、证据、下一步动作、
运行事件”等信息。本文件把这些概念统一定义成带类型的数据对象，避免每一章
使用结构不同的临时字典。它只描述数据和最基础的状态约束，不负责摄像头、
OCR、大模型调用或 USB-C 兼容性判断。

核心逻辑
--------
1. TaskSession/RealityObject 描述“正在处理哪个任务、哪个现实物体”。
2. ObservationRequest/Observation 描述“想看什么”和“实际看到了什么”。
3. Evidence 把观察或计算结果包装成可追溯证据。
4. BeliefState 按 confirmed/probable/unknown/conflict 保存当前认知，并保证同一
   字段在任一时刻只能属于一种状态。
5. Action 表示系统建议的下一步，RunEvent 表示运行过程中已经发生的事情。
6. to_primitive() 把上述对象递归转为仅含 dict/list/字符串/数字的结构，便于
   JSON 序列化、日志记录和跨进程传输。

技术栈
------
- Python 标准库；没有第三方依赖。
- dataclasses：用较少样板代码定义结构化数据。
- Enum：限制状态和事件的可选值，减少拼写错误。
- typing：为字段、容器和返回值提供静态类型提示。

典型调用流程
------------
TaskSession + RealityObject
    -> 创建 BeliefState
    -> ObservationRequest 请求新视角
    -> Observation 返回观察结果
    -> 上层代码生成 Evidence
    -> BeliefState.add_evidence() 合并证据/发现冲突
    -> 上层代码据此产生 Action 或 RunEvent
    -> to_primitive() 转成可打印、可传输的数据

阅读建议：先看 Evidence 和 BeliefState，再看 add_evidence() 的四种状态分支；
其余 dataclass 主要是各模块之间传递信息的“标准信封”。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any


class EvidenceStatus(str, Enum):
    """某个事实字段当前的证据状态。继承 str 便于直接序列化为字符串。"""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class ActionType(str, Enum):
    """证据不完整时，规划器可以建议的下一步动作类型。"""

    REQUEST_VIEW = "request_view"
    ASK_USER = "ask_user"
    RETRIEVE_KNOWLEDGE = "retrieve_knowledge"
    RUN_RULES = "run_rules"
    GENERATE_ANSWER = "generate_answer"


class RunEventType(str, Enum):
    """运行时事件类型；事件描述“已经发生什么”，不同于 Action 的“接下来做什么”。"""

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


@dataclass(frozen=True)
class TaskSession:
    """一次任务会话；frozen=True 表示创建后不允许随意修改关键标识。"""

    session_id: str
    intent: str
    target_id: str
    status: str = "running"
    thread_id: str | None = None

    def __post_init__(self) -> None:
        """补全并校验 thread_id；本课程 MVP 要求它与 session_id 完全一致。"""

        # frozen dataclass 不能普通赋值，因此初始化阶段使用 object.__setattr__。
        if self.thread_id is None:
            object.__setattr__(self, "thread_id", self.session_id)
        elif self.thread_id != self.session_id:
            raise ValueError("Chapter MVP requires thread_id to equal session_id")


@dataclass(frozen=True)
class RealityObject:
    """摄像头或任务正在关注的现实物体，例如某个具体充电器。"""

    target_id: str
    category: str = "unknown"
    selected: bool = True
    tracking_status: str = "active"


@dataclass(frozen=True)
class ObservationRequest:
    """发给感知端的结构化请求：看哪个目标、哪个视角、要捕获哪些特征。"""

    target_id: str
    view_type: str
    required_features: tuple[str, ...]
    instruction: str
    reason: str


@dataclass(frozen=True)
class Observation:
    """感知端返回的一次合格观察；它本身还不是业务证据。"""

    observation_id: str
    target_id: str
    view_type: str
    quality_score: float
    image_path: str | None = None
    local_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """关于某个目标某一字段的证据，并记录来源、置信度和派生关系。"""

    evidence_id: str
    target_id: str
    field: str
    value: Any
    source_type: str
    source_id: str
    confidence: float
    status: EvidenceStatus = EvidenceStatus.CONFIRMED
    derived_from: tuple[str, ...] = ()


@dataclass
class BeliefState:
    """系统对一个目标的当前认知，是本项目最核心的状态容器。

    confirmed/probable/unknown/conflicts 是互斥的四个状态区；ledger 是不丢历史
    的总账；supporting_evidence_ids 记录 confirmed 字段由哪些证据共同支持。
    """

    target_id: str
    # key 都是业务字段名（如 supported_protocol），value 是对应证据。
    confirmed: dict[str, Evidence] = field(default_factory=dict)
    probable: dict[str, Evidence] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)
    conflicts: dict[str, list[Evidence]] = field(default_factory=dict)
    # observed_views 记录已经看过的视角，ledger 保存所有进入系统的证据。
    observed_views: set[str] = field(default_factory=set)
    ledger: dict[str, Evidence] = field(default_factory=dict)
    supporting_evidence_ids: dict[str, list[str]] = field(default_factory=dict)

    def add_evidence(self, evidence: Evidence) -> None:
        """加入一条证据，同时维持字段状态互斥。

        冲突是“粘性”的：字段一旦进入 conflict，新证据仍会记账，但不会在这里
        自动选择某一方。后续章节必须通过明确的复核流程来解决冲突。
        """

        # 第一道边界：绝不允许把目标 A 的证据写进目标 B 的 BeliefState。
        if evidence.target_id != self.target_id:
            raise ValueError("Evidence target does not match the belief state target")

        # evidence_id 是幂等键：完全相同的重复提交可忽略，内容不同则是数据错误。
        prior = self.ledger.get(evidence.evidence_id)
        if prior is not None:
            if prior == evidence:
                return
            raise ValueError(f"Evidence ID already exists: {evidence.evidence_id}")
        self.ledger[evidence.evidence_id] = evidence

        field_name = evidence.field
        # 已冲突字段不会被后来的普通证据悄悄“洗掉”，只把新证据追加进冲突集合。
        if field_name in self.conflicts:
            if evidence.status != EvidenceStatus.UNKNOWN:
                self.conflicts[field_name].append(evidence)
            self._assert_mutually_exclusive(field_name)
            return

        # 调用方也可以显式声明冲突；此时把字段原有证据一起移入 conflicts。
        if evidence.status == EvidenceStatus.CONFLICT:
            prior_items = self._current_field_evidence(field_name)
            self._set_conflict(field_name, [*prior_items, evidence])
            return

        # UNKNOWN 只在没有更强证据时生效，不能覆盖已确认或较可信的结果。
        if evidence.status == EvidenceStatus.UNKNOWN:
            if field_name not in self.confirmed and field_name not in self.probable:
                self.unknown.add(field_name)
            self._assert_mutually_exclusive(field_name)
            return

        # PROBABLE 不能降级 CONFIRMED；两个不同的 probable 值会形成冲突。
        if evidence.status == EvidenceStatus.PROBABLE:
            if field_name in self.confirmed:
                self._assert_mutually_exclusive(field_name)
                return
            existing = self.probable.get(field_name)
            if existing is not None and existing.value != evidence.value:
                self._set_conflict(field_name, [existing, evidence])
                return
            self.probable[field_name] = evidence
            self.supporting_evidence_ids.setdefault(field_name, []).append(evidence.evidence_id)
            self.unknown.discard(field_name)
            self._assert_mutually_exclusive(field_name)
            return

        # 能走到这里的证据默认为 CONFIRMED。不同的 confirmed 值必须转成冲突。
        existing = self.confirmed.get(field_name)
        if existing is not None and existing.value != evidence.value:
            previous_support = [
                self.ledger[evidence_id]
                for evidence_id in self.supporting_evidence_ids.get(field_name, [existing.evidence_id])
            ]
            self._set_conflict(field_name, [*previous_support, evidence])
            return

        # 同值 confirmed 证据可以共同支持该字段，因此保留所有 evidence_id。
        self.confirmed[field_name] = evidence
        self.probable.pop(field_name, None)
        self.unknown.discard(field_name)
        support = self.supporting_evidence_ids.setdefault(field_name, [])
        if evidence.evidence_id not in support:
            support.append(evidence.evidence_id)
        self._assert_mutually_exclusive(field_name)

    def confirmed_value(self, field_name: str) -> Any | None:
        """读取已确认值；字段未确认时返回 None。"""

        evidence = self.confirmed.get(field_name)
        return None if evidence is None else evidence.value

    def evidence_for(self, field_name: str) -> list[Evidence]:
        """返回某字段当前有效的支持证据；冲突时返回冲突双方。"""

        if field_name in self.conflicts:
            return list(self.conflicts[field_name])
        evidence_ids = self.supporting_evidence_ids.get(field_name, [])
        return [self.ledger[evidence_id] for evidence_id in evidence_ids]

    def state_of(self, field_name: str) -> EvidenceStatus | None:
        """查询字段位于四个状态区中的哪一个；从未出现则返回 None。"""

        if field_name in self.conflicts:
            return EvidenceStatus.CONFLICT
        if field_name in self.confirmed:
            return EvidenceStatus.CONFIRMED
        if field_name in self.probable:
            return EvidenceStatus.PROBABLE
        if field_name in self.unknown:
            return EvidenceStatus.UNKNOWN
        return None

    def _current_field_evidence(self, field_name: str) -> list[Evidence]:
        """内部辅助函数：取得字段转为冲突前已有的 confirmed/probable 证据。"""

        if field_name in self.confirmed:
            return self.evidence_for(field_name)
        if field_name in self.probable:
            return [self.probable[field_name]]
        return []

    def _set_conflict(self, field_name: str, evidence: list[Evidence]) -> None:
        """把字段原子地切换到 conflict 区，并按 evidence_id 去重。"""

        unique = {item.evidence_id: item for item in evidence}
        self.conflicts[field_name] = list(unique.values())
        self.confirmed.pop(field_name, None)
        self.probable.pop(field_name, None)
        self.unknown.discard(field_name)
        self.supporting_evidence_ids.pop(field_name, None)
        self._assert_mutually_exclusive(field_name)

    def _assert_mutually_exclusive(self, field_name: str) -> None:
        """防御性检查：同一字段最多只能出现在一个状态区。"""

        memberships = sum(
            (
                field_name in self.confirmed,
                field_name in self.probable,
                field_name in self.unknown,
                field_name in self.conflicts,
            )
        )
        if memberships > 1:
            raise AssertionError(f"Belief state invariant broken for field: {field_name}")


@dataclass(frozen=True)
class Action:
    """系统规划的下一步操作；payload 携带该操作所需的结构化参数。"""

    action_type: ActionType
    target_id: str
    reason: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunEvent:
    """一条可流式输出的运行记录；sequence 表示同一会话内的事件顺序。"""

    event_type: RunEventType
    session_id: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0


def to_primitive(value: Any) -> Any:
    """递归转换复杂对象，使结果可被 json.dumps() 等序列化工具处理。"""

    # Enum 要取 value，否则默认 JSON 编码器不了解枚举对象。
    if isinstance(value, Enum):
        return value.value
    # 不能用 asdict() 一步结束，因为字段内部仍可能包含 Enum、set 等类型。
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    if isinstance(value, set):
        return [to_primitive(item) for item in sorted(value)]
    return value
