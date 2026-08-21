"""
第 2 章：从零实现一个“同步、可流式观察”的最小 Tool Calling Agent。

这个文件做什么
------------
第 1 章已经有证据规划和 USB-C 确定性规则，本章在它们外面加一层 Agent 运行
协议。模型每轮只能做两件事之一：请求调用一个工具，或给出最终回答。Agent
负责校验并执行工具，把结果送回模型，然后把全过程逐条输出为 RunEvent。

核心角色
--------
- ModelAdapter：模型接口。生产环境可接真实 LLM，本例使用透明、可测试的
  DeterministicModelAdapter（确定性替身），所以运行不需要 API Key。
- ToolSpec/ToolRegistry：工具的说明、必填参数、处理函数以及执行前安全校验。
- MinimalAgent：实现 ``模型决策 -> 工具调用 -> 工具结果 -> 再决策`` 循环。
- RunEvent：把开始、模型决策、工具请求/完成、外部动作、最终回答或失败，按
  sequence 编号后流式交给调用方。

实际使用的技术栈
----------------
- Python 标准库。
- dataclasses + Enum + typing.Protocol：定义结构化调用协议和可替换接口。
- Iterator/yield：实现同步事件流，调用方无需等到整次任务结束才看到进度。
- Callable：把普通 Python 函数注册为工具。
- 第 1 章的 BeliefState、Action、证据规则与回答生成函数。

调用流程
--------
run_demo()
    -> 创建 MinimalAgent(model, tool_registry)
    -> stream(...) 或 invoke(...)
    -> model.decide(...) 先选择 inspect_evidence_gaps
    -> ToolRegistry.execute(...) 校验工具名、参数和 target_id 后执行
    -> 证据不足：产生 ACTION_REQUIRED，模型返回“需要外部输入”
       或
       证据齐全：模型选择 evaluate_mvp_charging，工具执行第 1 章规则
    -> 模型根据工具结果生成 FINAL_ANSWER

``stream`` 逐条 yield 事件，适合终端、SSE 或 WebSocket 实时展示；``invoke``
内部仍走同一条 stream，只是收集全部事件后一次返回 AgentRunResult。

重要边界
--------
这里没有真实大模型，也没有并发、超时、取消、重试或持久化。确定性模型替身
只为讲清 Agent 与工具之间的数据协议；第 3 章会加入 asyncio 运行时能力。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Protocol

# 兼容“作为包导入”和“直接运行脚本”两种方式。
try:
    from .ch01_evidence_loop import (
        add_demo_device_evidence,
        add_label_evidence,
        build_demo_belief,
        build_evidence_answer,
        evaluate_compatibility,
        missing_evidence_fields,
        plan_next_actions,
    )
    from .realsight_domain import (
        BeliefState,
        Observation,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )
except ImportError:
    from ch01_evidence_loop import (  # type: ignore[no-redef]
        add_demo_device_evidence,
        add_label_evidence,
        build_demo_belief,
        build_evidence_answer,
        evaluate_compatibility,
        missing_evidence_fields,
        plan_next_actions,
    )
    from realsight_domain import (  # type: ignore[no-redef]
        BeliefState,
        Observation,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )


class DecisionKind(str, Enum):
    """模型一次决策的两种合法形态：调用工具，或结束并回答。"""

    TOOL_CALL = "tool_call"
    FINAL_ANSWER = "final_answer"


@dataclass(frozen=True)
class ToolCall:
    """模型提出的一次结构化工具调用；call_id 用于关联请求和结果。"""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelDecision:
    """模型单轮输出；两个可选字段由 kind 决定，而且必须严格二选一。"""

    kind: DecisionKind
    summary: str
    tool_call: ToolCall | None = None
    final_answer: str | None = None

    def __post_init__(self) -> None:
        """在对象创建时验证联合类型约束，尽早暴露错误的模型适配器输出。"""

        # TOOL_CALL 必须有 tool_call，并且不能同时夹带 final_answer。
        if self.kind == DecisionKind.TOOL_CALL and self.tool_call is None:
            raise ValueError("A tool-call decision requires tool_call")
        if self.kind == DecisionKind.TOOL_CALL and self.final_answer is not None:
            raise ValueError("A tool-call decision cannot include final_answer")
        # FINAL_ANSWER 与上面相反：必须有回答，而且不能再请求工具。
        if self.kind == DecisionKind.FINAL_ANSWER and self.final_answer is None:
            raise ValueError("A final-answer decision requires final_answer")
        if self.kind == DecisionKind.FINAL_ANSWER and self.tool_call is not None:
            raise ValueError("A final-answer decision cannot include tool_call")


@dataclass(frozen=True)
class ToolResult:
    """工具执行后的结构化返回值，通过 call_id 与原 ToolCall 对应。"""

    call_id: str
    name: str
    output: dict[str, Any]


@dataclass(frozen=True)
class ToolContext:
    """工具执行所需、但不应由模型随意填写的受信任运行上下文。"""

    session: TaskSession
    belief: BeliefState


# 所有同步工具处理函数都遵守同一签名，才能被注册表统一调用。
ToolHandler = Callable[[dict[str, Any], ToolContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """一项工具的注册信息：名字/说明供模型看，参数/handler 供运行时使用。"""

    name: str
    description: str
    required_arguments: tuple[str, ...]
    handler: ToolHandler


class ModelAdapter(Protocol):
    """模型适配器接口；任何实现只要具有 decide() 即可接入 MinimalAgent。"""

    def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        """返回一个结构化工具调用，或一个最终回答。"""


class ToolRegistry:
    """同步工具注册表，集中处理查找、参数校验、目标隔离和执行。"""

    def __init__(self, tools: tuple[ToolSpec, ...]) -> None:
        # 转成 name -> spec 字典以便 O(1) 查找；长度变化意味着有重名被覆盖。
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")

    @property
    def descriptions(self) -> tuple[dict[str, Any], ...]:
        """返回可暴露给模型/事件消费者的工具元数据，不泄露 Python handler。"""

        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "required_arguments": tool.required_arguments,
            }
            for tool in self._tools.values()
        )

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        """验证模型提出的调用，执行处理函数，再包装成统一 ToolResult。"""

        # 模型只能调用白名单中已注册的工具。
        tool = self._tools.get(call.name)
        if tool is None:
            raise ValueError(f"Tool is not registered: {call.name}")

        # 教学版只校验必填参数是否存在；生产版通常还会做完整 schema/type 校验。
        missing = [name for name in tool.required_arguments if name not in call.arguments]
        if missing:
            raise ValueError(f"Tool {call.name} is missing arguments: {missing}")

        # target_id 同时匹配 session 和 belief，防止工具越界操作另一个现实目标。
        target_id = call.arguments.get("target_id")
        if target_id != context.session.target_id or target_id != context.belief.target_id:
            raise ValueError("Tool call target does not match session and belief target")

        output = tool.handler(call.arguments, context)
        return ToolResult(call_id=call.call_id, name=call.name, output=output)


class DeterministicModelAdapter:
    """遵循固定决策策略的“假模型”，用来透明展示 Agent 协议。

    它不是 AI：只检查上一次工具名/结果并按 if 分支选择下一步，因此测试稳定。
    """

    def __init__(self) -> None:
        # 单调递增的编号让每个 ToolCall 都有可追踪的唯一 ID。
        self._next_call_number = 1

    def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        """根据工具历史决定下一步；user_input 在这个固定策略中暂不参与判断。"""

        del user_input
        # 第 1 轮没有工具结果：任何结论前都必须先检查证据缺口。
        if not tool_results:
            return self._tool_decision(
                "inspect_evidence_gaps",
                {"target_id": session.target_id},
                "先检查当前证据缺口，不能直接回答兼容性。",
            )

        # 本例策略只关心最近一次工具结果；真实模型通常会看到完整消息历史。
        last_result = tool_results[-1]
        if last_result.name == "inspect_evidence_gaps":
            actions = last_result.output["actions"]
            # plan_next_actions 返回 run_rules，表示证据门禁已经通过。
            if actions and actions[0]["action_type"] == "run_rules":
                return self._tool_decision(
                    "evaluate_mvp_charging",
                    {"target_id": session.target_id},
                    "证据门禁已通过，调用确定性规则。",
                )
            # 需要拍照/询问/检索都属于当前同步 Agent 无法自行跨越的外部边界。
            action_names = ", ".join(action["action_type"] for action in actions)
            return ModelDecision(
                kind=DecisionKind.FINAL_ANSWER,
                summary="存在外部输入边界，本轮不能继续调用规则。",
                final_answer=f"当前证据不足，需要执行：{action_names}。",
            )

        # 规则工具已经生成证据化文本，模型适配器只负责把它作为最终答案返回。
        if last_result.name == "evaluate_mvp_charging":
            return ModelDecision(
                kind=DecisionKind.FINAL_ANSWER,
                summary="规则工具已返回，生成证据化回答。",
                final_answer=str(last_result.output["answer"]),
            )

        return ModelDecision(
            kind=DecisionKind.FINAL_ANSWER,
            summary="收到无法解释的工具结果，停止执行。",
            final_answer="Agent 无法解释工具结果，任务停止。",
        )

    def _tool_decision(
        self,
        name: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> ModelDecision:
        """创建带递增 call_id 的工具调用决策，减少 decide() 中的重复代码。"""

        call = ToolCall(
            call_id=f"call-{self._next_call_number:03d}",
            name=name,
            arguments=arguments,
        )
        self._next_call_number += 1
        return ModelDecision(
            kind=DecisionKind.TOOL_CALL,
            summary=summary,
            tool_call=call,
        )


@dataclass(frozen=True)
class AgentRunResult:
    """invoke() 的聚合返回值：最终文本、完整事件轨迹以及是否失败。"""

    final_answer: str | None
    events: tuple[RunEvent, ...]
    failed: bool


class MinimalAgent:
    """同步最小 Agent 运行时：循环询问模型、执行工具并产出事件。"""

    def __init__(
        self,
        model: ModelAdapter,
        tools: ToolRegistry,
        *,
        max_model_steps: int = 4,
    ) -> None:
        # 最大步数是治理边界，防止模型不断调用工具形成无限循环。
        if max_model_steps < 1:
            raise ValueError("max_model_steps must be positive")
        self._model = model
        self._tools = tools
        self._max_model_steps = max_model_steps

    def stream(
        self,
        *,
        session: TaskSession,
        belief: BeliefState,
        user_input: str,
    ) -> Iterator[RunEvent]:
        """运行 Agent，并按发生顺序逐条 yield RunEvent。

        这是生成器函数：调用 stream() 时不会立刻跑完整任务；调用方每次迭代，
        执行才继续到下一个 yield，因此可以实时显示中间过程。
        """

        sequence = 0
        # 每次工具结果会追加到历史中，下轮模型据此决定继续调用还是回答。
        tool_results: list[ToolResult] = []

        def event(
            event_type: RunEventType,
            message: str,
            data: dict[str, Any] | None = None,
        ) -> RunEvent:
            """创建本会话的下一条事件，并自动递增 sequence。"""

            nonlocal sequence
            sequence += 1
            return RunEvent(
                event_type=event_type,
                session_id=session.session_id,
                message=message,
                data={} if data is None else data,
                sequence=sequence,
            )

        # 在任何模型/工具操作前检查目标隔离，错误时以事件报告而不是抛出到界面。
        if session.target_id != belief.target_id:
            yield event(
                RunEventType.TASK_FAILED,
                "任务目标与 Belief State 目标不一致",
                {"session_target": session.target_id, "belief_target": belief.target_id},
            )
            return

        # 第一条正常事件说明任务已开始，并公开本次 Agent 可用的工具白名单。
        yield event(
            RunEventType.TASK_STARTED,
            "最小 Agent 开始执行",
            {
                "intent": session.intent,
                "target_id": session.target_id,
                "available_tools": self._tools.descriptions,
            },
        )

        # Agent 核心循环：一次迭代对应“一次模型决策 + 最多一次工具执行”。
        for _ in range(self._max_model_steps):
            try:
                decision = self._model.decide(
                    user_input=user_input,
                    session=session,
                    tool_results=tuple(tool_results),
                )
            except Exception as exc:
                yield event(
                    RunEventType.TASK_FAILED,
                    "模型适配器调用失败",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                return
            # 无论模型要调工具还是回答，都先记录 MODEL_DECISION，便于审计。
            yield event(
                RunEventType.MODEL_DECISION,
                decision.summary,
                {
                    "decision_kind": decision.kind.value,
                    "tool_call": to_primitive(decision.tool_call),
                },
            )

            # 最终回答是本轮正常终点；yield 后立即 return，避免继续循环。
            if decision.kind == DecisionKind.FINAL_ANSWER:
                yield event(
                    RunEventType.FINAL_ANSWER,
                    "Agent 返回本轮最终输出",
                    {"answer": decision.final_answer},
                )
                return

            # 经过 ModelDecision.__post_init__ 后，TOOL_CALL 分支必定带有调用对象。
            call = decision.tool_call
            assert call is not None
            yield event(
                RunEventType.TOOL_CALL_REQUESTED,
                f"请求调用工具 {call.name}",
                {
                    "call_id": call.call_id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                },
            )

            # ToolRegistry 负责工具白名单、必填参数和 target_id 校验。
            try:
                result = self._tools.execute(
                    call,
                    ToolContext(session=session, belief=belief),
                )
            except Exception as exc:
                yield event(
                    RunEventType.TASK_FAILED,
                    "工具调用失败",
                    {
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return

            # 先保存结果供下轮模型读取，再通知外部工具已经完成。
            tool_results.append(result)
            yield event(
                RunEventType.TOOL_CALL_COMPLETED,
                f"工具 {result.name} 执行完成",
                {
                    "call_id": result.call_id,
                    "tool_name": result.name,
                    "output": result.output,
                },
            )

            # 除通用 TOOL_CALL_COMPLETED 外，再发业务事件，让 UI 不必解析工具输出。
            if result.name == "inspect_evidence_gaps":
                for action in result.output["actions"]:
                    if action["action_type"] != "run_rules":
                        yield event(
                            RunEventType.ACTION_REQUIRED,
                            action["reason"],
                            {"action": action},
                        )
            elif result.name == "evaluate_mvp_charging":
                yield event(
                    RunEventType.RULE_COMPLETED,
                    "MVP 充电规则执行完成",
                    {
                        "decision": result.output["decision"],
                        "rule_name": result.output["rule_name"],
                    },
                )

        # 循环自然耗尽说明模型始终没有返回最终答案，按治理规则判定失败。
        yield event(
            RunEventType.TASK_FAILED,
            "超过最大模型决策步数，执行被治理规则终止",
            {"max_model_steps": self._max_model_steps},
        )

    def invoke(
        self,
        *,
        session: TaskSession,
        belief: BeliefState,
        user_input: str,
    ) -> AgentRunResult:
        """消费完整 stream，将实时事件聚合成一次性 AgentRunResult。"""

        # invoke 不复制 Agent 逻辑，而是复用 stream，保证两种 API 行为一致。
        events = tuple(
            self.stream(session=session, belief=belief, user_input=user_input)
        )
        # 正常只有一个 FINAL_ANSWER；取最后一个让聚合逻辑对异常重复事件也稳健。
        final_events = [
            item for item in events if item.event_type == RunEventType.FINAL_ANSWER
        ]
        failed = any(item.event_type == RunEventType.TASK_FAILED for item in events)
        final_answer = (
            None if not final_events else str(final_events[-1].data["answer"])
        )
        return AgentRunResult(final_answer=final_answer, events=events, failed=failed)


def inspect_evidence_gaps_tool(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """工具适配层：调用第 1 章规划函数，并把结果转换成普通字典/列表。"""

    # target_id 已由 ToolRegistry 校验，业务函数直接读取受信任的 context.belief。
    del arguments
    return {
        "missing_fields": missing_evidence_fields(context.belief),
        "actions": to_primitive(plan_next_actions(context.belief)),
    }


def evaluate_mvp_charging_tool(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """工具适配层：执行第 1 章确定性规则，同时返回结构化结果和文本回答。"""

    del arguments
    result = evaluate_compatibility(context.belief)
    return {
        "rule_name": result.rule_name,
        "decision": result.decision,
        "result": to_primitive(result),
        "answer": build_evidence_answer(result),
    }


def build_tool_registry() -> ToolRegistry:
    """注册本章允许模型调用的两个工具，形成显式白名单。"""

    return ToolRegistry(
        (
            ToolSpec(
                name="inspect_evidence_gaps",
                description="检查目标当前缺少哪些证据，并返回下一步 Action。",
                required_arguments=("target_id",),
                handler=inspect_evidence_gaps_tool,
            ),
            ToolSpec(
                name="evaluate_mvp_charging",
                description="在证据齐全后执行确定性 USB-C MVP 充电规则。",
                required_arguments=("target_id",),
                handler=evaluate_mvp_charging_tool,
            ),
        )
    )


def build_complete_demo_belief() -> BeliefState:
    """构造证据齐全的演示状态，使 Agent 能走到规则工具和最终回答。"""

    belief = build_demo_belief()
    add_label_evidence(
        belief,
        Observation(
            observation_id="obs-ch02-label",
            target_id=belief.target_id,
            view_type="back_label",
            quality_score=0.93,
        ),
        "USB Power Delivery: 5V/3A, 9V/3A, 15V/3A, 20V/3.25A",
    )
    add_demo_device_evidence(belief)
    return belief


def print_event(item: RunEvent) -> None:
    """将单条事件压成一行 JSON，模拟日志或实时消息输出。"""

    print(json.dumps(to_primitive(item), ensure_ascii=False))


def run_demo() -> None:
    """分别演示证据不足时的 stream，以及证据齐全时的 invoke。"""

    session = TaskSession(
        session_id="session-ch02",
        intent="compatibility_check",
        target_id="charger-01",
    )

    # 场景 1：初始状态缺很多证据，Agent 会发出 ACTION_REQUIRED 并停止在外部边界。
    print("=== 1. stream：证据不足时逐条产生事件 ===")
    streaming_agent = MinimalAgent(DeterministicModelAdapter(), build_tool_registry())
    for item in streaming_agent.stream(
        session=session,
        belief=build_demo_belief(),
        user_input="这个充电器能给我的笔记本充电吗？",
    ):
        print_event(item)

    # 场景 2：预先补齐证据，模型会依次调用检查工具、规则工具并给出最终回答。
    print("\n=== 2. invoke：证据齐全时一次取得最终状态 ===")
    invoking_agent = MinimalAgent(DeterministicModelAdapter(), build_tool_registry())
    result = invoking_agent.invoke(
        session=session,
        belief=build_complete_demo_belief(),
        user_input="这个充电器能给我的笔记本充电吗？",
    )
    print(
        json.dumps(
            {
                "failed": result.failed,
                "event_count": len(result.events),
                "event_types": [item.event_type.value for item in result.events],
                "final_answer": result.final_answer,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    # 被第 3 章导入时只提供类型/函数；直接运行本文件时才展示 Demo。
    run_demo()
