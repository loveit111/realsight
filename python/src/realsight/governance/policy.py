"""
RealSight 正式主工作流的可持久化治理预算。

文件整体逻辑
------------
第 7 章已经解释 capability、命令、观察、外部尝试和成本预算。本模块把这些概念收敛为
一个可进入 LangGraph checkpoint 的不可变 ``GovernanceUsage``。每次规划或外部操作
先通过 ``consume`` 生成新快照；越权或超限抛出结构化 ``GovernanceViolation``，主图再
把它转换为 FAILED 终态和 TASK_FAILED 事件，而不是无限循环或返回 500。

边界
----
预算是应用安全护栏，不是身份认证或计费系统。成本单位是相对单位，不代表美元；真实
生产环境仍需要独立的认证、密钥、审计保留和供应商用量对账。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictInt, field_validator, model_validator

from realsight.contracts import Identifier, StrictContract

DEFAULT_CAPABILITIES = frozenset(
    {
        "model.plan",
        "perception.observe",
        "rules.usb_c",
        "specification.retrieve",
    }
)


class GovernanceViolation(RuntimeError):
    """能力或预算拒绝；``code`` 可安全写入事件和 API 摘要。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


NonNegativeCount = Annotated[StrictInt, Field(ge=0)]
PositiveLimit = Annotated[StrictInt, Field(ge=1)]


class GovernanceUsage(StrictContract):
    """治理策略、上限和当前用量的单个可序列化 checkpoint 快照。"""

    policy_id: Identifier = "local-teaching-v1"
    allowed_capabilities: frozenset[Identifier] = DEFAULT_CAPABILITIES
    max_iterations: PositiveLimit = 24
    max_commands: PositiveLimit = 32
    max_observations: PositiveLimit = 8
    max_external_attempts: PositiveLimit = 24
    max_cost_units: PositiveLimit = 32
    commands_used: NonNegativeCount = 0
    observations_used: NonNegativeCount = 0
    external_attempts_used: NonNegativeCount = 0
    cost_units_used: NonNegativeCount = 0

    @field_validator("allowed_capabilities")
    @classmethod
    def capability_names_are_namespaced(
        cls, capabilities: frozenset[str]
    ) -> frozenset[str]:
        if not capabilities:
            raise ValueError("allowed_capabilities must not be empty")
        for capability in capabilities:
            if "." not in capability:
                raise ValueError("capabilities must use namespace.name syntax")
        return capabilities

    @model_validator(mode="after")
    def usage_does_not_exceed_limits(self) -> GovernanceUsage:
        pairs = (
            (self.commands_used, self.max_commands, "commands"),
            (self.observations_used, self.max_observations, "observations"),
            (
                self.external_attempts_used,
                self.max_external_attempts,
                "external attempts",
            ),
            (self.cost_units_used, self.max_cost_units, "cost units"),
        )
        for used, limit, name in pairs:
            if used > limit:
                raise ValueError(f"{name} usage must not exceed its limit")
        return self

    def ensure_iteration(self, iteration: int) -> None:
        """进入下一轮规划前检查执行轮数，防止跨多次 resume 的无限循环。"""

        if iteration >= self.max_iterations:
            raise GovernanceViolation(
                "max_iterations_exceeded",
                f"workflow iteration budget exhausted at {iteration}",
            )

    def require(self, capability: str) -> None:
        """检查当前策略是否允许指定能力。"""

        if capability not in self.allowed_capabilities:
            raise GovernanceViolation(
                "capability_denied",
                f"capability {capability!r} is not allowed by {self.policy_id}",
            )

    def consume(
        self,
        *,
        capability: str | None = None,
        commands: int = 0,
        observations: int = 0,
        external_attempts: int = 0,
        cost_units: int = 0,
    ) -> GovernanceUsage:
        """原子检查并返回新用量；任何一项失败都不会部分扣减。"""

        increments = (commands, observations, external_attempts, cost_units)
        if any(value < 0 for value in increments):
            raise ValueError("governance increments must not be negative")
        if capability is not None:
            self.require(capability)
        next_values = {
            "commands_used": self.commands_used + commands,
            "observations_used": self.observations_used + observations,
            "external_attempts_used": self.external_attempts_used + external_attempts,
            "cost_units_used": self.cost_units_used + cost_units,
        }
        checks = (
            (next_values["commands_used"], self.max_commands, "max_commands_exceeded"),
            (
                next_values["observations_used"],
                self.max_observations,
                "max_observations_exceeded",
            ),
            (
                next_values["external_attempts_used"],
                self.max_external_attempts,
                "max_external_attempts_exceeded",
            ),
            (
                next_values["cost_units_used"],
                self.max_cost_units,
                "max_cost_units_exceeded",
            ),
        )
        for used, limit, code in checks:
            if used > limit:
                raise GovernanceViolation(
                    code,
                    f"governance budget {code.removesuffix('_exceeded')} exhausted",
                )
        return GovernanceUsage.model_validate(
            {**self.model_dump(mode="python"), **next_values}
        )


__all__ = ["DEFAULT_CAPABILITIES", "GovernanceUsage", "GovernanceViolation"]
