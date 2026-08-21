"""
自动生成的 Protobuf 消息代码。

文件整体逻辑：由 realsight.proto 生成 Python 消息、枚举和 descriptor。
使用的技术栈：Protocol Buffers Python runtime；内容由 protoc 维护。
调用流程：手写 grpc_client 构造/读取消息 -> 本模块序列化或解析 wire bytes。
阅读边界：不要手改本文件的 descriptor；修改 contracts/realsight.proto 后重新生成。
"""

from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ViewType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VIEW_TYPE_UNSPECIFIED: _ClassVar[ViewType]
    VIEW_TYPE_FULL_OBJECT: _ClassVar[ViewType]
    VIEW_TYPE_FRONT_LABEL: _ClassVar[ViewType]
    VIEW_TYPE_BACK_LABEL: _ClassVar[ViewType]
    VIEW_TYPE_PORT_CLOSEUP: _ClassVar[ViewType]

class ObservationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OBSERVATION_STATUS_UNSPECIFIED: _ClassVar[ObservationStatus]
    OBSERVATION_STATUS_ACCEPTED: _ClassVar[ObservationStatus]
    OBSERVATION_STATUS_REJECTED: _ClassVar[ObservationStatus]
    OBSERVATION_STATUS_FAILED: _ClassVar[ObservationStatus]
VIEW_TYPE_UNSPECIFIED: ViewType
VIEW_TYPE_FULL_OBJECT: ViewType
VIEW_TYPE_FRONT_LABEL: ViewType
VIEW_TYPE_BACK_LABEL: ViewType
VIEW_TYPE_PORT_CLOSEUP: ViewType
OBSERVATION_STATUS_UNSPECIFIED: ObservationStatus
OBSERVATION_STATUS_ACCEPTED: ObservationStatus
OBSERVATION_STATUS_REJECTED: ObservationStatus
OBSERVATION_STATUS_FAILED: ObservationStatus

class ObservationRequest(_message.Message):
    __slots__ = ("schema_version", "request_id", "session_id", "target_id", "view_type", "required_features", "instruction", "reason", "timeout_ms", "correlation_id")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    VIEW_TYPE_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FEATURES_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    CORRELATION_ID_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    request_id: str
    session_id: str
    target_id: str
    view_type: ViewType
    required_features: _containers.RepeatedScalarFieldContainer[str]
    instruction: str
    reason: str
    timeout_ms: int
    correlation_id: str
    def __init__(self, schema_version: _Optional[int] = ..., request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., target_id: _Optional[str] = ..., view_type: _Optional[_Union[ViewType, str]] = ..., required_features: _Optional[_Iterable[str]] = ..., instruction: _Optional[str] = ..., reason: _Optional[str] = ..., timeout_ms: _Optional[int] = ..., correlation_id: _Optional[str] = ...) -> None: ...

class QualitySignals(_message.Message):
    __slots__ = ("overall_score", "sharpness", "exposure", "glare", "target_ratio")
    OVERALL_SCORE_FIELD_NUMBER: _ClassVar[int]
    SHARPNESS_FIELD_NUMBER: _ClassVar[int]
    EXPOSURE_FIELD_NUMBER: _ClassVar[int]
    GLARE_FIELD_NUMBER: _ClassVar[int]
    TARGET_RATIO_FIELD_NUMBER: _ClassVar[int]
    overall_score: float
    sharpness: float
    exposure: float
    glare: float
    target_ratio: float
    def __init__(self, overall_score: _Optional[float] = ..., sharpness: _Optional[float] = ..., exposure: _Optional[float] = ..., glare: _Optional[float] = ..., target_ratio: _Optional[float] = ...) -> None: ...

class Observation(_message.Message):
    __slots__ = ("schema_version", "observation_id", "request_id", "target_id", "view_type", "captured_at_unix_ms", "quality", "local_features", "image_path", "status", "failure_reason", "source_frame_sequence", "source_position_ms")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    VIEW_TYPE_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FIELD_NUMBER: _ClassVar[int]
    LOCAL_FEATURES_FIELD_NUMBER: _ClassVar[int]
    IMAGE_PATH_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FRAME_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_POSITION_MS_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    observation_id: str
    request_id: str
    target_id: str
    view_type: ViewType
    captured_at_unix_ms: int
    quality: QualitySignals
    local_features: _containers.RepeatedScalarFieldContainer[str]
    image_path: str
    status: ObservationStatus
    failure_reason: str
    source_frame_sequence: int
    source_position_ms: int
    def __init__(self, schema_version: _Optional[int] = ..., observation_id: _Optional[str] = ..., request_id: _Optional[str] = ..., target_id: _Optional[str] = ..., view_type: _Optional[_Union[ViewType, str]] = ..., captured_at_unix_ms: _Optional[int] = ..., quality: _Optional[_Union[QualitySignals, _Mapping]] = ..., local_features: _Optional[_Iterable[str]] = ..., image_path: _Optional[str] = ..., status: _Optional[_Union[ObservationStatus, str]] = ..., failure_reason: _Optional[str] = ..., source_frame_sequence: _Optional[int] = ..., source_position_ms: _Optional[int] = ...) -> None: ...

class ObservationProgress(_message.Message):
    __slots__ = ("request_id", "target_id", "stage", "progress_percent", "message")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_PERCENT_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    target_id: str
    stage: str
    progress_percent: int
    message: str
    def __init__(self, request_id: _Optional[str] = ..., target_id: _Optional[str] = ..., stage: _Optional[str] = ..., progress_percent: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class ObservationFailure(_message.Message):
    __slots__ = ("request_id", "target_id", "code", "message", "retryable")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    target_id: str
    code: str
    message: str
    retryable: bool
    def __init__(self, request_id: _Optional[str] = ..., target_id: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: _Optional[bool] = ...) -> None: ...

class ObservationEvent(_message.Message):
    __slots__ = ("schema_version", "event_id", "request_id", "sequence", "occurred_at_unix_ms", "progress", "observation", "failure")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_FIELD_NUMBER: _ClassVar[int]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    event_id: str
    request_id: str
    sequence: int
    occurred_at_unix_ms: int
    progress: ObservationProgress
    observation: Observation
    failure: ObservationFailure
    def __init__(self, schema_version: _Optional[int] = ..., event_id: _Optional[str] = ..., request_id: _Optional[str] = ..., sequence: _Optional[int] = ..., occurred_at_unix_ms: _Optional[int] = ..., progress: _Optional[_Union[ObservationProgress, _Mapping]] = ..., observation: _Optional[_Union[Observation, _Mapping]] = ..., failure: _Optional[_Union[ObservationFailure, _Mapping]] = ...) -> None: ...

class CancelObservationRequest(_message.Message):
    __slots__ = ("request_id", "reason")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    reason: str
    def __init__(self, request_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelObservationResponse(_message.Message):
    __slots__ = ("request_id", "accepted")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    accepted: bool
    def __init__(self, request_id: _Optional[str] = ..., accepted: _Optional[bool] = ...) -> None: ...
