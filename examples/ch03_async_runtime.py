"""
第 3 章：为最小 Agent 加入异步工具、服务进度、并发、超时和取消。

这个文件做什么
------------
第 2 章的 Agent 是同步的：工具执行时，当前任务只能原地等待。本章使用 asyncio
把模型和工具接口改成异步，并模拟一个持续运行的“感知服务”。Agent 在等待观察
结果时仍能逐条转发服务进度，也能响应用户取消或工具超时，并可靠清理后台任务。

核心角色
--------
- AsyncPerceptionModelAdapter：确定性异步模型替身，决定先查证据缺口，再请求观察。
- AsyncToolRegistry：注册/校验异步工具，并 ``await`` 工具处理函数。
- SimulatedPerceptionService：用 ``asyncio.sleep`` 模拟未来 C++ 摄像头服务的耗时，
  依次上报 requested/progress/completed；它不是大模型，也不是子 Agent。
- AsyncMinimalAgent：同时等待“工具完成、进度到达、用户取消”三个信号，并用
  ``asyncio.timeout`` 限制单次工具耗时。
- astream/ainvoke：分别提供异步事件流和一次性聚合结果。

实际使用的技术栈
----------------
- Python 标准库 asyncio：协程、Task、Queue、Event、timeout、wait、TaskGroup。
- async/await 与 AsyncIterator：非阻塞等待并流式 yield RunEvent。
- dataclasses、Protocol、Callable/Awaitable：定义可替换的异步模型和工具协议。
- 第 1～2 章的 BeliefState、Action、ToolCall、ModelDecision 和 ToolResult。

单个 Agent 的调用流程
--------------------
astream(...)
    -> await model.decide() 选择 inspect_evidence_gaps
    -> await 异步缺口工具，得到 request_view Action
    -> 再次 await model.decide() 选择 request_observation
    -> 创建 tool_task 执行感知工具
    -> 服务进度经 emit_progress() 写入 progress_queue
    -> Agent 同时等待 progress_task / tool_task / cancel_task
    -> 进度到达就 yield SERVICE_* 事件
    -> 工具完成就保存 ToolResult，再让模型生成 FINAL_ANSWER
    -> 超时或取消时取消 tool_task，并输出相应终止事件

run_demo() 还会用 TaskGroup 同时运行两个独立 Agent，证明等待 I/O 时可以并发；
最后把工具超时设得比服务耗时更短，演示超时如何向下取消感知子任务。

重要边界
--------
这里没有真实 LLM、摄像头、C++、gRPC、OCR 或持久化。服务和模型都是确定性
模拟器；本章重点是异步运行边界与资源清理，而不是完整的视觉识别流程。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

# 兼容包导入和直接执行脚本两种运行方式。
try:
    from .ch01_evidence_loop import (
        build_demo_belief,
        missing_evidence_fields,
        plan_next_actions,
    )
    from .ch02_minimal_agent_stream import (
        DecisionKind,
        ModelDecision,
        ToolCall,
        ToolResult,
    )
    from .realsight_domain import (
        BeliefState,
        Observation,
        ObservationRequest,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )
except ImportError:
    from ch01_evidence_loop import (  # type: ignore[no-redef]
        build_demo_belief,
        missing_evidence_fields,
        plan_next_actions,
    )
    from ch02_minimal_agent_stream import (  # type: ignore[no-redef]
        DecisionKind,
        ModelDecision,
        ToolCall,
        ToolResult,
    )
    from realsight_domain import (  # type: ignore[no-redef]
        BeliefState,
        Observation,
        ObservationRequest,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )


@dataclass(frozen=True)
class ProgressUpdate:
    """异步服务发给 Agent 的中间进度，只允许三种服务生命周期事件。"""

    event_type: RunEventType
    message: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        """限制事件类型，防止服务伪造任务级 FINAL_ANSWER/TASK_FAILED 等事件。"""

        allowed = {
            RunEventType.SERVICE_REQUESTED,
            RunEventType.SERVICE_PROGRESS,
            RunEventType.SERVICE_COMPLETED,
        }
        if self.event_type not in allowed:
            raise ValueError("ProgressUpdate only accepts service lifecycle events")


# “进度回调”的类型：传入 ProgressUpdate，调用后返回一个可 await 的对象。
ProgressEmitter = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class AsyncToolContext:
    """异步工具的受信任上下文；services 用名称保存可调用的外部服务。"""

    session: TaskSession
    belief: BeliefState
    services: dict[str, Any]


# 所有异步工具都接收“模型参数 + 上下文 + 进度回调”，并异步返回普通字典。
AsyncToolHandler = Callable[
    [dict[str, Any], AsyncToolContext, ProgressEmitter],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class AsyncToolSpec:
    """异步工具注册信息；每个工具自带独立的超时时间。"""

    name: str
    description: str
    required_arguments: tuple[str, ...]
    timeout_seconds: float
    handler: AsyncToolHandler

    def __post_init__(self) -> None:
        """拒绝零或负超时，避免工具一开始就进入含糊的超时状态。"""

        if self.timeout_seconds <= 0:
            raise ValueError("Tool timeout_seconds must be positive")


class AsyncModelAdapter(Protocol):
    """异步模型接口；真实网络 LLM 适配器也可实现相同签名。"""

    async def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        """非阻塞地返回一个结构化工具调用或最终回答。"""


class AsyncToolRegistry:
    """异步工具白名单：准备阶段做同步校验，执行阶段 await handler。"""

    def __init__(self, tools: tuple[AsyncToolSpec, ...]) -> None:
        # 字典便于按名字查找；数量变少说明输入中有重复工具名。
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Async tool names must be unique")

    @property
    def descriptions(self) -> tuple[dict[str, Any], ...]:
        """返回可展示给模型/调用方的工具说明，包括运行时超时预算。"""

        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "required_arguments": tool.required_arguments,
                "timeout_seconds": tool.timeout_seconds,
            }
            for tool in self._tools.values()
        )

    def prepare(
        self,
        call: ToolCall,
        context: AsyncToolContext,
    ) -> AsyncToolSpec:
        """在创建后台 Task 前完成工具名、必填参数和目标隔离校验。"""

        tool = self._tools.get(call.name)
        if tool is None:
            raise ValueError(f"Async tool is not registered: {call.name}")

        # 本教学版本验证“字段存在”；生产版通常会再用 schema 检查具体类型和范围。
        missing = [name for name in tool.required_arguments if name not in call.arguments]
        if missing:
            raise ValueError(f"Async tool {call.name} is missing arguments: {missing}")

        # 不信任模型传来的 target_id，必须与受信任的 session/belief 双重一致。
        target_id = call.arguments.get("target_id")
        if target_id != context.session.target_id or target_id != context.belief.target_id:
            raise ValueError("Async tool target does not match session and belief target")
        return tool

    async def execute(
        self,
        spec: AsyncToolSpec,
        call: ToolCall,
        context: AsyncToolContext,
        emit_progress: ProgressEmitter,
    ) -> ToolResult:
        """await 已校验的 handler，并将输出包装成与第 2 章一致的 ToolResult。"""

        output = await spec.handler(call.arguments, context, emit_progress)
        return ToolResult(call_id=call.call_id, name=call.name, output=output)


class AsyncPerceptionModelAdapter:
    """本章观察流程的确定性异步策略；它是可测试的模型替身，不是 LLM。"""

    def __init__(self) -> None:
        # 工具调用编号只用于建立可读、可关联的调用轨迹。
        self._next_call_number = 1

    async def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        """根据工具历史选择缺口检查、观察请求或最终回答。"""

        del user_input
        # 第一次决策固定先检查证据，绝不直接请求感知服务。
        if not tool_results:
            return self._tool_decision(
                "inspect_evidence_gaps",
                {"target_id": session.target_id},
                "先异步检查证据缺口。",
            )

        last_result = tool_results[-1]
        if last_result.name == "inspect_evidence_gaps":
            # 这里只挑 request_view，因为本章专门演示异步视觉观察边界。
            request_actions = [
                action
                for action in last_result.output["actions"]
                if action["action_type"] == "request_view"
            ]
            # 没有视觉缺口就结束；ask_user/retrieve_knowledge 不属于本章服务范围。
            if not request_actions:
                return ModelDecision(
                    kind=DecisionKind.FINAL_ANSWER,
                    summary="没有待执行的视觉观察请求。",
                    final_answer="当前没有需要感知服务补充的视角。",
                )
            # 将第 1 个 ObservationRequest 的 payload 原样映射为异步工具参数。
            request = request_actions[0]["payload"]
            return self._tool_decision(
                "request_observation",
                {
                    "target_id": session.target_id,
                    "view_type": request["view_type"],
                    "required_features": request["required_features"],
                    "instruction": request["instruction"],
                    "reason": request["reason"],
                },
                "证据缺口需要背面标签，向感知服务发送观察请求。",
            )

        # 感知工具只返回 Observation；把它提取为 Evidence 是后续章节的工作。
        if last_result.name == "request_observation":
            observation = last_result.output["observation"]
            return ModelDecision(
                kind=DecisionKind.FINAL_ANSWER,
                summary="感知服务已返回有效 Observation。",
                final_answer=(
                    f"已获得 {observation['view_type']} Observation "
                    f"{observation['observation_id']}；下一步应提取 Evidence。"
                ),
            )

        return ModelDecision(
            kind=DecisionKind.FINAL_ANSWER,
            summary="无法解释工具结果，停止运行。",
            final_answer="异步 Agent 无法解释工具结果。",
        )

    def _tool_decision(
        self,
        name: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> ModelDecision:
        """创建带唯一 async-call 编号的工具调用决策。"""

        call = ToolCall(
            call_id=f"async-call-{self._next_call_number:03d}",
            name=name,
            arguments=arguments,
        )
        self._next_call_number += 1
        return ModelDecision(
            kind=DecisionKind.TOOL_CALL,
            summary=summary,
            tool_call=call,
        )


class SimulatedPerceptionService:
    """未来 C++ 感知运行时的请求驱动替身，用计数器帮助测试并发和清理。"""

    def __init__(self, *, observation_delay_seconds: float = 0.04) -> None:
        # 延迟为 0 也合法，可用于快速测试；负数没有业务含义。
        if observation_delay_seconds < 0:
            raise ValueError("observation_delay_seconds cannot be negative")
        self.observation_delay_seconds = observation_delay_seconds
        # 以下公开计数器不是业务结果，而是用来验证并发、完成和取消是否符合预期。
        self.active_requests = 0
        self.maximum_active_requests = 0
        self.completed_requests = 0
        self.cancelled_requests = 0
        # 测试可以 await 这个 Event，精确等到服务真正开始后再触发取消。
        self.request_started = asyncio.Event()
        self._next_observation_number = 1

    async def observe(
        self,
        request: ObservationRequest,
        emit_progress: ProgressEmitter,
    ) -> Observation:
        """模拟一次分阶段观察，并通过 emit_progress 上报生命周期。"""

        # 先分配 ID 并登记“当前有一个活跃请求”。
        observation_number = self._next_observation_number
        self._next_observation_number += 1
        self.active_requests += 1
        self.maximum_active_requests = max(
            self.maximum_active_requests,
            self.active_requests,
        )
        self.request_started.set()

        try:
            # 阶段 1：请求已经进入感知服务。
            await emit_progress(
                ProgressUpdate(
                    event_type=RunEventType.SERVICE_REQUESTED,
                    message="感知服务已接收 ObservationRequest",
                    data={
                        "target_id": request.target_id,
                        "view_type": request.view_type,
                    },
                )
            )
            # asyncio.sleep 让出事件循环；等待期间其他 Agent 可以继续运行。
            await asyncio.sleep(self.observation_delay_seconds / 2)
            # 阶段 2：模拟筛选清晰、低反光并含文字区域的视频帧。
            await emit_progress(
                ProgressUpdate(
                    event_type=RunEventType.SERVICE_PROGRESS,
                    message="感知服务正在筛选满足质量条件的帧",
                    data={
                        "target_id": request.target_id,
                        "required_features": request.required_features,
                    },
                )
            )
            await asyncio.sleep(self.observation_delay_seconds / 2)

            # 阶段 3：构造一个满足请求的 Observation 并上报完成。
            observation_id = f"obs-async-{observation_number:03d}"
            observation = Observation(
                observation_id=observation_id,
                target_id=request.target_id,
                view_type=request.view_type,
                quality_score=0.93,
                image_path=f"artifacts/{request.target_id}/{observation_id}.jpg",
                local_features=("text_region", "low_glare"),
            )
            self.completed_requests += 1
            await emit_progress(
                ProgressUpdate(
                    event_type=RunEventType.SERVICE_COMPLETED,
                    message="感知服务已生成有效 Observation",
                    data={
                        "observation_id": observation.observation_id,
                        "quality_score": observation.quality_score,
                    },
                )
            )
            return observation
        except asyncio.CancelledError:
            # 记录取消后必须重新抛出，让上层清楚 Task 没有正常完成。
            self.cancelled_requests += 1
            raise
        finally:
            # 无论成功、异常还是取消，都要减少活跃计数，避免资源状态“泄漏”。
            self.active_requests -= 1


@dataclass(frozen=True)
class AsyncAgentRunResult:
    """ainvoke() 的聚合结果；terminal_event 精确说明任务如何结束。"""

    final_answer: str | None
    events: tuple[RunEvent, ...]
    terminal_event: RunEventType | None

    @property
    def failed(self) -> bool:
        """运行错误和超时都视为失败；用户主动取消单独判断。"""

        return self.terminal_event in {
            RunEventType.TASK_FAILED,
            RunEventType.TASK_TIMED_OUT,
        }

    @property
    def cancelled(self) -> bool:
        """任务是否由取消信号结束。"""

        return self.terminal_event == RunEventType.TASK_CANCELLED


class AsyncMinimalAgent:
    """异步最小 Agent 运行时，协调模型、工具、进度、超时和取消。"""

    def __init__(
        self,
        model: AsyncModelAdapter,
        tools: AsyncToolRegistry,
        *,
        services: dict[str, Any],
        max_model_steps: int = 4,
        progress_queue_size: int = 8,
    ) -> None:
        # 两个正数限制分别防止模型无限循环，以及进度无限堆积占用内存。
        if max_model_steps < 1:
            raise ValueError("max_model_steps must be positive")
        if progress_queue_size < 1:
            raise ValueError("progress_queue_size must be positive")
        self._model = model
        self._tools = tools
        self._services = services
        self._max_model_steps = max_model_steps
        self._progress_queue_size = progress_queue_size

    async def astream(
        self,
        *,
        session: TaskSession,
        belief: BeliefState,
        user_input: str,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[RunEvent]:
        """异步运行 Agent，并实时 yield 按序编号的 RunEvent。

        cancel_event 是协作式取消信号：调用方 set() 后，Agent 会在模型决策前
        或等待工具期间发现它，然后取消正在运行的子任务并正常发出终止事件。
        """

        sequence = 0
        tool_results: list[ToolResult] = []

        def event(
            event_type: RunEventType,
            message: str,
            data: dict[str, Any] | None = None,
        ) -> RunEvent:
            """创建同一 session 下的下一条有序事件。"""

            nonlocal sequence
            sequence += 1
            return RunEvent(
                event_type=event_type,
                session_id=session.session_id,
                message=message,
                data={} if data is None else data,
                sequence=sequence,
            )

        # 所有异步任务创建前先做目标隔离，避免错误工作已经在后台运行。
        if session.target_id != belief.target_id:
            yield event(
                RunEventType.TASK_FAILED,
                "任务目标与 Belief State 目标不一致",
                {"session_target": session.target_id, "belief_target": belief.target_id},
            )
            return

        # 正常流的第一条事件，同时告诉消费者本次可用的工具和各自超时。
        yield event(
            RunEventType.TASK_STARTED,
            "异步 Agent 开始执行",
            {
                "intent": session.intent,
                "target_id": session.target_id,
                "available_tools": self._tools.descriptions,
            },
        )

        # 每轮最多产生一次模型决策和一次工具调用。
        for _ in range(self._max_model_steps):
            # 模型调用前是第一个取消检查点，不必发起新的网络/工具工作。
            if cancel_event is not None and cancel_event.is_set():
                yield event(
                    RunEventType.TASK_CANCELLED,
                    "任务在下一次模型决策前被用户取消",
                )
                return

            try:
                # await 表示模型 I/O 未完成时把执行权交还事件循环。
                decision = await self._model.decide(
                    user_input=user_input,
                    session=session,
                    tool_results=tuple(tool_results),
                )
            except Exception as exc:
                yield event(
                    RunEventType.TASK_FAILED,
                    "异步模型适配器调用失败",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                return

            # 决策本身也进入事件轨迹，便于前端展示和事后调试。
            yield event(
                RunEventType.MODEL_DECISION,
                decision.summary,
                {
                    "decision_kind": decision.kind.value,
                    "tool_call": to_primitive(decision.tool_call),
                },
            )

            # FINAL_ANSWER 是正常终点，不会再创建工具 Task。
            if decision.kind == DecisionKind.FINAL_ANSWER:
                yield event(
                    RunEventType.FINAL_ANSWER,
                    "异步 Agent 返回本轮最终输出",
                    {"answer": decision.final_answer},
                )
                return

            call = decision.tool_call
            assert call is not None
            # context 中的 session/belief/services 均由运行时注入，不接受模型伪造。
            context = AsyncToolContext(
                session=session,
                belief=belief,
                services=self._services,
            )
            # prepare 是纯同步校验；失败时不会留下任何异步后台任务。
            try:
                spec = self._tools.prepare(call, context)
            except Exception as exc:
                yield event(
                    RunEventType.TASK_FAILED,
                    "异步工具调用校验失败",
                    {
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return

            yield event(
                RunEventType.TOOL_CALL_REQUESTED,
                f"请求调用异步工具 {call.name}",
                {
                    "call_id": call.call_id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                    "timeout_seconds": spec.timeout_seconds,
                },
            )

            # 有界 Queue 在生产速度过快时对服务施加背压，避免进度消息无限占内存。
            progress_queue: asyncio.Queue[ProgressUpdate] = asyncio.Queue(
                maxsize=self._progress_queue_size
            )

            async def emit_progress(update: ProgressUpdate) -> None:
                """服务使用的回调：把进度安全送进本次调用专属的队列。"""

                await progress_queue.put(update)

            # 工具必须成为独立 Task，Agent 才能一边等结果，一边消费进度/取消信号。
            tool_task = asyncio.create_task(
                self._tools.execute(spec, call, context, emit_progress),
                name=f"tool:{call.call_id}:{call.name}",
            )
            # 如果调用方提供 Event，就创建一个等待它被 set() 的并行 Task。
            cancel_task = (
                asyncio.create_task(cancel_event.wait(), name=f"cancel:{call.call_id}")
                if cancel_event is not None
                else None
            )
            # progress_task 每次只等待一条队列消息；result 为 None 表示工具尚未完成。
            progress_task: asyncio.Task[ProgressUpdate] | None = None
            result: ToolResult | None = None

            try:
                # timeout 包住整个工具等待过程；超时会跳到下面的 TimeoutError 分支。
                async with asyncio.timeout(spec.timeout_seconds):
                    while result is None:
                        # 为“下一条进度”创建等待任务，和工具/取消任务一起竞速。
                        progress_task = asyncio.create_task(
                            progress_queue.get(),
                            name=f"progress:{call.call_id}",
                        )
                        waiters: set[asyncio.Task[Any]] = {tool_task, progress_task}
                        if cancel_task is not None:
                            waiters.add(cancel_task)
                        # FIRST_COMPLETED：三类信号谁先到就先处理，不阻塞其他信号。
                        completed, _ = await asyncio.wait(
                            waiters,
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if progress_task in completed:
                            # 服务进度会被提升为统一 RunEvent，并补充所属工具信息。
                            update = progress_task.result()
                            progress_task = None
                            yield event(
                                update.event_type,
                                update.message,
                                {
                                    **update.data,
                                    "call_id": call.call_id,
                                    "tool_name": call.name,
                                },
                            )

                        if cancel_task is not None and cancel_task in completed:
                            # 用户取消优先终止工具；等待其清理结束后再发布 TASK_CANCELLED。
                            await _cancel_and_wait(tool_task)
                            yield event(
                                RunEventType.TASK_CANCELLED,
                                "用户取消了正在等待的异步工具",
                                {
                                    "call_id": call.call_id,
                                    "tool_name": call.name,
                                },
                            )
                            return

                        if tool_task in completed:
                            # 工具已结束，不再需要悬挂的 queue.get() 等待任务。
                            if progress_task is not None:
                                await _cancel_and_wait(progress_task)
                                progress_task = None
                            # 工具完成前刚写入队列的尾部进度也必须排空，避免丢 completed 消息。
                            while not progress_queue.empty():
                                update = progress_queue.get_nowait()
                                yield event(
                                    update.event_type,
                                    update.message,
                                    {
                                        **update.data,
                                        "call_id": call.call_id,
                                        "tool_name": call.name,
                                    },
                                )
                            # await 已完成 Task 会立即取得结果；若工具抛错则进入通用异常分支。
                            result = await tool_task
            except TimeoutError:
                # asyncio.timeout 只取消当前等待范围；这里显式取消真正工作的 tool_task。
                await _cancel_and_wait(tool_task)
                yield event(
                    RunEventType.TASK_TIMED_OUT,
                    "异步工具超过允许时间",
                    {
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "timeout_seconds": spec.timeout_seconds,
                    },
                )
                return
            except asyncio.CancelledError:
                # 如果整个 astream Task 被外部 task.cancel()，先清理子任务再继续传播取消。
                await _cancel_and_wait(tool_task)
                raise
            except Exception as exc:
                # 任何工具异常都转为可观察的 TASK_FAILED，同时保证后台任务已收尾。
                await _cancel_and_wait(tool_task)
                yield event(
                    RunEventType.TASK_FAILED,
                    "异步工具执行失败",
                    {
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return
            finally:
                # finally 是最后保险：所有辅助 Task 都必须被取消并等待，避免“Task was destroyed”。
                if progress_task is not None:
                    await _cancel_and_wait(progress_task)
                if cancel_task is not None:
                    await _cancel_and_wait(cancel_task)

            # 能走到这里表示工具正常完成；结果进入历史，供下一轮模型决策。
            assert result is not None
            tool_results.append(result)
            yield event(
                RunEventType.TOOL_CALL_COMPLETED,
                f"异步工具 {result.name} 执行完成",
                {
                    "call_id": result.call_id,
                    "tool_name": result.name,
                    "output": result.output,
                },
            )

        # 模型在规定轮数内始终未回答，按治理边界结束而不是无限运行。
        yield event(
            RunEventType.TASK_FAILED,
            "超过最大异步模型决策步数",
            {"max_model_steps": self._max_model_steps},
        )

    async def ainvoke(
        self,
        *,
        session: TaskSession,
        belief: BeliefState,
        user_input: str,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncAgentRunResult:
        """完整消费 astream，并聚合最终回答、事件轨迹和终止原因。"""

        # 异步列表推导会一直迭代到 astream 正常结束。
        events = tuple(
            [
                item
                async for item in self.astream(
                    session=session,
                    belief=belief,
                    user_input=user_input,
                    cancel_event=cancel_event,
                )
            ]
        )
        # 终止事件不只有 FINAL_ANSWER，还包括失败、超时和用户取消。
        terminal_types = {
            RunEventType.FINAL_ANSWER,
            RunEventType.TASK_FAILED,
            RunEventType.TASK_TIMED_OUT,
            RunEventType.TASK_CANCELLED,
        }
        terminal_events = [item for item in events if item.event_type in terminal_types]
        terminal_event = terminal_events[-1].event_type if terminal_events else None
        final_events = [
            item for item in events if item.event_type == RunEventType.FINAL_ANSWER
        ]
        final_answer = (
            None if not final_events else str(final_events[-1].data["answer"])
        )
        return AsyncAgentRunResult(
            final_answer=final_answer,
            events=events,
            terminal_event=terminal_event,
        )


async def _cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """取消一个 Task 并等待其 finally 清理完成；本函数本身不覆盖主错误。"""

    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # 清理阶段的次要异常不能掩盖触发清理的主超时/取消/工具错误。
        pass


async def inspect_evidence_gaps_async_tool(
    arguments: dict[str, Any],
    context: AsyncToolContext,
    emit_progress: ProgressEmitter,
) -> dict[str, Any]:
    """第 1 章同步规划函数的异步工具包装器。"""

    del arguments, emit_progress
    # sleep(0) 主动让出一次事件循环，强调这是遵守 async 协议的轻量工具。
    await asyncio.sleep(0)
    return {
        "missing_fields": missing_evidence_fields(context.belief),
        "actions": to_primitive(plan_next_actions(context.belief)),
    }


async def request_observation_async_tool(
    arguments: dict[str, Any],
    context: AsyncToolContext,
    emit_progress: ProgressEmitter,
) -> dict[str, Any]:
    """将模型参数还原为 ObservationRequest，再委托给感知服务执行。"""

    # 服务来自受信任 context；类型不对时尽早失败，而不是调用未知对象。
    service = context.services.get("perception")
    if not isinstance(service, SimulatedPerceptionService):
        raise ValueError("Perception service is unavailable")

    # ToolCall 中只有普通 JSON 风格数据，这里恢复为强类型领域对象。
    request = ObservationRequest(
        target_id=str(arguments["target_id"]),
        view_type=str(arguments["view_type"]),
        required_features=tuple(arguments["required_features"]),
        instruction=str(arguments["instruction"]),
        reason=str(arguments["reason"]),
    )
    observation = await service.observe(request, emit_progress)
    # 服务返回后再次验证 target_id，形成请求前后两道目标隔离边界。
    if observation.target_id != context.belief.target_id:
        raise ValueError("Perception service returned an Observation for another target")
    return {"observation": to_primitive(observation)}


def build_async_tool_registry(
    *,
    observation_timeout_seconds: float = 0.25,
) -> AsyncToolRegistry:
    """注册证据检查与观察请求两个异步工具，并配置各自超时。"""

    return AsyncToolRegistry(
        (
            AsyncToolSpec(
                name="inspect_evidence_gaps",
                description="异步检查当前证据缺口并返回 Action。",
                required_arguments=("target_id",),
                timeout_seconds=0.1,
                handler=inspect_evidence_gaps_async_tool,
            ),
            AsyncToolSpec(
                name="request_observation",
                description="向持续运行的感知服务请求一个满足条件的 Observation。",
                required_arguments=(
                    "target_id",
                    "view_type",
                    "required_features",
                    "instruction",
                    "reason",
                ),
                timeout_seconds=observation_timeout_seconds,
                handler=request_observation_async_tool,
            ),
        )
    )


def build_async_agent(
    service: SimulatedPerceptionService,
    *,
    observation_timeout_seconds: float = 0.25,
    progress_queue_size: int = 8,
) -> AsyncMinimalAgent:
    """把模型替身、工具注册表和感知服务组装成可运行的异步 Agent。"""

    return AsyncMinimalAgent(
        AsyncPerceptionModelAdapter(),
        build_async_tool_registry(
            observation_timeout_seconds=observation_timeout_seconds
        ),
        services={"perception": service},
        progress_queue_size=progress_queue_size,
    )


async def collect_event_types(
    agent: AsyncMinimalAgent,
    session: TaskSession,
) -> list[str]:
    """运行一次 Agent 并只提取事件类型，供并发 Demo 汇总结果。"""

    result = await agent.ainvoke(
        session=session,
        belief=build_demo_belief(),
        user_input="请补充背面标签观察。",
    )
    return [item.event_type.value for item in result.events]


async def run_demo() -> None:
    """依次展示正常事件流、双任务并发和超时取消三个场景。"""

    session = TaskSession(
        session_id="session-ch03",
        intent="compatibility_check",
        target_id="charger-01",
    )

    # 场景 1：async for 在事件一产生时就打印，无需等待整个 Agent 完成。
    print("=== 1. 正常异步事件流 ===")
    service = SimulatedPerceptionService(observation_delay_seconds=0.02)
    agent = build_async_agent(service)
    async for item in agent.astream(
        session=session,
        belief=build_demo_belief(),
        user_input="这个充电器还缺什么观察？",
    ):
        print(json.dumps(to_primitive(item), ensure_ascii=False))

    # 场景 2：两个 Agent 共享同一模拟服务，观察 maximum_active_requests 是否达到 2。
    print("\n=== 2. 两个独立任务并发推进 ===")
    concurrent_service = SimulatedPerceptionService(observation_delay_seconds=0.03)
    first_agent = build_async_agent(concurrent_service)
    second_agent = build_async_agent(concurrent_service)
    results: dict[str, list[str]] = {}

    async def run_one(name: str, current_agent: AsyncMinimalAgent) -> None:
        """一个并发分支；每个分支使用独立 session 和 Agent 状态。"""

        current_session = TaskSession(
            session_id=f"session-ch03-{name}",
            intent="compatibility_check",
            target_id="charger-01",
        )
        results[name] = await collect_event_types(current_agent, current_session)

    # TaskGroup 会并发运行子任务，并保证离开上下文前所有子任务都已结束/清理。
    async with asyncio.TaskGroup() as group:
        group.create_task(run_one("A", first_agent))
        group.create_task(run_one("B", second_agent))

    print(
        json.dumps(
            {
                "maximum_active_requests": concurrent_service.maximum_active_requests,
                "completed_requests": concurrent_service.completed_requests,
                "terminal_events": {
                    name: event_types[-1] for name, event_types in results.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # 场景 3：服务要 0.1 秒，工具预算仅 0.01 秒，所以必然触发 TASK_TIMED_OUT。
    print("\n=== 3. 超时会取消感知子任务 ===")
    slow_service = SimulatedPerceptionService(observation_delay_seconds=0.1)
    timeout_agent = build_async_agent(
        slow_service,
        observation_timeout_seconds=0.01,
    )
    timeout_result = await timeout_agent.ainvoke(
        session=TaskSession(
            session_id="session-ch03-timeout",
            intent="compatibility_check",
            target_id="charger-01",
        ),
        belief=build_demo_belief(),
        user_input="请求背面标签观察。",
    )
    print(
        json.dumps(
            {
                "terminal_event": (
                    None
                    if timeout_result.terminal_event is None
                    else timeout_result.terminal_event.value
                ),
                "cancelled_service_requests": slow_service.cancelled_requests,
                "active_service_requests": slow_service.active_requests,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    # asyncio.run 创建事件循环、执行顶层协程，并在结束时关闭事件循环。
    asyncio.run(run_demo())
