"""
第 10 章最小 Python gRPC 客户端 Demo。

文件整体逻辑
------------
本 Demo 构造一个“读取充电器背面标签”的正式 ObservationRequest，连接已经启动的
C++ 感知服务，按顺序打印进度、失败和最终 Observation。它不读取图片、不调用大模型，
目的只是让你看见第 3 章模拟事件如何变成真实的跨进程 server stream。

使用的技术栈
------------
- Python 3.12 argparse/json/pathlib。
- RealSight Pydantic 领域模型。
- grpcio + 生成 stub，由 ``PerceptionClient`` 封装 deadline 和显式适配。

调用流程
--------
命令行 ``--address``
    -> build_request()
    -> PerceptionClient.observe()
    -> C++ request_accepted/source_opened/scanning/keyframe_selected
    -> 最终 Observation
    -> 逐行 JSON 输出并验证 image_path 存在。

边界
----
C++ 服务必须先启动。这个 Demo 只验证传输与质量关键帧，不把 local_features 当作 OCR
文本，也不生成 Evidence；这些属于第 11 章。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import grpc
from realsight.contracts import Observation, ObservationRequest, ViewType
from realsight.perception import (
    PerceptionClient,
    PerceptionFailure,
    PerceptionProgress,
)


def build_request(timeout_ms: int) -> ObservationRequest:
    """创建与前几章 ID/字段语义一致的背面标签观察请求。"""

    return ObservationRequest(
        request_id="request-ch10-demo",
        session_id="session-ch10-demo",
        target_id="charger-ch10-demo",
        view_type=ViewType.BACK_LABEL,
        required_features=(
            "charger_label",
            "charger_max_power_w",
            "charger_protocol",
        ),
        instruction="拍摄充电器背面标签并保持画面清晰",
        reason="后续需要从关键帧提取 USB-C 功率与协议证据",
        timeout_ms=timeout_ms,
        correlation_id="chapter-10-demo",
    )


def item_to_json(item: PerceptionProgress | PerceptionFailure | Observation) -> str:
    """把三种流项目转成便于观察的单行 JSON，不改变领域对象。"""

    if isinstance(item, Observation):
        payload: dict[str, Any] = item.model_dump(mode="json")
        payload["kind"] = "observation"
    else:
        payload = asdict(item)
        payload["occurred_at"] = item.occurred_at.isoformat()
        payload["kind"] = (
            "progress" if isinstance(item, PerceptionProgress) else "failure"
        )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    """连接服务并消费完整事件流；最终关键帧路径不存在时返回非零。"""

    parser = argparse.ArgumentParser(description="RealSight Chapter 10 gRPC demo")
    parser.add_argument("--address", default="127.0.0.1:50051")
    parser.add_argument("--timeout-ms", type=int, default=5_000)
    parser.add_argument(
        "--cancel-after-source-opened",
        action="store_true",
        help="教学验证：收到 source_opened 后调用独立 Cancel RPC",
    )
    args = parser.parse_args(argv)

    final_observation: Observation | None = None
    cancel_accepted = False
    try:
        with PerceptionClient(args.address) as client:
            for item in client.observe(build_request(args.timeout_ms)):
                print(item_to_json(item))
                if (
                    args.cancel_after_source_opened
                    and isinstance(item, PerceptionProgress)
                    and item.stage == "source_opened"
                ):
                    cancel_accepted = client.cancel(
                        "request-ch10-demo", "chapter 10 cancellation demonstration"
                    )
                    print(
                        json.dumps(
                            {
                                "kind": "cancel_requested",
                                "accepted": cancel_accepted,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                if isinstance(item, Observation):
                    final_observation = item
    except grpc.RpcError as error:
        if (
            args.cancel_after_source_opened
            and cancel_accepted
            and error.code() is grpc.StatusCode.CANCELLED
        ):
            print("CANCEL_DEMO_OK code=CANCELLED server_stop=verified")
            return 0
        raise

    if args.cancel_after_source_opened:
        print("取消请求没有以 CANCELLED 状态结束观察")
        return 1

    if final_observation is None:
        print("没有收到最终 Observation")
        return 1
    if final_observation.image_path is None:
        print("Observation 没有关键帧工件路径")
        return 1
    image_path = Path(final_observation.image_path)
    if not image_path.is_file():
        print("Observation 的关键帧工件不存在")
        return 1
    # 不额外安装 Python OpenCV；标准 JPEG SOI 文件头足以排除空文件和错误文本文件。
    if image_path.stat().st_size < 4 or image_path.read_bytes()[:2] != b"\xff\xd8":
        print("Observation 的关键帧不是有效 JPEG 文件")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
