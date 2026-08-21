"""
第 5 章自动化测试：验证主动观察中断、checkpoint 隔离和恢复输入安全性。

这个文件的整体逻辑
------------------
每个测试都使用真实 LangGraph + InMemorySaver，不用伪造“暂停标志”。测试先运行图直到
interrupt，再读取 checkpoint，随后用 Command(resume=...) 的封装入口继续。除正常的
标签、接口、笔记本型号三段流程外，还会攻击错 thread、错 request、错目标、错视角、
错 Evidence 来源和错用户字段。错误恢复必须失败，但原 checkpoint 仍能接受正确输入。

使用的技术栈
------------
- Python 标准库 unittest：组织独立、确定性的测试。
- LangGraph InMemorySaver/interrupt/Command：真实暂停、恢复和 thread checkpoint。
- Pydantic ValidationError：验证恢复 payload 的结构和范围。
- 第 4～5 章领域契约：Observation、Evidence、BeliefState、Action、RunEvent。

测试调用流程
------------
python -m unittest discover -s tests -p "test_ch05*.py" -v
    -> setUp() 为每项测试创建独立 graph/saver/thread
    -> 首次 invoke 在 back_label 等待节点暂停
    -> 正常测试依次恢复 label、port、user
    -> 失败测试提交错误 payload，检查异常与 checkpoint 未被污染
    -> 线程隔离测试在同一个 saver 中运行两个 session
    -> Belief 合并测试检查同值支持与异值冲突

这些测试不需要 LLM、摄像头、网络或 SQLite。InMemorySaver 只能证明同一进程内的
checkpoint 语义；跨进程恢复属于第 6 章。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples"
for import_path in (PROJECT_ROOT, EXAMPLES_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from ch05_active_observation_interrupts import (  # noqa: E402
    ActiveObservationState,
    ObservationPausePayload,
    ObservationResume,
    UserPausePayload,
    UserResume,
    build_active_observation_graph,
    build_initial_state,
    extract_pause,
    graph_config,
    load_checkpoint_state,
    make_evidence,
    make_observation_resume,
    merge_resumed_evidence,
    resume_active_observation,
)
from contracts.models import (  # noqa: E402
    ActionType,
    Evidence,
    EvidenceStatus,
    GraphRoute,
    Observation,
    ObservationStatus,
    QualitySignals,
    RunEventType,
    SessionStatus,
    ViewType,
    utc_now,
)


class Chapter05GraphTestCase(unittest.TestCase):
    """为每项测试创建一个已经暂停在背面标签请求处的新 thread。"""

    def setUp(self) -> None:
        self.graph, self.saver = build_active_observation_graph()
        self.initial = build_initial_state()
        self.config = graph_config(self.initial.session.thread_id)
        self.first_result = self.graph.invoke(self.initial, config=self.config)
        self.first_interrupt, self.first_pause = extract_pause(self.first_result)
        self.current_interrupt_id = self.first_interrupt.id
        self.first_state = load_checkpoint_state(self.graph, self.config)

    def resume(self, payload: dict[str, object]) -> dict[str, object]:
        """使用测试当前记录的 interrupt ID 走安全恢复入口。"""

        return resume_active_observation(
            self.graph,
            self.config,
            payload,
            interrupt_id=self.current_interrupt_id,
        )

    def resume_label(self, *, evidence: bool = True) -> dict[str, object]:
        """提交背面标签恢复值，并返回下一次 invoke 结果。"""

        request = self.first_state.pending_request
        if request is None:
            raise AssertionError("test setup must have a pending label request")
        payload = make_observation_resume(
            request,
            observation_id="test-label-observation",
            field="charger_label",
            value="USB-C PD 65W",
        )
        if not evidence:
            payload["evidence"] = []
        return self.resume(payload)

    def reach_port_pause(self) -> ActiveObservationState:
        """恢复标签并确认图暂停在接口特写。"""

        result = self.resume_label()
        interrupt_info, pause = extract_pause(result)
        self.current_interrupt_id = interrupt_info.id
        self.assertIsInstance(pause, ObservationPausePayload)
        state = load_checkpoint_state(self.graph, self.config)
        self.assertEqual(state.pending_request.view_type, ViewType.PORT_CLOSEUP)
        return state

    def reach_user_pause(self) -> ActiveObservationState:
        """依次恢复标签和接口，返回等待 laptop_model 的状态。"""

        port_state = self.reach_port_pause()
        payload = make_observation_resume(
            port_state.pending_request,
            observation_id="test-port-observation",
            field="charger_port_type",
            value="usb_c",
        )
        result = self.resume(payload)
        interrupt_info, pause = extract_pause(result)
        self.current_interrupt_id = interrupt_info.id
        self.assertIsInstance(pause, UserPausePayload)
        return load_checkpoint_state(self.graph, self.config)


class InterruptLifecycleTests(Chapter05GraphTestCase):
    """验证三个暂停点、同一 thread 恢复和最终出口。"""

    def test_first_invoke_pauses_after_request_is_checkpointed(self) -> None:
        self.assertIsInstance(self.first_pause, ObservationPausePayload)
        self.assertEqual(self.first_pause.thread_id, self.initial.session.thread_id)
        self.assertEqual(self.first_state.session.status, SessionStatus.WAITING_OBSERVATION)
        self.assertEqual(self.first_state.route, GraphRoute.REQUEST_OBSERVATION)
        self.assertEqual(self.first_state.pending_request.view_type, ViewType.BACK_LABEL)
        self.assertEqual(
            self.first_state.pending_request.required_features,
            ("charger_label",),
        )
        self.assertEqual(self.graph.get_state(self.config).next, ("wait_for_observation",))

    def test_interrupt_payload_is_plain_json_serializable_data(self) -> None:
        serialized = json.dumps(self.first_interrupt.value, ensure_ascii=False)

        self.assertIn("observation_required", serialized)
        self.assertNotIn("BaseModel", serialized)

    def test_label_resume_advances_to_port_closeup(self) -> None:
        state = self.reach_port_pause()

        self.assertIn("charger_label", state.belief.confirmed)
        self.assertIn(ViewType.BACK_LABEL, state.belief.observed_views)
        self.assertEqual(state.completed_interrupts, 1)
        self.assertEqual(state.pending_request.view_type, ViewType.PORT_CLOSEUP)

    def test_port_resume_advances_to_laptop_question(self) -> None:
        state = self.reach_user_pause()

        self.assertEqual(state.session.status, SessionStatus.WAITING_USER)
        self.assertEqual(state.route, GraphRoute.AWAIT_USER)
        self.assertEqual(state.expected_user_field, "laptop_model")
        self.assertEqual(state.pending_actions[0].action_type, ActionType.ASK_USER)
        self.assertIn("charger_port_type", state.belief.confirmed)
        self.assertIn(ViewType.PORT_CLOSEUP, state.belief.observed_views)
        self.assertEqual(state.completed_interrupts, 2)

    def test_user_resume_reaches_rules_and_completes_thread(self) -> None:
        state = self.reach_user_pause()
        payload = UserResume(
            target_id=state.target.target_id,
            field="laptop_model",
            value="ExampleBook Pro 14",
            source_id="test-user-message",
        ).model_dump(mode="json")

        result = self.resume(payload)
        final = ActiveObservationState.model_validate(result)
        snapshot = self.graph.get_state(self.config)

        self.assertEqual(final.route, GraphRoute.RUN_RULES)
        self.assertEqual(final.pending_actions[0].action_type, ActionType.RUN_RULES)
        self.assertEqual(final.completed_interrupts, 3)
        self.assertIn("laptop_model", final.belief.confirmed)
        self.assertEqual(snapshot.next, ())

    def test_event_sequence_remains_strict_across_resumes(self) -> None:
        state = self.reach_user_pause()
        payload = UserResume(
            target_id=state.target.target_id,
            field="laptop_model",
            value="ExampleBook Pro 14",
            source_id="test-user-message",
        ).model_dump(mode="json")
        final = ActiveObservationState.model_validate(
            self.resume(payload)
        )

        sequences = [event.sequence for event in final.events]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        self.assertEqual(final.events[0].event_type, RunEventType.TASK_STARTED)

    def test_completed_thread_cannot_be_resumed_again(self) -> None:
        state = self.reach_user_pause()
        payload = UserResume(
            target_id=state.target.target_id,
            field="laptop_model",
            value="ExampleBook Pro 14",
            source_id="test-user-message",
        ).model_dump(mode="json")
        self.resume(payload)

        with self.assertRaisesRegex(ValueError, "no pending interrupt"):
            self.resume(payload)


class ResumeBoundaryTests(Chapter05GraphTestCase):
    """错误恢复不得污染 checkpoint，随后仍应能提交正确输入。"""

    def assert_label_checkpoint_unchanged(self) -> None:
        """失败后仍应等待原 request，且完成中断计数没有增加。"""

        state = load_checkpoint_state(self.graph, self.config)
        self.assertEqual(state.pending_request, self.first_state.pending_request)
        self.assertEqual(state.completed_interrupts, 0)
        self.assertEqual(self.graph.get_state(self.config).next, ("wait_for_observation",))

    def test_unknown_thread_is_rejected_before_graph_execution(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="wrong-thread-observation",
            field="charger_label",
            value="65W",
        )

        with self.assertRaisesRegex(ValueError, "no checkpoint exists"):
            resume_active_observation(
                self.graph,
                graph_config("another-thread"),
                payload,
                interrupt_id=self.current_interrupt_id,
            )
        self.assert_label_checkpoint_unchanged()

    def test_stale_interrupt_id_is_rejected_before_consuming_checkpoint(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="stale-interrupt-observation",
            field="charger_label",
            value="65W",
        )

        with self.assertRaisesRegex(ValueError, "interrupt_id does not match"):
            resume_active_observation(
                self.graph,
                self.config,
                payload,
                interrupt_id="stale-interrupt-id",
            )
        self.assert_label_checkpoint_unchanged()

    def test_wrong_resume_kind_is_rejected_then_correct_value_can_retry(self) -> None:
        wrong = UserResume(
            target_id=self.first_state.target.target_id,
            field="laptop_model",
            value="ExampleBook",
            source_id="wrong-kind-message",
        ).model_dump(mode="json")

        with self.assertRaisesRegex(ValueError, "requires observation_result"):
            self.resume(wrong)
        self.assert_label_checkpoint_unchanged()

        result = self.resume_label()
        _, pause = extract_pause(result)
        self.assertIsInstance(pause, ObservationPausePayload)

    def test_wrong_request_id_is_rejected(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="wrong-request-observation",
            field="charger_label",
            value="65W",
        )
        payload["observation"]["request_id"] = "another-request"

        with self.assertRaisesRegex(ValueError, "request_id does not match"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_wrong_target_is_rejected(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="wrong-target-observation",
            field="charger_label",
            value="65W",
        )
        payload["observation"]["target_id"] = "another-charger"

        with self.assertRaisesRegex(ValueError, "target_id does not match"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_wrong_view_is_rejected(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="wrong-view-observation",
            field="charger_label",
            value="65W",
        )
        payload["observation"]["view_type"] = ViewType.PORT_CLOSEUP.value

        with self.assertRaisesRegex(ValueError, "view_type does not match"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_failed_observation_is_rejected(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="failed-observation",
            field="charger_label",
            value="65W",
        )
        payload["observation"]["status"] = ObservationStatus.FAILED.value
        payload["observation"]["failure_reason"] = "摄像头断开"

        with self.assertRaisesRegex(ValueError, "only accepted observation"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_evidence_source_must_equal_observation_id(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="source-observation",
            field="charger_label",
            value="65W",
        )
        payload["evidence"][0]["source_id"] = "another-observation"

        with self.assertRaisesRegex(ValueError, "source_id must equal"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_unrequested_evidence_field_is_rejected(self) -> None:
        payload = make_observation_resume(
            self.first_state.pending_request,
            observation_id="unrequested-field-observation",
            field="charger_label",
            value="65W",
        )
        payload["evidence"][0]["field"] = "charger_port_type"

        with self.assertRaisesRegex(ValueError, "field was not requested"):
            self.resume(payload)
        self.assert_label_checkpoint_unchanged()

    def test_empty_evidence_is_valid_but_requests_label_again(self) -> None:
        result = self.resume_label(evidence=False)
        _, pause = extract_pause(result)
        state = load_checkpoint_state(self.graph, self.config)

        self.assertIsInstance(pause, ObservationPausePayload)
        self.assertEqual(state.pending_request.view_type, ViewType.BACK_LABEL)
        self.assertNotEqual(
            state.pending_request.request_id,
            self.first_state.pending_request.request_id,
        )
        self.assertNotIn("charger_label", state.belief.confirmed)
        self.assertIn(ViewType.BACK_LABEL, state.belief.observed_views)
        self.assertEqual(state.completed_interrupts, 1)

    def test_wrong_user_field_is_rejected_without_consuming_interrupt(self) -> None:
        state = self.reach_user_pause()
        wrong = UserResume(
            target_id=state.target.target_id,
            field="charger_label",
            value="错误字段",
            source_id="wrong-field-message",
        ).model_dump(mode="json")

        with self.assertRaisesRegex(ValueError, "field does not match"):
            self.resume(wrong)

        after = load_checkpoint_state(self.graph, self.config)
        self.assertEqual(after.expected_user_field, "laptop_model")
        self.assertEqual(after.completed_interrupts, 2)

    def test_null_user_value_is_rejected_by_pydantic(self) -> None:
        state = self.reach_user_pause()
        wrong = {
            "kind": "user_input",
            "target_id": state.target.target_id,
            "field": "laptop_model",
            "value": None,
            "source_id": "null-message",
        }

        with self.assertRaises(ValidationError):
            self.resume(wrong)


class ThreadIsolationTests(unittest.TestCase):
    """同一个 saver 中的两个 thread 不能共享 checkpoint 或 BeliefState。"""

    def test_two_threads_advance_independently(self) -> None:
        graph, _ = build_active_observation_graph()
        state_a = build_initial_state(
            session_id="session-a",
            target_id="charger-a",
        )
        state_b = build_initial_state(
            session_id="session-b",
            target_id="charger-b",
        )
        config_a = graph_config("session-a")
        config_b = graph_config("session-b")
        result_a = graph.invoke(state_a, config=config_a)
        graph.invoke(state_b, config=config_b)
        interrupt_a, _ = extract_pause(result_a)

        checkpoint_a = load_checkpoint_state(graph, config_a)
        payload_a = make_observation_resume(
            checkpoint_a.pending_request,
            observation_id="observation-a",
            field="charger_label",
            value="Label A",
        )
        resume_active_observation(
            graph,
            config_a,
            payload_a,
            interrupt_id=interrupt_a.id,
        )

        advanced_a = load_checkpoint_state(graph, config_a)
        unchanged_b = load_checkpoint_state(graph, config_b)
        self.assertIn("charger_label", advanced_a.belief.confirmed)
        self.assertNotIn("charger_label", unchanged_b.belief.confirmed)
        self.assertEqual(advanced_a.target.target_id, "charger-a")
        self.assertEqual(unchanged_b.target.target_id, "charger-b")


class BeliefMergeTests(unittest.TestCase):
    """恢复证据的合并不能静默覆盖不同值。"""

    def test_same_value_adds_supporting_source(self) -> None:
        belief = build_initial_state().belief
        first = make_evidence(
            evidence_id="label-source-1",
            target_id=belief.target_id,
            field="charger_label",
            value="65W",
            source_type="vision",
            source_id="observation-1",
        )
        second = make_evidence(
            evidence_id="label-source-2",
            target_id=belief.target_id,
            field="charger_label",
            value="65W",
            source_type="manual_review",
            source_id="review-1",
        )

        belief = merge_resumed_evidence(belief, (first,), ViewType.BACK_LABEL)
        belief = merge_resumed_evidence(belief, (second,), ViewType.BACK_LABEL)

        self.assertEqual(
            belief.supporting_evidence_ids["charger_label"],
            ("label-source-1", "label-source-2"),
        )
        self.assertNotIn("charger_label", belief.conflicts)

    def test_different_values_create_conflict(self) -> None:
        belief = build_initial_state().belief
        first = make_evidence(
            evidence_id="label-65w",
            target_id=belief.target_id,
            field="charger_label",
            value="65W",
            source_type="vision",
            source_id="observation-1",
        )
        second = make_evidence(
            evidence_id="label-45w",
            target_id=belief.target_id,
            field="charger_label",
            value="45W",
            source_type="manual_review",
            source_id="review-1",
        )

        belief = merge_resumed_evidence(belief, (first,), ViewType.BACK_LABEL)
        belief = merge_resumed_evidence(belief, (second,), ViewType.BACK_LABEL)

        self.assertNotIn("charger_label", belief.confirmed)
        self.assertEqual(len(belief.conflicts["charger_label"]), 2)
        self.assertEqual(
            {item.value for item in belief.conflicts["charger_label"]},
            {"65W", "45W"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
