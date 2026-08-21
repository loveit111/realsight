"""
RealSight 感知适配包。

文件整体逻辑
------------
该包连接 Python 业务模型与 C++ 感知服务。目前第 10 章提供 gRPC 客户端和显式协议
适配；第 11 章会在 Observation 之上增加视觉证据提取，但不会反向修改 C++ 采集职责。

使用的技术栈：Python 3.12、Pydantic 领域模型、grpcio/Protobuf。
调用流程：工作流 -> PerceptionClient -> C++ -> Observation -> 后续视觉证据层。
边界：本包不保存 LangGraph checkpoint，也不执行 USB-C 兼容规则。
"""

from .grpc_client import (
    PerceptionClient,
    PerceptionFailure,
    PerceptionProgress,
    StreamItem,
)

__all__ = [
    "PerceptionClient",
    "PerceptionFailure",
    "PerceptionProgress",
    "StreamItem",
]
