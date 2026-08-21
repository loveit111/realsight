"""
第 4 章自动化测试：验证 Pydantic、LangGraph 与 Protobuf 三层契约真的守住边界。

这个文件的整体逻辑
------------------
测试不只覆盖“正确输入能运行”，还主动构造容易污染系统状态的错误输入：越界置信度、
错误目标、BeliefState 状态区重叠、账本引用断裂、Action 类型与 payload 不一致、
Protobuf 未指定枚举和旧 schema_version。只有这些输入在最靠近边界的位置被拒绝，
后续中断恢复、持久化和 C++ 联调才有可信基础。

使用的技术栈
------------
- Python 标准库 unittest：无需 pytest，也可以运行同步测试。
- Pydantic ValidationError：断言字段级和聚合级验证失败。
- LangGraph：通过第四章编译图验证三条条件路由。
- grpcio-tools + protobuf runtime：临时生成 pb2，验证真实 wire bytes 往返。

测试调用流程
------------
python -m unittest discover -s tests -p "test_ch04*.py" -v
    -> unittest 导入 contracts/models.py 与 ch04_state_and_contracts.py
    -> ContractValidationTests 攻击 Pydantic 对象和聚合不变量
    -> GraphRoutingTests 运行 observe / ask_user / rules 三条 LangGraph 分支
    -> ProtobufContractTests 编译 .proto，检查服务描述和二进制往返
    -> SerializationTests 验证状态可作为第 6 章检查点输入

测试数据全部是确定性的，不需要网络、摄像头、LLM 或外部数据库。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples"
for import_path in (PROJECT_ROOT, EXAMPLES_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from ch04_state_and_contracts import (  # noqa: E402
    add_confirmed_evidence,
    build_initial_state,
    decode_observation,
    decode_observation_request,
    encode_observation,
    encode_observation_request,
    load_generated_proto_module,
    make_evidence,
    observation_from_proto,
    observation_request_from_proto,
    rebuild_state,
    run_planning_graph,
)
from contracts.models import (  # noqa: E402
    Action,
    ActionType,
    AskUserPayload,
    BeliefState,
    EvidenceStatus,
    GraphRoute,
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
    RealSightGraphState,
    RequestViewPayload,
    RunEvent,
    RunEventType,
    TaskSession,
    ViewType,
)


def make_request(*, correlation_id: str | None = "plan-test") -> ObservationRequest:
    """创建多个测试共用的合法观察请求。"""

    return ObservationRequest(
        request_id="request-test-001",
        session_id="session-test-001",
        target_id="charger-test-001",
        view_type=ViewType.BACK_LABEL,
        required_features=("charger_label", "charger_protocol"),
        instruction="请拍摄背面标签",
        reason="需要读取额定参数",
        timeout_ms=3_000,
        correlation_id=correlation_id,
    )


def make_observation(
    *,
    status: ObservationStatus = ObservationStatus.ACCEPTED,
    failure_reason: str | None = None,
) -> Observation:
    """创建包含 optional 质量字段的合法观察。"""

    return Observation(
        observation_id="observation-test-001",
        request_id="request-test-001",
        target_id="charger-test-001",
        view_type=ViewType.BACK_LABEL,
        captured_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        quality=QualitySignals(
            overall_score=0.93,
            sharpness=0.91,
            exposure=None,
            glare=0.92,
            target_ratio=0.72,
        ),
        local_features=("text_region", "usb_c_logo"),
        image_path="artifacts/observation-test-001.jpg",
        status=status,
        failure_reason=failure_reason,
        source_frame_sequence=42,
        source_position_ms=1_680,
    )


class ContractValidationTests(unittest.TestCase):
    """字段和跨对象错误应在进入图之前被 Pydantic 拒绝。"""

    def test_task_session_requires_thread_id_to_equal_session_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "thread_id must equal session_id"):
            TaskSession(
                session_id="session-001",
                thread_id="another-thread",
                intent="检查兼容性",
                target_id="charger-001",
            )

    def test_contract_rejects_unknown_extra_field(self) -> None:
        payload = make_request().model_dump(mode="python")
        payload["silent_typo"] = True

        with self.assertRaises(ValidationError):
            ObservationRequest.model_validate(payload)

    def test_confidence_rejects_out_of_range_and_string_values(self) -> None:
        with self.assertRaises(ValidationError):
            make_evidence(
                evidence_id="ev-high",
                target_id="charger-001",
                field="charger_label",
                value="65W",
                source_type="ocr",
                source_id="obs-001",
                confidence=1.01,
            )

        with self.assertRaises(ValidationError):
            make_evidence(
                evidence_id="ev-string",
                target_id="charger-001",
                field="charger_label",
                value="65W",
                source_type="ocr",
                source_id="obs-001",
                confidence="0.9",  # type: ignore[arg-type]
            )

    def test_observation_requires_timezone(self) -> None:
        payload = make_observation().model_dump(mode="python")
        payload["captured_at"] = datetime(2026, 8, 3, 12, 0)

        with self.assertRaises(ValidationError):
            Observation.model_validate(payload)

    def test_observation_status_and_failure_reason_must_agree(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires failure_reason"):
            make_observation(status=ObservationStatus.FAILED)

        with self.assertRaisesRegex(ValidationError, "must not contain failure_reason"):
            make_observation(
                status=ObservationStatus.ACCEPTED,
                failure_reason="不应存在",
            )

    def test_belief_status_zones_are_mutually_exclusive(self) -> None:
        evidence = make_evidence(
            evidence_id="ev-label",
            target_id="charger-001",
            field="charger_label",
            value="65W",
            source_type="ocr",
            source_id="obs-001",
        )

        with self.assertRaisesRegex(ValidationError, "status zones overlap"):
            BeliefState(
                target_id="charger-001",
                confirmed={"charger_label": evidence},
                unknown=frozenset({"charger_label"}),
                ledger={evidence.evidence_id: evidence},
                supporting_evidence_ids={"charger_label": (evidence.evidence_id,)},
            )

    def test_belief_rejects_cross_target_evidence(self) -> None:
        evidence = make_evidence(
            evidence_id="ev-label",
            target_id="another-charger",
            field="charger_label",
            value="65W",
            source_type="ocr",
            source_id="obs-001",
        )

        with self.assertRaisesRegex(ValidationError, "target does not match"):
            BeliefState(
                target_id="charger-001",
                confirmed={"charger_label": evidence},
                ledger={evidence.evidence_id: evidence},
                supporting_evidence_ids={"charger_label": (evidence.evidence_id,)},
            )

    def test_belief_rejects_broken_ledger_reference(self) -> None:
        evidence = make_evidence(
            evidence_id="ev-label",
            target_id="charger-001",
            field="charger_label",
            value="65W",
            source_type="ocr",
            source_id="obs-001",
        )

        with self.assertRaisesRegex(ValidationError, "exist unchanged in ledger"):
            BeliefState(
                target_id="charger-001",
                confirmed={"charger_label": evidence},
                ledger={},
                supporting_evidence_ids={"charger_label": (evidence.evidence_id,)},
            )

    def test_belief_rejects_duplicate_items_disguised_as_conflict(self) -> None:
        evidence = make_evidence(
            evidence_id="ev-duplicate",
            target_id="charger-001",
            field="charger_max_power_w",
            value=65,
            source_type="ocr",
            source_id="obs-001",
        )

        with self.assertRaisesRegex(ValidationError, "IDs must be unique"):
            BeliefState(
                target_id="charger-001",
                conflicts={"charger_max_power_w": (evidence, evidence)},
                ledger={evidence.evidence_id: evidence},
            )

    def test_belief_support_must_include_active_evidence(self) -> None:
        active = make_evidence(
            evidence_id="ev-active",
            target_id="charger-001",
            field="charger_label",
            value="65W",
            source_type="ocr",
            source_id="obs-001",
        )
        another = make_evidence(
            evidence_id="ev-another",
            target_id="charger-001",
            field="charger_label",
            value="65W",
            source_type="manual_review",
            source_id="review-001",
        )

        with self.assertRaisesRegex(ValidationError, "active evidence must be listed"):
            BeliefState(
                target_id="charger-001",
                confirmed={"charger_label": active},
                ledger={active.evidence_id: active, another.evidence_id: another},
                supporting_evidence_ids={"charger_label": (another.evidence_id,)},
            )

    def test_action_type_must_match_discriminated_payload(self) -> None:
        request = make_request()

        with self.assertRaisesRegex(ValidationError, "action_type must match"):
            Action(
                action_id="action-001",
                target_id=request.target_id,
                action_type=ActionType.ASK_USER,
                reason="故意制造不一致",
                payload=RequestViewPayload(request=request),
            )

    def test_request_action_cannot_cross_target(self) -> None:
        request = make_request()

        with self.assertRaisesRegex(ValidationError, "request target must match"):
            Action(
                action_id="action-001",
                target_id="another-charger",
                action_type=ActionType.REQUEST_VIEW,
                reason="故意制造跨目标请求",
                payload=RequestViewPayload(request=request),
            )

    def test_graph_state_requires_strict_event_sequence(self) -> None:
        state = build_initial_state()
        event_2 = RunEvent(
            event_id="event-002",
            session_id=state.session.session_id,
            sequence=2,
            event_type=RunEventType.BELIEF_UPDATED,
            message="第二条",
        )
        event_1 = RunEvent(
            event_id="event-001",
            session_id=state.session.session_id,
            sequence=1,
            event_type=RunEventType.TASK_STARTED,
            message="第一条",
        )
        payload = state.model_dump(mode="python")
        payload["events"] = (event_2, event_1)

        with self.assertRaisesRegex(ValidationError, "strictly increasing"):
            RealSightGraphState.model_validate(payload)


class GraphRoutingTests(unittest.TestCase):
    """最小图必须稳定走完请求观察、询问用户、准备规则三条路径。"""

    def test_initial_state_routes_to_observation(self) -> None:
        result = run_planning_graph(build_initial_state())

        self.assertEqual(result.route, GraphRoute.REQUEST_OBSERVATION)
        self.assertEqual(result.pending_actions[0].action_type, ActionType.REQUEST_VIEW)
        self.assertIsNotNone(result.pending_request)
        self.assertEqual(result.pending_request.required_features, ("charger_label",))

    def test_visual_evidence_complete_routes_to_user(self) -> None:
        state = run_planning_graph(build_initial_state())
        label = make_evidence(
            evidence_id="ev-label-002",
            target_id=state.target.target_id,
            field="charger_label",
            value="USB-C PD 65W",
            source_type="vision_ocr",
            source_id="observation-002",
            confidence=0.96,
        )
        next_state = rebuild_state(
            state,
            belief=add_confirmed_evidence(state.belief, label),
            iteration=1,
        )

        result = run_planning_graph(next_state)

        self.assertEqual(result.route, GraphRoute.AWAIT_USER)
        self.assertIsInstance(result.pending_actions[0].payload, AskUserPayload)
        self.assertEqual(result.pending_actions[0].payload.expected_field, "laptop_model")

    def test_complete_evidence_routes_to_rules(self) -> None:
        state = run_planning_graph(build_initial_state())
        for iteration, evidence in enumerate(
            (
                make_evidence(
                    evidence_id="ev-label-003",
                    target_id=state.target.target_id,
                    field="charger_label",
                    value="USB-C PD 65W",
                    source_type="vision_ocr",
                    source_id="observation-003",
                ),
                make_evidence(
                    evidence_id="ev-laptop-003",
                    target_id=state.target.target_id,
                    field="laptop_model",
                    value="ExampleBook Pro 14",
                    source_type="user_input",
                    source_id="message-003",
                ),
            ),
            start=1,
        ):
            state = rebuild_state(
                state,
                belief=add_confirmed_evidence(state.belief, evidence),
                iteration=iteration,
            )

        result = run_planning_graph(state)

        self.assertEqual(result.route, GraphRoute.RUN_RULES)
        self.assertEqual(result.pending_actions[0].action_type, ActionType.RUN_RULES)
        self.assertEqual(result.missing_fields, ())


class ProtobufContractTests(unittest.TestCase):
    """验证 .proto 可以真实编译，且 wire 往返后仍执行 Pydantic 语义校验。"""

    def test_proto_compiles_and_declares_streaming_service(self) -> None:
        module = load_generated_proto_module()
        service = module.DESCRIPTOR.services_by_name["PerceptionRuntime"]
        observe_method = service.methods_by_name["Observe"]

        self.assertTrue(observe_method.server_streaming)
        self.assertFalse(observe_method.client_streaming)
        self.assertEqual(observe_method.input_type.name, "ObservationRequest")
        self.assertEqual(observe_method.output_type.name, "ObservationEvent")

    def test_request_binary_round_trip_preserves_optional_field(self) -> None:
        request = make_request(correlation_id="trace-123")

        restored = decode_observation_request(encode_observation_request(request))

        self.assertEqual(restored, request)
        self.assertEqual(restored.correlation_id, "trace-123")

    def test_request_binary_round_trip_preserves_absent_optional_field(self) -> None:
        request = make_request(correlation_id=None)

        restored = decode_observation_request(encode_observation_request(request))

        self.assertEqual(restored, request)
        self.assertIsNone(restored.correlation_id)

    def test_unspecified_proto_enum_is_rejected_by_adapter(self) -> None:
        module = load_generated_proto_module()
        message = module.ObservationRequest(
            schema_version=1,
            request_id="request-001",
            session_id="session-001",
            target_id="charger-001",
            view_type=module.VIEW_TYPE_UNSPECIFIED,
            required_features=["charger_label"],
            instruction="拍摄标签",
            reason="读取参数",
            timeout_ms=2_000,
        )

        with self.assertRaisesRegex(ValueError, "unspecified protobuf view_type"):
            observation_request_from_proto(message, module)

    def test_old_or_missing_schema_version_is_rejected(self) -> None:
        module = load_generated_proto_module()
        message = module.ObservationRequest(
            schema_version=0,
            request_id="request-001",
            session_id="session-001",
            target_id="charger-001",
            view_type=module.VIEW_TYPE_BACK_LABEL,
            required_features=["charger_label"],
            instruction="拍摄标签",
            reason="读取参数",
            timeout_ms=2_000,
        )

        with self.assertRaises(ValidationError):
            observation_request_from_proto(message, module)

    def test_observation_binary_round_trip_preserves_presence(self) -> None:
        observation = make_observation()

        restored = decode_observation(encode_observation(observation))

        self.assertEqual(restored, observation)
        self.assertIsNone(restored.quality.exposure)
        self.assertEqual(restored.quality.sharpness, 0.91)
        self.assertEqual(restored.image_path, observation.image_path)

    def test_missing_required_proto_presence_is_rejected(self) -> None:
        module = load_generated_proto_module()
        message_without_time = module.Observation(
            schema_version=1,
            observation_id="observation-001",
            request_id="request-001",
            target_id="charger-001",
            view_type=module.VIEW_TYPE_BACK_LABEL,
            quality=module.QualitySignals(overall_score=0.9),
            status=module.OBSERVATION_STATUS_ACCEPTED,
        )
        with self.assertRaisesRegex(ValueError, "requires captured_at"):
            observation_from_proto(message_without_time, module)

        message_without_score = module.Observation(
            schema_version=1,
            observation_id="observation-001",
            request_id="request-001",
            target_id="charger-001",
            view_type=module.VIEW_TYPE_BACK_LABEL,
            captured_at_unix_ms=1_786_000_000_000,
            quality=module.QualitySignals(),
            status=module.OBSERVATION_STATUS_ACCEPTED,
        )
        with self.assertRaisesRegex(ValueError, "requires overall_score"):
            observation_from_proto(message_without_score, module)

    def test_failed_observation_binary_round_trip(self) -> None:
        observation = make_observation(
            status=ObservationStatus.FAILED,
            failure_reason="摄像头已断开",
        )

        restored = decode_observation(encode_observation(observation))

        self.assertEqual(restored.status, ObservationStatus.FAILED)
        self.assertEqual(restored.failure_reason, "摄像头已断开")

    def test_proto_keeps_python_business_state_out_of_runtime_boundary(self) -> None:
        proto_text = (PROJECT_ROOT / "contracts" / "realsight.proto").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("message Evidence", proto_text)
        self.assertNotIn("message BeliefState", proto_text)
        self.assertNotIn("message Action", proto_text)

    def test_generated_pb2_is_not_committed_as_learning_source(self) -> None:
        generated_file = PROJECT_ROOT / "contracts" / "realsight_pb2.py"

        self.assertFalse(generated_file.exists())


class SerializationTests(unittest.TestCase):
    """状态必须可稳定序列化，才能在第 6 章进入检查点存储。"""

    def test_graph_state_json_round_trip(self) -> None:
        state = run_planning_graph(build_initial_state())

        restored = RealSightGraphState.model_validate_json(state.model_dump_json())

        self.assertEqual(restored, state)

    def test_state_rejects_pending_request_from_another_session(self) -> None:
        state = build_initial_state()
        wrong_request = ObservationRequest(
            request_id="request-wrong-session",
            session_id="another-session",
            target_id=state.target.target_id,
            view_type=ViewType.BACK_LABEL,
            required_features=("charger_label",),
            instruction="拍摄背面",
            reason="测试会话隔离",
            timeout_ms=2_000,
        )
        payload = state.model_dump(mode="python")
        payload["pending_request"] = wrong_request

        with self.assertRaisesRegex(ValidationError, "request session does not match"):
            RealSightGraphState.model_validate(payload)

    def test_state_requires_action_and_pending_request_to_match(self) -> None:
        state = build_initial_state()
        pending_request = ObservationRequest(
            request_id="request-a",
            session_id=state.session.session_id,
            target_id=state.target.target_id,
            view_type=ViewType.BACK_LABEL,
            required_features=("charger_label",),
            instruction="拍摄背面",
            reason="测试请求引用",
            timeout_ms=2_000,
        )
        action_request = pending_request.model_copy(update={"request_id": "request-b"})
        action = Action(
            action_id="action-request-b",
            target_id=state.target.target_id,
            action_type=ActionType.REQUEST_VIEW,
            reason="测试请求引用",
            payload=RequestViewPayload(request=action_request),
        )
        payload = state.model_dump(mode="python")
        payload.update(
            {
                "pending_request": pending_request,
                "pending_actions": (action,),
                "route": GraphRoute.REQUEST_OBSERVATION,
            }
        )

        with self.assertRaisesRegex(ValidationError, "must equal"):
            RealSightGraphState.model_validate(payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
