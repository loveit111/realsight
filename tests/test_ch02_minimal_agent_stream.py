import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from examples.ch01_evidence_loop import build_demo_belief  # noqa: E402
from examples.ch02_minimal_agent_stream import (  # noqa: E402
    DecisionKind,
    DeterministicModelAdapter,
    MinimalAgent,
    ModelDecision,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_complete_demo_belief,
    build_tool_registry,
)
from examples.realsight_domain import RunEventType, TaskSession  # noqa: E402


class FixedToolModel:
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments

    def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        del user_input, session, tool_results
        return ModelDecision(
            kind=DecisionKind.TOOL_CALL,
            summary="测试工具调用",
            tool_call=ToolCall("call-test", self.tool_name, self.arguments),
        )


class ExplodingModel:
    def decide(self, **kwargs: object) -> ModelDecision:
        del kwargs
        raise RuntimeError("model unavailable")


class Chapter02MinimalAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = TaskSession(
            "session-ch02-test",
            "compatibility_check",
            "charger-01",
        )

    def build_agent(self) -> MinimalAgent:
        return MinimalAgent(DeterministicModelAdapter(), build_tool_registry())

    def test_incomplete_evidence_streams_actions_without_running_rule(self) -> None:
        events = tuple(
            self.build_agent().stream(
                session=self.session,
                belief=build_demo_belief(),
                user_input="能充电吗？",
            )
        )
        event_types = [item.event_type for item in events]

        self.assertEqual(event_types.count(RunEventType.ACTION_REQUIRED), 2)
        self.assertNotIn(RunEventType.RULE_COMPLETED, event_types)
        self.assertEqual(events[-1].event_type, RunEventType.FINAL_ANSWER)
        self.assertIn("request_view", events[-1].data["answer"])
        self.assertIn("ask_user", events[-1].data["answer"])

    def test_complete_evidence_calls_rule_then_returns_final_answer(self) -> None:
        result = self.build_agent().invoke(
            session=self.session,
            belief=build_complete_demo_belief(),
            user_input="能充电吗？",
        )
        event_types = [item.event_type for item in result.events]

        self.assertFalse(result.failed)
        self.assertIn(RunEventType.RULE_COMPLETED, event_types)
        self.assertEqual(result.events[-1].event_type, RunEventType.FINAL_ANSWER)
        self.assertIn("满足充电要求", result.final_answer or "")
        self.assertIn("obs-ch02-label", result.final_answer or "")

    def test_tool_request_and_result_share_call_id(self) -> None:
        events = tuple(
            self.build_agent().stream(
                session=self.session,
                belief=build_complete_demo_belief(),
                user_input="能充电吗？",
            )
        )
        requested = {
            item.data["call_id"]
            for item in events
            if item.event_type == RunEventType.TOOL_CALL_REQUESTED
        }
        completed = {
            item.data["call_id"]
            for item in events
            if item.event_type == RunEventType.TOOL_CALL_COMPLETED
        }

        self.assertEqual(requested, completed)
        self.assertEqual(requested, {"call-001", "call-002"})

    def test_event_sequence_is_strictly_increasing(self) -> None:
        events = tuple(
            self.build_agent().stream(
                session=self.session,
                belief=build_complete_demo_belief(),
                user_input="能充电吗？",
            )
        )

        self.assertEqual(
            [item.sequence for item in events],
            list(range(1, len(events) + 1)),
        )

    def test_invoke_collects_the_same_event_protocol_as_stream(self) -> None:
        streamed = tuple(
            self.build_agent().stream(
                session=self.session,
                belief=build_demo_belief(),
                user_input="能充电吗？",
            )
        )
        invoked = self.build_agent().invoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertEqual(
            [item.event_type for item in streamed],
            [item.event_type for item in invoked.events],
        )
        self.assertEqual(streamed[-1].data["answer"], invoked.final_answer)

    def test_session_and_belief_target_mismatch_fails_before_model_call(self) -> None:
        other_session = TaskSession(
            "session-other",
            "compatibility_check",
            "charger-02",
        )

        events = tuple(
            self.build_agent().stream(
                session=other_session,
                belief=build_demo_belief(),
                user_input="能充电吗？",
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, RunEventType.TASK_FAILED)

    def test_unregistered_tool_fails_closed(self) -> None:
        agent = MinimalAgent(
            FixedToolModel("delete_everything", {"target_id": "charger-01"}),
            build_tool_registry(),
        )

        result = agent.invoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertTrue(result.failed)
        self.assertIsNone(result.final_answer)
        self.assertIn("not registered", result.events[-1].data["error"])

    def test_tool_target_mismatch_fails_closed(self) -> None:
        agent = MinimalAgent(
            FixedToolModel("inspect_evidence_gaps", {"target_id": "charger-02"}),
            build_tool_registry(),
        )

        result = agent.invoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertTrue(result.failed)
        self.assertIn("target", result.events[-1].data["error"])

    def test_max_model_steps_stops_a_non_converging_model(self) -> None:
        agent = MinimalAgent(
            FixedToolModel("inspect_evidence_gaps", {"target_id": "charger-01"}),
            build_tool_registry(),
            max_model_steps=1,
        )

        result = agent.invoke(
            session=self.session,
            belief=build_complete_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertTrue(result.failed)
        self.assertEqual(result.events[-1].event_type, RunEventType.TASK_FAILED)
        self.assertIn("max_model_steps", result.events[-1].data)

    def test_model_decision_shape_rejects_missing_payload(self) -> None:
        with self.assertRaises(ValueError):
            ModelDecision(kind=DecisionKind.TOOL_CALL, summary="缺少 call")
        with self.assertRaises(ValueError):
            ModelDecision(kind=DecisionKind.FINAL_ANSWER, summary="缺少 answer")
        with self.assertRaises(ValueError):
            ModelDecision(
                kind=DecisionKind.TOOL_CALL,
                summary="不能同时回答",
                tool_call=ToolCall("call-mixed", "inspect_evidence_gaps", {}),
                final_answer="提前回答",
            )
        with self.assertRaises(ValueError):
            ModelDecision(
                kind=DecisionKind.FINAL_ANSWER,
                summary="不能同时调工具",
                tool_call=ToolCall("call-mixed", "inspect_evidence_gaps", {}),
                final_answer="回答",
            )

    def test_model_adapter_exception_becomes_failed_event(self) -> None:
        result = MinimalAgent(ExplodingModel(), build_tool_registry()).invoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertTrue(result.failed)
        self.assertEqual(result.events[-1].event_type, RunEventType.TASK_FAILED)
        self.assertEqual(result.events[-1].data["error_type"], "RuntimeError")

    def test_tool_exception_becomes_failed_event(self) -> None:
        def explode(arguments: dict[str, object], context: object) -> dict[str, object]:
            del arguments, context
            raise RuntimeError("tool unavailable")

        registry = ToolRegistry(
            (
                ToolSpec(
                    name="explode",
                    description="测试异常工具",
                    required_arguments=("target_id",),
                    handler=explode,
                ),
            )
        )
        agent = MinimalAgent(
            FixedToolModel("explode", {"target_id": "charger-01"}),
            registry,
        )

        result = agent.invoke(
            session=self.session,
            belief=build_demo_belief(),
            user_input="能充电吗？",
        )

        self.assertTrue(result.failed)
        self.assertEqual(result.events[-1].data["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
