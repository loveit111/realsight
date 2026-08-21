import asyncio
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from examples.ch01_evidence_loop import build_demo_belief  # noqa: E402
from examples.ch02_minimal_agent_stream import (  # noqa: E402
    DecisionKind,
    ModelDecision,
    ToolCall,
    ToolResult,
)
from examples.ch03_async_runtime import (  # noqa: E402
    AsyncMinimalAgent,
    AsyncToolContext,
    AsyncToolRegistry,
    AsyncToolSpec,
    ProgressEmitter,
    SimulatedPerceptionService,
    build_async_agent,
    build_async_tool_registry,
)
from examples.realsight_domain import (  # noqa: E402
    RunEventType,
    TaskSession,
)


class AsyncFixedToolModel:
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments

    async def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        del user_input, session, tool_results
        return ModelDecision(
            kind=DecisionKind.TOOL_CALL,
            summary="异步测试工具调用",
            tool_call=ToolCall("async-call-test", self.tool_name, self.arguments),
        )


class AsyncExplodingModel:
    async def decide(self, **kwargs: object) -> ModelDecision:
        del kwargs
        raise RuntimeError("async model unavailable")


class Chapter03AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = TaskSession(
            "session-ch03-test",
            "compatibility_check",
            "charger-01",
        )

    async def test_normal_run_streams_service_lifecycle_and_final_answer(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0.001)
        result = await build_async_agent(service).ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求背面标签观察。",
        )
        event_types = [item.event_type for item in result.events]

        self.assertEqual(result.terminal_event, RunEventType.FINAL_ANSWER)
        self.assertIn(RunEventType.SERVICE_REQUESTED, event_types)
        self.assertIn(RunEventType.SERVICE_PROGRESS, event_types)
        self.assertIn(RunEventType.SERVICE_COMPLETED, event_types)
        self.assertIn("obs-async-001", result.final_answer or "")
        service_events = [
            item
            for item in result.events
            if item.event_type
            in {
                RunEventType.SERVICE_REQUESTED,
                RunEventType.SERVICE_PROGRESS,
                RunEventType.SERVICE_COMPLETED,
            }
        ]
        self.assertEqual(
            {item.data["call_id"] for item in service_events},
            {"async-call-002"},
        )
        observation_result = next(
            item
            for item in result.events
            if item.event_type == RunEventType.TOOL_CALL_COMPLETED
            and item.data["tool_name"] == "request_observation"
        )
        self.assertTrue(
            observation_result.data["output"]["observation"]["image_path"].endswith(
                "obs-async-001.jpg"
            )
        )
        self.assertEqual(service.completed_requests, 1)
        self.assertEqual(service.active_requests, 0)

    async def test_event_sequence_is_strictly_increasing(self) -> None:
        result = await build_async_agent(
            SimulatedPerceptionService(observation_delay_seconds=0)
        ).ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(
            [item.sequence for item in result.events],
            list(range(1, len(result.events) + 1)),
        )

    async def test_timeout_cancels_service_task_and_emits_timeout(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0.2)
        agent = build_async_agent(service, observation_timeout_seconds=0.005)

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_TIMED_OUT)
        self.assertTrue(result.failed)
        self.assertEqual(service.cancelled_requests, 1)
        self.assertEqual(service.active_requests, 0)
        self.assertNotIn(
            RunEventType.FINAL_ANSWER,
            [item.event_type for item in result.events],
        )

    async def test_cooperative_user_cancel_emits_cancelled_and_cleans_service(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0.2)
        cancel_event = asyncio.Event()
        agent = build_async_agent(service)

        async def cancel_after_start() -> None:
            await service.request_started.wait()
            cancel_event.set()

        cancel_task = asyncio.create_task(cancel_after_start())
        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
            cancel_event=cancel_event,
        )
        await cancel_task

        self.assertTrue(result.cancelled)
        self.assertFalse(result.failed)
        self.assertEqual(result.terminal_event, RunEventType.TASK_CANCELLED)
        self.assertEqual(service.cancelled_requests, 1)
        self.assertEqual(service.active_requests, 0)

    async def test_external_task_cancellation_propagates_after_cleanup(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0.2)
        agent = build_async_agent(service)

        async def consume() -> None:
            async for _ in agent.astream(
                session=self.session,
                belief=build_demo_belief(),
                user_input="请求观察。",
            ):
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(service.request_started.wait(), timeout=0.1)
        consumer.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertEqual(service.cancelled_requests, 1)
        self.assertEqual(service.active_requests, 0)

    async def test_task_group_runs_two_independent_requests_concurrently(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0.02)
        results = []

        async def run_one(index: int) -> None:
            agent = build_async_agent(service)
            result = await agent.ainvoke(
                session=TaskSession(
                    f"session-concurrent-{index}",
                    "compatibility_check",
                    "charger-01",
                ),
                belief=build_demo_belief(),
                user_input="请求观察。",
            )
            results.append(result)

        async with asyncio.TaskGroup() as group:
            group.create_task(run_one(1))
            group.create_task(run_one(2))

        self.assertEqual(len(results), 2)
        self.assertEqual(service.maximum_active_requests, 2)
        self.assertEqual(service.completed_requests, 2)
        self.assertTrue(
            all(item.terminal_event == RunEventType.FINAL_ANSWER for item in results)
        )
        self.assertEqual(
            {item.final_answer for item in results},
            {
                "已获得 back_label Observation obs-async-001；下一步应提取 Evidence。",
                "已获得 back_label Observation obs-async-002；下一步应提取 Evidence。",
            },
        )

    async def test_bounded_progress_queue_does_not_lose_completion(self) -> None:
        service = SimulatedPerceptionService(observation_delay_seconds=0)
        agent = build_async_agent(service, progress_queue_size=1)

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.FINAL_ANSWER)
        self.assertIn(
            RunEventType.SERVICE_COMPLETED,
            [item.event_type for item in result.events],
        )

    async def test_session_and_belief_target_mismatch_fails_before_model(self) -> None:
        result = await build_async_agent(
            SimulatedPerceptionService(observation_delay_seconds=0)
        ).ainvoke(
            session=TaskSession(
                "session-other-target",
                "compatibility_check",
                "charger-02",
            ),
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertEqual(len(result.events), 1)

    async def test_unregistered_async_tool_fails_closed(self) -> None:
        agent = AsyncMinimalAgent(
            AsyncFixedToolModel("unknown", {"target_id": "charger-01"}),
            build_async_tool_registry(),
            services={"perception": SimulatedPerceptionService()},
        )

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertIn("not registered", result.events[-1].data["error"])

    async def test_async_tool_target_mismatch_fails_closed(self) -> None:
        agent = AsyncMinimalAgent(
            AsyncFixedToolModel(
                "inspect_evidence_gaps",
                {"target_id": "charger-02"},
            ),
            build_async_tool_registry(),
            services={"perception": SimulatedPerceptionService()},
        )

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertIn("target", result.events[-1].data["error"])

    async def test_async_model_exception_becomes_failed_event(self) -> None:
        agent = AsyncMinimalAgent(
            AsyncExplodingModel(),
            build_async_tool_registry(),
            services={"perception": SimulatedPerceptionService()},
        )

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertEqual(result.events[-1].data["error_type"], "RuntimeError")

    async def test_async_tool_exception_becomes_failed_event(self) -> None:
        async def explode(
            arguments: dict[str, object],
            context: AsyncToolContext,
            emit_progress: ProgressEmitter,
        ) -> dict[str, object]:
            del arguments, context, emit_progress
            raise RuntimeError("async tool unavailable")

        registry = AsyncToolRegistry(
            (
                AsyncToolSpec(
                    name="explode",
                    description="测试异步异常",
                    required_arguments=("target_id",),
                    timeout_seconds=0.1,
                    handler=explode,
                ),
            )
        )
        agent = AsyncMinimalAgent(
            AsyncFixedToolModel("explode", {"target_id": "charger-01"}),
            registry,
            services={},
        )

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertEqual(result.events[-1].data["error_type"], "RuntimeError")

    async def test_max_model_steps_stops_non_converging_async_model(self) -> None:
        agent = AsyncMinimalAgent(
            AsyncFixedToolModel(
                "inspect_evidence_gaps",
                {"target_id": "charger-01"},
            ),
            build_async_tool_registry(),
            services={"perception": SimulatedPerceptionService()},
            max_model_steps=1,
        )

        result = await agent.ainvoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="请求观察。",
        )

        self.assertEqual(result.terminal_event, RunEventType.TASK_FAILED)
        self.assertIn("max_model_steps", result.events[-1].data)


if __name__ == "__main__":
    unittest.main()
