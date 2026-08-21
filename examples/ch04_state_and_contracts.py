"""
第 4 章 Demo：把 Pydantic 状态、LangGraph 路由和 Protobuf 传输串成一个最小闭环。

这个文件做什么
------------
本 Demo 不调用大模型，也不连接真实摄像头。它从一份 USB-C 证据状态开始，让真实
LangGraph 根据缺失字段依次选择三条路径：
1. 缺少充电器背面标签时，生成 request_view 与 ObservationRequest。
2. 视觉字段已经齐全但缺少笔记本型号时，生成 ask_user。
3. 所有必要字段齐全时，生成 run_rules，交给第 12 章确定性规则引擎。

第一次产生 ObservationRequest 后，Demo 使用 grpcio-tools 在系统临时目录编译
``contracts/realsight.proto``，将 Pydantic 请求转换为真实 Protobuf 消息、编码成
bytes，再解码回 Pydantic 模型。生成的 ``realsight_pb2.py`` 是构建产物，不是学习
源码，因此不会写入仓库；应阅读的是 .proto 和本文件的显式适配函数。

使用的技术栈
------------
- Pydantic v2：验证八个稳定领域类型和 RealSightGraphState。
- LangGraph StateGraph：定义节点、普通边、条件边、START/END 和编译后的图。
- Protocol Buffers / grpcio-tools：编译 .proto，并验证二进制请求/观察往返。
- Python 标准库：tempfile、importlib、datetime、json 和 pathlib。

调用流程
--------
run_demo()
    -> build_initial_state() 创建“功率与协议已知”的 Pydantic 状态
    -> run_planning_graph() 调用编译后的 LangGraph
        -> assess_evidence_node() 计算 missing_fields
        -> route_after_assessment() 选择条件边
        -> build_observation_node() / ask_user_node() / prepare_rules_node()
    -> encode_observation_request() 将请求转换为 Protobuf bytes
    -> decode_observation_request() 将 bytes 还原并重新执行 Pydantic 校验
    -> add_confirmed_evidence() 模拟补充标签和笔记本型号
    -> 再运行两次图，依次得到 await_user 和 run_rules

关键边界
--------
- Pydantic 模型负责业务语义；Protobuf 只负责 C++/Python 传输，不替代业务校验。
- LangGraph State 只保存可 JSON 序列化数据；gRPC channel、摄像头和模型客户端应通过
  runtime context 注入，不能进入检查点。
- 本章只“准备调用规则”，不提前实现第 12 章 USB-C 兼容性规则。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from grpc_tools import protoc
from langgraph.graph import END, START, StateGraph
from pydantic import JsonValue, ValidationError


# 直接执行本文件时，Python 默认只把 examples 目录放入 sys.path。
# 这里加入项目根目录，才能导入同级 contracts/models.py。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.models import (  # noqa: E402
    Action,
    ActionType,
    AskUserPayload,
    BeliefState,
    Evidence,
    EvidenceStatus,
    GraphRoute,
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
    RealityObject,
    RealSightGraphState,
    RequestViewPayload,
    RunRulesPayload,
    SessionStatus,
    TaskSession,
    ViewType,
)


REQUIRED_USB_C_FIELDS = (
    "charger_label",
    "charger_max_power_w",
    "charger_protocol",
    "laptop_model",
)

# 这些字段可通过摄像头观察获得；其余缺口在本章交给用户补充。
OBSERVABLE_FIELDS = frozenset(
    {"charger_label", "charger_max_power_w", "charger_protocol"}
)


def make_evidence(
    *,
    evidence_id: str,
    target_id: str,
    field: str,
    value: JsonValue,
    source_type: str,
    source_id: str,
    confidence: float = 1.0,
) -> Evidence:
    """创建一条已确认教学证据，统一填入常用字段。"""

    return Evidence(
        evidence_id=evidence_id,
        target_id=target_id,
        field=field,
        value=value,
        source_type=source_type,
        source_id=source_id,
        confidence=confidence,
        status=EvidenceStatus.CONFIRMED,
    )


def build_belief(
    target_id: str,
    evidence_items: tuple[Evidence, ...],
) -> BeliefState:
    """从互不冲突的 confirmed 证据创建一个通过聚合校验的 BeliefState。"""

    confirmed: dict[str, Evidence] = {}
    ledger: dict[str, Evidence] = {}
    supporting: dict[str, tuple[str, ...]] = {}

    for evidence in evidence_items:
        if evidence.target_id != target_id:
            raise ValueError("evidence target does not match build_belief target")
        if evidence.status != EvidenceStatus.CONFIRMED:
            raise ValueError("this teaching helper only accepts confirmed evidence")
        if evidence.evidence_id in ledger:
            raise ValueError(f"duplicate evidence ID: {evidence.evidence_id}")
        if evidence.field in confirmed:
            raise ValueError(f"duplicate confirmed field: {evidence.field}")

        confirmed[evidence.field] = evidence
        ledger[evidence.evidence_id] = evidence
        supporting[evidence.field] = (evidence.evidence_id,)

    return BeliefState(
        target_id=target_id,
        confirmed=confirmed,
        ledger=ledger,
        supporting_evidence_ids=supporting,
    )


def add_confirmed_evidence(
    belief: BeliefState,
    evidence: Evidence,
) -> BeliefState:
    """返回加入一条 confirmed 证据后的新 BeliefState，不原地修改旧快照。"""

    if evidence.target_id != belief.target_id:
        raise ValueError("new evidence target does not match belief target")
    if evidence.status != EvidenceStatus.CONFIRMED:
        raise ValueError("this helper only adds confirmed evidence")
    if evidence.evidence_id in belief.ledger:
        raise ValueError(f"evidence ID already exists: {evidence.evidence_id}")
    if evidence.field in belief.confirmed:
        raise ValueError(
            "this chapter does not silently replace confirmed evidence; "
            "conflict resolution is a separate workflow"
        )

    confirmed = dict(belief.confirmed)
    ledger = dict(belief.ledger)
    supporting = dict(belief.supporting_evidence_ids)
    confirmed[evidence.field] = evidence
    ledger[evidence.evidence_id] = evidence
    supporting[evidence.field] = (evidence.evidence_id,)

    return BeliefState(
        target_id=belief.target_id,
        confirmed=confirmed,
        probable=dict(belief.probable),
        unknown=belief.unknown - {evidence.field},
        conflicts=dict(belief.conflicts),
        observed_views=belief.observed_views,
        ledger=ledger,
        supporting_evidence_ids=supporting,
    )


def rebuild_state(
    state: RealSightGraphState,
    *,
    belief: BeliefState,
    iteration: int,
) -> RealSightGraphState:
    """用新证据创建下一轮状态，并显式清空上一轮的临时决策输出。"""

    data = state.model_dump(mode="python")
    data.update(
        {
            "belief": belief,
            "iteration": iteration,
            "missing_fields": (),
            "pending_request": None,
            "pending_actions": (),
            "route": None,
        }
    )
    # 不直接使用 model_copy(update=...)，因为它不会重新验证 update 中的所有值。
    return RealSightGraphState.model_validate(data)


def build_initial_state() -> RealSightGraphState:
    """创建 Demo 初始状态：已知 65W 与 USB PD，缺少标签图和笔记本型号。"""

    target_id = "charger-001"
    initial_evidence = (
        make_evidence(
            evidence_id="ev-power-001",
            target_id=target_id,
            field="charger_max_power_w",
            value=65,
            source_type="user_note",
            source_id="note-001",
        ),
        make_evidence(
            evidence_id="ev-protocol-001",
            target_id=target_id,
            field="charger_protocol",
            value="usb_pd",
            source_type="user_note",
            source_id="note-001",
        ),
    )

    return RealSightGraphState(
        session=TaskSession(
            session_id="session-001",
            thread_id="session-001",
            intent="判断 USB-C 充电兼容性",
            target_id=target_id,
            status=SessionStatus.RUNNING,
        ),
        target=RealityObject(target_id=target_id, category="usb_c_charger"),
        belief=build_belief(target_id, initial_evidence),
        required_fields=REQUIRED_USB_C_FIELDS,
    )


def assess_evidence_node(state: RealSightGraphState) -> dict[str, Any]:
    """LangGraph 节点 1：按稳定顺序计算仍未 confirmed 的必要字段。"""

    missing = tuple(
        field_name
        for field_name in state.required_fields
        if field_name not in state.belief.confirmed
        or field_name in state.belief.conflicts
    )
    return {"missing_fields": missing}


def route_after_assessment(
    state: RealSightGraphState,
) -> Literal["observe", "ask_user", "rules"]:
    """条件边：视觉缺口优先观察，其次询问用户，全部齐全才运行规则。"""

    if any(field_name in OBSERVABLE_FIELDS for field_name in state.missing_fields):
        return "observe"
    if state.missing_fields:
        return "ask_user"
    return "rules"


def build_observation_node(state: RealSightGraphState) -> dict[str, Any]:
    """LangGraph 节点 2A：把可观察缺口转换为请求和 REQUEST_VIEW 动作。"""

    observable_missing = tuple(
        field_name
        for field_name in state.missing_fields
        if field_name in OBSERVABLE_FIELDS
    )
    request = ObservationRequest(
        request_id=f"obs-req-{state.iteration + 1:03d}",
        session_id=state.session.session_id,
        target_id=state.target.target_id,
        view_type=ViewType.BACK_LABEL,
        required_features=observable_missing,
        instruction="请将充电器背面标签移近摄像头并保持稳定",
        reason="需要核对铭牌、额定功率与 USB-C 协议",
        timeout_ms=5_000,
        correlation_id=f"plan-{state.iteration + 1:03d}",
    )
    action = Action(
        action_id=f"action-view-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.REQUEST_VIEW,
        reason="存在可以通过新视角补齐的证据缺口",
        payload=RequestViewPayload(request=request),
    )
    return {
        "pending_request": request,
        "pending_actions": (action,),
        "route": GraphRoute.REQUEST_OBSERVATION,
    }


def ask_user_node(state: RealSightGraphState) -> dict[str, Any]:
    """LangGraph 节点 2B：视觉字段齐全后，请用户提供剩余上下文。"""

    expected_field = state.missing_fields[0]
    action = Action(
        action_id=f"action-user-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.ASK_USER,
        reason="该字段不能从当前充电器画面可靠推断",
        payload=AskUserPayload(
            question="请提供需要充电的笔记本电脑准确型号",
            expected_field=expected_field,
        ),
    )
    return {
        "pending_actions": (action,),
        "route": GraphRoute.AWAIT_USER,
    }


def prepare_rules_node(state: RealSightGraphState) -> dict[str, Any]:
    """LangGraph 节点 2C：证据齐全时只声明下一步，不在本章偷跑规则实现。"""

    action = Action(
        action_id=f"action-rules-{state.iteration + 1:03d}",
        target_id=state.target.target_id,
        action_type=ActionType.RUN_RULES,
        reason="USB-C 兼容性必要字段已经齐全且没有冲突",
        payload=RunRulesPayload(rule_set="usb_c_compatibility_v1"),
    )
    return {
        "pending_actions": (action,),
        "route": GraphRoute.RUN_RULES,
    }


def build_planning_graph() -> Any:
    """组装并编译第四章最小 LangGraph；compile 会检查基础拓扑。"""

    workflow = StateGraph(RealSightGraphState)
    workflow.add_node("assess_evidence", assess_evidence_node)
    workflow.add_node("build_observation", build_observation_node)
    workflow.add_node("ask_user", ask_user_node)
    workflow.add_node("prepare_rules", prepare_rules_node)

    workflow.add_edge(START, "assess_evidence")
    workflow.add_conditional_edges(
        "assess_evidence",
        route_after_assessment,
        {
            "observe": "build_observation",
            "ask_user": "ask_user",
            "rules": "prepare_rules",
        },
    )
    workflow.add_edge("build_observation", END)
    workflow.add_edge("ask_user", END)
    workflow.add_edge("prepare_rules", END)
    return workflow.compile()


PLANNING_GRAPH = build_planning_graph()


def run_planning_graph(state: RealSightGraphState) -> RealSightGraphState:
    """运行编译图，并在图出口再次用 Pydantic 校验完整状态。"""

    result = PLANNING_GRAPH.invoke(state)
    if isinstance(result, RealSightGraphState):
        return result
    return RealSightGraphState.model_validate(result)


# 动态生成的 pb2 模块只编译一次；TemporaryDirectory 会在进程退出时自动清理。
_PROTO_TEMP_DIR: tempfile.TemporaryDirectory[str] | None = None
_PROTO_MODULE: ModuleType | None = None


def load_generated_proto_module() -> ModuleType:
    """把 realsight.proto 编译到临时目录并动态加载生成的 Python 模块。"""

    global _PROTO_TEMP_DIR, _PROTO_MODULE
    if _PROTO_MODULE is not None:
        return _PROTO_MODULE

    contracts_dir = PROJECT_ROOT / "contracts"
    proto_path = contracts_dir / "realsight.proto"
    _PROTO_TEMP_DIR = tempfile.TemporaryDirectory(prefix="realsight_proto_")
    output_dir = Path(_PROTO_TEMP_DIR.name)

    exit_code = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{contracts_dir}",
            f"--python_out={output_dir}",
            str(proto_path),
        ]
    )
    if exit_code != 0:
        raise RuntimeError(f"protoc failed with exit code {exit_code}")

    generated_path = output_dir / "realsight_pb2.py"
    spec = importlib.util.spec_from_file_location("realsight_ch04_pb2", generated_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load generated realsight_pb2 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _PROTO_MODULE = module
    return module


_VIEW_TO_PROTO = {
    ViewType.FULL_OBJECT: "VIEW_TYPE_FULL_OBJECT",
    ViewType.FRONT_LABEL: "VIEW_TYPE_FRONT_LABEL",
    ViewType.BACK_LABEL: "VIEW_TYPE_BACK_LABEL",
    ViewType.PORT_CLOSEUP: "VIEW_TYPE_PORT_CLOSEUP",
}
_PROTO_TO_VIEW = {proto_name: view for view, proto_name in _VIEW_TO_PROTO.items()}
_STATUS_TO_PROTO = {
    ObservationStatus.ACCEPTED: "OBSERVATION_STATUS_ACCEPTED",
    ObservationStatus.REJECTED: "OBSERVATION_STATUS_REJECTED",
    ObservationStatus.FAILED: "OBSERVATION_STATUS_FAILED",
}
_PROTO_TO_STATUS = {
    proto_name: status for status, proto_name in _STATUS_TO_PROTO.items()
}


def observation_request_to_proto(
    request: ObservationRequest,
    module: ModuleType,
) -> Any:
    """显式把业务请求映射到生成的 Protobuf 类型，不依赖字段名碰巧相同。"""

    message = module.ObservationRequest(
        schema_version=request.schema_version,
        request_id=request.request_id,
        session_id=request.session_id,
        target_id=request.target_id,
        view_type=getattr(module, _VIEW_TO_PROTO[request.view_type]),
        required_features=list(request.required_features),
        instruction=request.instruction,
        reason=request.reason,
        timeout_ms=request.timeout_ms,
    )
    if request.correlation_id is not None:
        message.correlation_id = request.correlation_id
    return message


def observation_request_from_proto(message: Any, module: ModuleType) -> ObservationRequest:
    """将 Protobuf 请求转回 Pydantic；枚举、版本和业务约束在这里重新检查。"""

    try:
        view_name = module.ViewType.Name(message.view_type)
        view_type = _PROTO_TO_VIEW[view_name]
    except (KeyError, ValueError) as error:
        raise ValueError("unsupported or unspecified protobuf view_type") from error

    return ObservationRequest(
        schema_version=message.schema_version,
        request_id=message.request_id,
        session_id=message.session_id,
        target_id=message.target_id,
        view_type=view_type,
        required_features=tuple(message.required_features),
        instruction=message.instruction,
        reason=message.reason,
        timeout_ms=message.timeout_ms,
        correlation_id=(
            message.correlation_id if message.HasField("correlation_id") else None
        ),
    )


def encode_observation_request(request: ObservationRequest) -> bytes:
    """Pydantic -> Protobuf -> bytes；deterministic 便于测试，不代表加密或签名。"""

    module = load_generated_proto_module()
    message = observation_request_to_proto(request, module)
    return message.SerializeToString(deterministic=True)


def decode_observation_request(payload: bytes) -> ObservationRequest:
    """bytes -> Protobuf -> Pydantic；不允许只解析 wire 数据而跳过语义校验。"""

    module = load_generated_proto_module()
    message = module.ObservationRequest()
    message.ParseFromString(payload)
    return observation_request_from_proto(message, module)


def observation_to_proto(observation: Observation, module: ModuleType) -> Any:
    """把 Observation 映射到 Protobuf，保留 optional 质量字段的“是否存在”语义。"""

    quality = module.QualitySignals(overall_score=observation.quality.overall_score)
    for field_name in ("sharpness", "exposure", "glare", "target_ratio"):
        value = getattr(observation.quality, field_name)
        if value is not None:
            setattr(quality, field_name, value)

    message = module.Observation(
        schema_version=observation.schema_version,
        observation_id=observation.observation_id,
        request_id=observation.request_id,
        target_id=observation.target_id,
        view_type=getattr(module, _VIEW_TO_PROTO[observation.view_type]),
        captured_at_unix_ms=int(observation.captured_at.timestamp() * 1_000),
        quality=quality,
        local_features=list(observation.local_features),
        status=getattr(module, _STATUS_TO_PROTO[observation.status]),
    )
    if observation.image_path is not None:
        message.image_path = observation.image_path
    if observation.failure_reason is not None:
        message.failure_reason = observation.failure_reason
    if observation.source_frame_sequence is not None:
        message.source_frame_sequence = observation.source_frame_sequence
    if observation.source_position_ms is not None:
        message.source_position_ms = observation.source_position_ms
    return message


def observation_from_proto(message: Any, module: ModuleType) -> Observation:
    """把 Protobuf Observation 恢复成经过完整校验的 Pydantic Observation。"""

    if not message.HasField("captured_at_unix_ms"):
        raise ValueError("protobuf observation requires captured_at_unix_ms")
    if not message.HasField("quality"):
        raise ValueError("protobuf observation requires quality")
    if not message.quality.HasField("overall_score"):
        raise ValueError("protobuf quality requires overall_score")

    try:
        view_type = _PROTO_TO_VIEW[module.ViewType.Name(message.view_type)]
        status = _PROTO_TO_STATUS[
            module.ObservationStatus.Name(message.status)
        ]
    except (KeyError, ValueError) as error:
        raise ValueError("unsupported protobuf observation enum value") from error

    quality_values: dict[str, float | None] = {
        "overall_score": message.quality.overall_score,
    }
    for field_name in ("sharpness", "exposure", "glare", "target_ratio"):
        quality_values[field_name] = (
            getattr(message.quality, field_name)
            if message.quality.HasField(field_name)
            else None
        )

    return Observation(
        schema_version=message.schema_version,
        observation_id=message.observation_id,
        request_id=message.request_id,
        target_id=message.target_id,
        view_type=view_type,
        captured_at=datetime.fromtimestamp(
            message.captured_at_unix_ms / 1_000,
            tz=timezone.utc,
        ),
        quality=QualitySignals(**quality_values),
        local_features=tuple(message.local_features),
        image_path=message.image_path if message.HasField("image_path") else None,
        status=status,
        failure_reason=(
            message.failure_reason if message.HasField("failure_reason") else None
        ),
        source_frame_sequence=(
            message.source_frame_sequence
            if message.HasField("source_frame_sequence")
            else None
        ),
        source_position_ms=(
            message.source_position_ms
            if message.HasField("source_position_ms")
            else None
        ),
    )


def encode_observation(observation: Observation) -> bytes:
    """将 Pydantic Observation 编码成 Protobuf 二进制。"""

    module = load_generated_proto_module()
    return observation_to_proto(observation, module).SerializeToString(deterministic=True)


def decode_observation(payload: bytes) -> Observation:
    """将 Protobuf 二进制解码为 Pydantic Observation。"""

    module = load_generated_proto_module()
    message = module.Observation()
    message.ParseFromString(payload)
    return observation_from_proto(message, module)


def print_decision(label: str, state: RealSightGraphState) -> None:
    """用稳定、易读的 JSON 打印每轮路由结果。"""

    action = state.pending_actions[0]
    summary = {
        "step": label,
        "missing_fields": list(state.missing_fields),
        "route": state.route.value if state.route is not None else None,
        "action_type": action.action_type.value,
        "action_payload": action.payload.model_dump(mode="json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def demonstrate_rejected_input() -> None:
    """展示 Pydantic 如何拒绝越界置信度，而不是让错误流入图。"""

    try:
        make_evidence(
            evidence_id="ev-invalid",
            target_id="charger-001",
            field="charger_label",
            value="65W",
            source_type="ocr",
            source_id="obs-invalid",
            confidence=1.2,
        )
    except ValidationError as error:
        first_error = error.errors()[0]
        print(
            "Pydantic rejected invalid confidence:",
            f"location={first_error['loc']}, type={first_error['type']}",
        )


def run_demo() -> None:
    """运行三轮状态路由、一次 Protobuf 往返和一次 JSON 检查点往返。"""

    print("=== RealSight Chapter 4: state and contracts ===")
    print(
        "versions:",
        {
            name: importlib.metadata.version(name)
            for name in ("pydantic", "protobuf", "grpcio-tools", "langgraph")
        },
    )
    demonstrate_rejected_input()

    # 第一轮：缺少视觉字段，状态图产生 request_view。
    state = run_planning_graph(build_initial_state())
    print_decision("1-request-observation", state)
    assert state.pending_request is not None

    wire_bytes = encode_observation_request(state.pending_request)
    restored_request = decode_observation_request(wire_bytes)
    print(
        "protobuf round-trip:",
        {
            "wire_bytes": len(wire_bytes),
            "request_equal": restored_request == state.pending_request,
            "generated_in_temp": True,
        },
    )

    # 第二轮：模拟视觉子系统从背面标签中提取到铭牌证据，仍缺笔记本型号。
    label_evidence = make_evidence(
        evidence_id="ev-label-001",
        target_id=state.target.target_id,
        field="charger_label",
        value="USB-C PD 65W",
        source_type="vision_ocr",
        source_id="observation-001",
        confidence=0.97,
    )
    belief_with_label = add_confirmed_evidence(state.belief, label_evidence)
    state = run_planning_graph(
        rebuild_state(state, belief=belief_with_label, iteration=1)
    )
    print_decision("2-ask-user", state)

    # 第三轮：模拟用户提供笔记本型号，必要字段齐全，进入确定性规则阶段。
    laptop_evidence = make_evidence(
        evidence_id="ev-laptop-001",
        target_id=state.target.target_id,
        field="laptop_model",
        value="ExampleBook Pro 14",
        source_type="user_input",
        source_id="message-003",
    )
    complete_belief = add_confirmed_evidence(state.belief, laptop_evidence)
    state = run_planning_graph(
        rebuild_state(state, belief=complete_belief, iteration=2)
    )
    print_decision("3-run-rules", state)

    # JSON 往返模拟未来检查点最小要求：状态必须能序列化并恢复为同一模型。
    checkpoint_json = state.model_dump_json()
    restored_state = RealSightGraphState.model_validate_json(checkpoint_json)
    print(
        "checkpoint JSON round-trip:",
        {
            "json_bytes": len(checkpoint_json.encode("utf-8")),
            "state_equal": restored_state == state,
        },
    )


if __name__ == "__main__":
    run_demo()
