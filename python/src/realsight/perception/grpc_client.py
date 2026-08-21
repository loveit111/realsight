"""
第 10 章 Python gRPC 客户端与显式协议适配器。

文件整体逻辑
------------
本模块接收第 4 章 Pydantic ``ObservationRequest``，逐字段构造生成的 Protobuf
请求，并设置有限 deadline 调用 C++ ``Observe`` server stream。收到的每个 wire
事件先检查 schema、ID、sequence、时间和 oneof，再转换为容易理解的进度/失败数据类
或正式 Pydantic ``Observation``。因此跨进程消息不会绕过业务入口校验。

使用的技术栈
------------
- Python 3.12 dataclass、datetime、Iterator 与 context manager。
- grpcio：Channel、生成 stub、server-streaming iterator、deadline 和 Cancel RPC。
- Protobuf generated API：optional presence、oneof 与 enum 数值。
- Pydantic v2 领域模型：ObservationRequest/Observation/QualitySignals 二次校验。

调用流程
--------
``PerceptionClient(address)``
    -> ``observe(pydantic_request)``
    -> ``request_to_proto``
    -> stub.Observe(..., timeout=秒)
    -> 对每个 ObservationEvent 调用 ``stream_item_from_proto``
    -> yield PerceptionProgress / PerceptionFailure / Observation
    -> 调用者消费到流结束并关闭 channel。

``cancel(request_id, reason)`` 则通过独立 unary RPC 请求 C++ stop_source 停止活动采集。

边界
----
客户端不读取图片内容、不做 OCR、不把 Observation 直接升级为 Evidence。gRPC ``timeout``
是整个 RPC 的 deadline，不是每个事件的独立超时；调用者应把 RpcError 作为传输/执行失败
处理，而不是伪造成业务证据。当前使用 insecure channel，只允许连接受信任的本机服务。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import grpc  # type: ignore[import-untyped]

from realsight.contracts import (
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
    ViewType,
)
from realsight.generated import realsight_pb2, realsight_pb2_grpc


@dataclass(frozen=True, slots=True)
class PerceptionProgress:
    """C++ 已经完成的一个低频进度阶段；不是工作流 Action。"""

    event_id: str
    request_id: str
    sequence: int
    occurred_at: datetime
    stage: str
    progress_percent: int
    message: str


@dataclass(frozen=True, slots=True)
class PerceptionFailure:
    """C++ 在有效 gRPC 流中报告的结构化服务失败。"""

    event_id: str
    request_id: str
    sequence: int
    occurred_at: datetime
    code: str
    message: str
    retryable: bool


type StreamItem = PerceptionProgress | PerceptionFailure | Observation

_VIEW_TO_PROTO = {
    ViewType.FULL_OBJECT: realsight_pb2.VIEW_TYPE_FULL_OBJECT,
    ViewType.FRONT_LABEL: realsight_pb2.VIEW_TYPE_FRONT_LABEL,
    ViewType.BACK_LABEL: realsight_pb2.VIEW_TYPE_BACK_LABEL,
    ViewType.PORT_CLOSEUP: realsight_pb2.VIEW_TYPE_PORT_CLOSEUP,
}
_PROTO_TO_VIEW = {value: key for key, value in _VIEW_TO_PROTO.items()}
_PROTO_TO_STATUS = {
    realsight_pb2.OBSERVATION_STATUS_ACCEPTED: ObservationStatus.ACCEPTED,
    realsight_pb2.OBSERVATION_STATUS_REJECTED: ObservationStatus.REJECTED,
    realsight_pb2.OBSERVATION_STATUS_FAILED: ObservationStatus.FAILED,
}


def _aware_datetime(unix_ms: int, field_name: str) -> datetime:
    """把正数 Unix 毫秒转换为 UTC 时间，并拒绝协议默认零值。"""

    if unix_ms <= 0:
        raise ValueError(f"{field_name} must be a positive Unix timestamp")
    return datetime.fromtimestamp(unix_ms / 1_000, tz=UTC)


def request_to_proto(
    request: ObservationRequest,
) -> realsight_pb2.ObservationRequest:
    """显式映射业务请求，避免依赖两个模型字段恰好同名。"""

    message = realsight_pb2.ObservationRequest(
        schema_version=request.schema_version,
        request_id=request.request_id,
        session_id=request.session_id,
        target_id=request.target_id,
        view_type=_VIEW_TO_PROTO[request.view_type],
        required_features=list(request.required_features),
        instruction=request.instruction,
        reason=request.reason,
        timeout_ms=request.timeout_ms,
    )
    if request.correlation_id is not None:
        message.correlation_id = request.correlation_id
    return message


def _observation_from_proto(
    message: realsight_pb2.Observation,
) -> Observation:
    """把最终 Observation 转成 Pydantic，并保留所有 optional presence。"""

    if message.schema_version != 1:
        raise ValueError("observation schema_version must equal 1")
    if not message.HasField("captured_at_unix_ms"):
        raise ValueError("observation requires captured_at_unix_ms")
    if not message.HasField("quality") or not message.quality.HasField("overall_score"):
        raise ValueError("observation requires quality.overall_score")
    try:
        view_type = _PROTO_TO_VIEW[message.view_type]
        status = _PROTO_TO_STATUS[message.status]
    except KeyError as exc:
        raise ValueError("observation contains an unspecified enum") from exc

    optional_quality: dict[str, float | None] = {}
    for field_name in ("sharpness", "exposure", "glare", "target_ratio"):
        optional_quality[field_name] = (
            getattr(message.quality, field_name)
            if message.quality.HasField(field_name)
            else None
        )

    return Observation(
        schema_version=1,
        observation_id=message.observation_id,
        request_id=message.request_id,
        target_id=message.target_id,
        view_type=view_type,
        captured_at=_aware_datetime(message.captured_at_unix_ms, "captured_at_unix_ms"),
        quality=QualitySignals(
            overall_score=message.quality.overall_score,
            **optional_quality,
        ),
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


def stream_item_from_proto(
    event: realsight_pb2.ObservationEvent,
) -> StreamItem:
    """验证事件信封和 oneof，再转换成一个稳定 Python 类型。"""

    if event.schema_version != 1:
        raise ValueError("event schema_version must equal 1")
    if not event.event_id or not event.request_id or event.sequence <= 0:
        raise ValueError("event identity and sequence are required")
    occurred_at = _aware_datetime(event.occurred_at_unix_ms, "occurred_at_unix_ms")
    payload_kind = event.WhichOneof("payload")

    if payload_kind == "progress":
        progress = event.progress
        if progress.request_id != event.request_id:
            raise ValueError("progress request_id does not match event envelope")
        if not 0 <= progress.progress_percent <= 100:
            raise ValueError("progress_percent must be between zero and 100")
        return PerceptionProgress(
            event_id=event.event_id,
            request_id=event.request_id,
            sequence=event.sequence,
            occurred_at=occurred_at,
            stage=progress.stage,
            progress_percent=progress.progress_percent,
            message=progress.message,
        )
    if payload_kind == "failure":
        failure = event.failure
        if failure.request_id != event.request_id:
            raise ValueError("failure request_id does not match event envelope")
        return PerceptionFailure(
            event_id=event.event_id,
            request_id=event.request_id,
            sequence=event.sequence,
            occurred_at=occurred_at,
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
        )
    if payload_kind == "observation":
        observation = _observation_from_proto(event.observation)
        if observation.request_id != event.request_id:
            raise ValueError("observation request_id does not match event envelope")
        return observation
    raise ValueError("observation event must contain exactly one payload")


class PerceptionClient:
    """管理一个本机 gRPC channel，并暴露有 deadline 的观察与取消操作。"""

    def __init__(self, address: str = "127.0.0.1:50051") -> None:
        if not address:
            raise ValueError("gRPC address must not be empty")
        self._channel = grpc.insecure_channel(address)
        self._stub = realsight_pb2_grpc.PerceptionRuntimeStub(  # type: ignore[no-untyped-call]
            self._channel
        )
        self._closed = False

    def observe(self, request: ObservationRequest) -> Iterator[StreamItem]:
        """按服务器写入顺序产出事件；deadline 到期时 grpcio 会抛出 RpcError。"""

        if self._closed:
            raise RuntimeError("perception client is already closed")
        # timeout_ms 同时进入协议和 gRPC deadline，避免任一端无限等待。
        call = self._stub.Observe(
            request_to_proto(request),
            timeout=request.timeout_ms / 1_000,
            wait_for_ready=True,
        )
        for event in call:
            yield stream_item_from_proto(event)

    def cancel(self, request_id: str, reason: str) -> bool:
        """通过独立 RPC 请求停止活动观察，并给取消操作本身设置短 deadline。"""

        if self._closed:
            raise RuntimeError("perception client is already closed")
        if not request_id or not reason:
            raise ValueError("cancel requires request_id and reason")
        response = self._stub.Cancel(
            realsight_pb2.CancelObservationRequest(
                request_id=request_id,
                reason=reason,
            ),
            timeout=2.0,
            wait_for_ready=True,
        )
        return bool(response.accepted)

    def close(self) -> None:
        """幂等关闭 channel，释放后台网络资源。"""

        if not self._closed:
            self._channel.close()
            self._closed = True

    def __enter__(self) -> PerceptionClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "PerceptionClient",
    "PerceptionFailure",
    "PerceptionProgress",
    "StreamItem",
    "request_to_proto",
    "stream_item_from_proto",
]
