"""
第 13 章主 Agent 的状态与暂停契约。

文件逻辑
--------
前面章节的 RealSightGraphState 只描述一个持续被观察的现实目标。USB-C
兼容性却同时涉及充电器和笔记本，若把笔记本规格塞进 charger 的
BeliefState，会破坏 Evidence.target_id 的含义。本文件用 MainAgentState
继承既有状态，并额外保存 laptop_target、笔记本资料 Evidence、规则结果和
最终回答，保持两个现实对象的证据边界清晰。

技术栈
------
- Python 3.12 类型标注与 StrEnum；
- Pydantic v2 StrictContract：检查 checkpoint 可以安全序列化；
- LangGraph interrupt/resume：本文件只定义暂停和恢复载荷，不启动图；
- 第 4、11、12 章的 contracts、视觉 Evidence 与 USB-C 规则结果。

调用流程
--------
application 创建 MainAgentState
    -> workflow 生成 Action 并在 ObservationPause/LaptopModelPause 处暂停
    -> API 或 C++ 适配器提交 ObservationResume/LaptopModelResume
    -> workflow 读取本文件的双目标上下文，调用视觉、检索和规则模块
    -> 写回 compatibility_result 与 final_answer。

边界
----
这里不访问模型、摄像头、SQLite 或网络。它只约束数据形状；实际规划在
planner.py，流程编排在 main_agent.py，HTTP 接口在第 14 章 application 层。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from realsight.compatibility import LookupStatus, UsbCCompatibilityResult
from realsight.contracts import (
    Action,
    Evidence,
    Identifier,
    NonEmptyText,
    Observation,
    RealityObject,
    RealSightGraphState,
    StrictContract,
)
from realsight.governance import GovernanceUsage


class PauseKind(StrEnum):
    """主工作流能安全交给外部世界完成的两种中断。"""

    OBSERVATION = "observation_required"
    LAPTOP_MODEL = "laptop_model_required"


class MainAgentState(RealSightGraphState):
    """第 13 章的完整业务 checkpoint，扩展而不改名既有核心状态。"""

    laptop_target: RealityObject
    governance_usage: GovernanceUsage = Field(default_factory=GovernanceUsage)
    laptop_model_evidence: Evidence | None = None
    laptop_specification_evidence: tuple[Evidence, ...] = ()
    lookup_status: LookupStatus | None = None
    lookup_message: NonEmptyText | None = None
    compatibility_result: UsbCCompatibilityResult | None = None
    final_answer: NonEmptyText | None = None
    resumed_payload: dict[str, JsonValue] | None = None
    pause_kind: PauseKind | None = None
    cancel_reason: NonEmptyText | None = None

    @field_validator("laptop_specification_evidence")
    @classmethod
    def specification_evidence_ids_are_unique(
        cls, evidence_items: tuple[Evidence, ...]
    ) -> tuple[Evidence, ...]:
        """同一次检索不能把同一条资料 Evidence 重复写入 checkpoint。"""

        evidence_ids = [item.evidence_id for item in evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("laptop specification evidence IDs must be unique")
        return evidence_items

    @model_validator(mode="after")
    def two_reality_objects_remain_separate(self) -> MainAgentState:
        """防止充电器证据、笔记本资料和规则结论跨对象混写。"""

        if self.laptop_target.target_id == self.target.target_id:
            raise ValueError("charger target and laptop target must be different")
        if self.laptop_model_evidence is not None:
            if self.laptop_model_evidence.target_id != self.laptop_target.target_id:
                raise ValueError("laptop model evidence belongs to another target")
            if self.laptop_model_evidence.field != "laptop_model":
                raise ValueError("laptop model evidence field must be laptop_model")
        for item in self.laptop_specification_evidence:
            if item.target_id != self.laptop_target.target_id:
                raise ValueError("specification evidence belongs to another target")
        if self.compatibility_result is not None:
            if self.compatibility_result.charger_target_id != self.target.target_id:
                raise ValueError("compatibility result charger target is inconsistent")
            if (
                self.compatibility_result.laptop_target_id
                != self.laptop_target.target_id
            ):
                raise ValueError("compatibility result laptop target is inconsistent")
        if self.final_answer is not None and self.compatibility_result is None:
            raise ValueError("final answer requires a deterministic rule result")
        if self.lookup_status is None and self.lookup_message is not None:
            raise ValueError("lookup message requires a lookup status")
        return self


class ObservationPause(StrictContract):
    """发给 C++ 运行时或 API 客户端的、可序列化的观察暂停信息。"""

    kind: Literal[PauseKind.OBSERVATION] = PauseKind.OBSERVATION
    session_id: Identifier
    thread_id: Identifier
    action: Action


class LaptopModelPause(StrictContract):
    """发给用户界面的笔记本型号补充请求。"""

    kind: Literal[PauseKind.LAPTOP_MODEL] = PauseKind.LAPTOP_MODEL
    session_id: Identifier
    thread_id: Identifier
    action: Action
    laptop_target_id: Identifier


class ObservationResume(StrictContract):
    """C++ gRPC 适配器或回放器恢复观察暂停时提交的 Observation。"""

    kind: Literal["observation"] = "observation"
    observation: Observation


class LaptopModelResume(StrictContract):
    """用户确认笔记本型号时的恢复载荷，模型名尚未等于资料已经可信。"""

    kind: Literal["laptop_model"] = "laptop_model"
    value: NonEmptyText
    source_id: Identifier


ResumePayload = Annotated[
    ObservationResume | LaptopModelResume,
    Field(discriminator="kind"),
]


__all__ = [
    "LaptopModelPause",
    "LaptopModelResume",
    "MainAgentState",
    "ObservationPause",
    "ObservationResume",
    "PauseKind",
    "ResumePayload",
]
