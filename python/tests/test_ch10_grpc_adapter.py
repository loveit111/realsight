"""
第 10 章 Python 协议适配与工程边界测试。

文件整体逻辑
------------
这些测试直接构造生成的 Protobuf 消息，验证手写适配器会保留 optional 字段、重新校验
Pydantic 语义，并拒绝空 payload、ID 不一致和默认时间戳。它还检查代码生成文件带有
学习头部、CMake 将 runtime 与 transport 分层，而不是只看 gRPC 能否导入。

使用的技术栈
------------
- pytest 9：异常断言和普通单元测试。
- grpcio-tools 生成的 ``realsight_pb2``：真实 oneof/optional/enum 行为。
- Pydantic 领域类型与 ``realsight.perception.grpc_client`` 显式适配器。
- pathlib：检查生成文件与 CMake 教学约束。

调用流程
--------
pytest
    -> Pydantic request -> request_to_proto
    -> 构造 progress/observation wire event -> stream_item_from_proto
    -> 检查类型、presence、追踪字段
    -> 构造坏事件 -> 确认适配器拒绝
    -> 检查 generated/CMake 的工程边界。

边界
----
本文件不启动 C++ 进程；跨语言真实网络链路由 ``scripts/run_ch10_demo.py`` 验证，C++
画面算法由 CTest 验证。把三者分开后，失败时能判断是适配、算法还是进程联调问题。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from realsight.contracts import (
    Observation,
    ObservationRequest,
    ObservationStatus,
    ViewType,
)
from realsight.doctor import CheckStatus, check_rpc_toolchain
from realsight.generated import realsight_pb2
from realsight.perception.grpc_client import (
    PerceptionClient,
    PerceptionFailure,
    PerceptionProgress,
    StreamItem,
    request_to_proto,
    stream_item_from_proto,
)
from realsight.workflow.replay import GrpcObservationProvider

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW_MS = int(datetime(2026, 8, 17, 12, 0, tzinfo=UTC).timestamp() * 1_000)


def make_request() -> ObservationRequest:
    """创建多个映射测试共用的合法业务请求。"""

    return ObservationRequest(
        request_id="request-ch10-test",
        session_id="session-ch10-test",
        target_id="charger-ch10-test",
        view_type=ViewType.BACK_LABEL,
        required_features=("charger_label", "charger_protocol"),
        instruction="拍摄背面标签",
        reason="读取协议与功率",
        timeout_ms=4_000,
        correlation_id="trace-ch10-test",
    )


def envelope(
    *, sequence: int = 1, request_id: str = "request-ch10-test"
) -> realsight_pb2.ObservationEvent:
    """构造尚未设置 oneof payload 的合法事件信封。"""

    return realsight_pb2.ObservationEvent(
        schema_version=1,
        event_id=f"event-ch10-{sequence}",
        request_id=request_id,
        sequence=sequence,
        occurred_at_unix_ms=NOW_MS,
    )


def test_request_adapter_preserves_optional_correlation_and_enum() -> None:
    """业务请求映射后必须保留显式 presence，而不是把 None 变成空字符串。"""

    message = request_to_proto(make_request())

    assert message.view_type == realsight_pb2.VIEW_TYPE_BACK_LABEL
    assert tuple(message.required_features) == (
        "charger_label",
        "charger_protocol",
    )
    assert message.HasField("correlation_id")
    assert message.correlation_id == "trace-ch10-test"


def test_progress_event_becomes_typed_dataclass() -> None:
    """进度事件应保留信封序号、UTC 时间和受控百分比。"""

    event = envelope()
    event.progress.CopyFrom(
        realsight_pb2.ObservationProgress(
            request_id="request-ch10-test",
            target_id="charger-ch10-test",
            stage="scanning",
            progress_percent=45,
            message="evaluated=10 accepted=3",
        )
    )

    item = stream_item_from_proto(event)

    assert isinstance(item, PerceptionProgress)
    assert item.sequence == 1
    assert item.progress_percent == 45
    assert item.occurred_at.tzinfo is UTC


def test_observation_preserves_quality_presence_and_source_trace() -> None:
    """未计算 target_ratio 要保持 None，源帧序号和视频位置则必须进入领域对象。"""

    event = envelope(sequence=2)
    event.observation.CopyFrom(
        realsight_pb2.Observation(
            schema_version=1,
            observation_id="observation-ch10-test",
            request_id="request-ch10-test",
            target_id="charger-ch10-test",
            view_type=realsight_pb2.VIEW_TYPE_BACK_LABEL,
            captured_at_unix_ms=NOW_MS,
            quality=realsight_pb2.QualitySignals(
                overall_score=0.82,
                sharpness=0.91,
                exposure=0.73,
                glare=0.88,
            ),
            local_features=("quality:sharp", "quality:low_glare"),
            image_path="runtime-data/observations/keyframe.jpg",
            status=realsight_pb2.OBSERVATION_STATUS_ACCEPTED,
            source_frame_sequence=17,
            source_position_ms=680,
        )
    )

    item = stream_item_from_proto(event)

    assert isinstance(item, Observation)
    assert item.status is ObservationStatus.ACCEPTED
    assert item.quality.target_ratio is None
    assert item.source_frame_sequence == 17
    assert item.source_position_ms == 680


def test_empty_payload_and_mismatched_request_are_rejected() -> None:
    """oneof 缺失或 payload 越过事件 request_id 都不能进入工作流。"""

    with pytest.raises(ValueError, match="exactly one payload"):
        stream_item_from_proto(envelope())

    event = envelope()
    event.progress.CopyFrom(
        realsight_pb2.ObservationProgress(
            request_id="another-request",
            target_id="charger-ch10-test",
            stage="scanning",
            progress_percent=10,
            message="bad envelope",
        )
    )
    with pytest.raises(ValueError, match="does not match"):
        stream_item_from_proto(event)


def test_generated_files_and_cmake_make_the_boundary_visible() -> None:
    """生成代码有学习说明，transport 依赖 proto，而 runtime 不依赖 gRPC。"""

    generated_dir = PROJECT_ROOT / "python" / "src" / "realsight" / "generated"
    for name in ("realsight_pb2.py", "realsight_pb2_grpc.py"):
        header = (generated_dir / name).read_text(encoding="utf-8")[:800]
        assert "文件整体逻辑" in header
        assert "调用流程" in header

    cmake = (PROJECT_ROOT / "cpp" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "find_package(gRPC CONFIG REQUIRED)" in cmake
    assert "add_library(realsight_transport" in cmake
    assert "RealSight::runtime RealSight::proto gRPC::grpc++" in cmake


def test_doctor_sees_real_protobuf_and_grpc_tools() -> None:
    """当前教学机必须能找到真实 protoc、C++ 插件和 Python grpcio runtime。"""

    result = check_rpc_toolchain()

    assert result.status is CheckStatus.PASS
    assert "grpc_cpp_plugin" in result.detail


class FakeProviderClient:
    """让 GrpcObservationProvider 的边界分支无需真实端口即可稳定复现。"""

    def __init__(
        self,
        items: tuple[StreamItem, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.items = items
        self.error = error
        self.cancelled: tuple[str, str] | None = None

    def observe(self, request: ObservationRequest) -> object:
        if self.error is not None:
            raise self.error
        return iter(self.items)

    def cancel(self, request_id: str, reason: str) -> bool:
        self.cancelled = (request_id, reason)
        return True

    def close(self) -> None:
        return None


def test_grpc_provider_reports_structured_failure_and_missing_observation() -> None:
    failure = PerceptionFailure(
        event_id="event-failure",
        request_id=make_request().request_id,
        sequence=1,
        occurred_at=datetime.now(UTC),
        code="NO_ACCEPTED_FRAME",
        message="source ended",
        retryable=True,
    )
    failed_provider = GrpcObservationProvider(
        cast(PerceptionClient, FakeProviderClient((failure,)))
    )
    with pytest.raises(RuntimeError, match="NO_ACCEPTED_FRAME"):
        failed_provider.observe(make_request())

    empty_provider = GrpcObservationProvider(
        cast(PerceptionClient, FakeProviderClient())
    )
    with pytest.raises(RuntimeError, match="without an accepted"):
        empty_provider.observe(make_request())


@pytest.mark.parametrize(
    "error", [TimeoutError("deadline"), ConnectionError("disconnect")]
)
def test_grpc_provider_propagates_timeout_and_disconnect(error: Exception) -> None:
    provider = GrpcObservationProvider(
        cast(PerceptionClient, FakeProviderClient(error=error))
    )
    with pytest.raises(type(error), match=str(error)):
        provider.observe(make_request())


def test_grpc_provider_forwards_cancel_rpc() -> None:
    fake_client = FakeProviderClient()
    provider = GrpcObservationProvider(cast(PerceptionClient, fake_client))
    assert provider.cancel("request-cancel", "user_cancelled")
    assert fake_client.cancelled == ("request-cancel", "user_cancelled")
