"""
第 13、14 章的感知回放适配器与教学识别器。

文件逻辑
--------
真实 C++ 运行时会通过第 10 章 PerceptionClient 产生 Observation；为了让初学者
无需摄像头也能跑完整流程，本文件提供 ReplayObservationProvider。它只返回一条
已准备好的 Observation，随后仍由第 11 章 VisionEvidenceAgent 生成 Evidence。
GrpcObservationProvider 则把同一观察端口接到 C++ gRPC 流。

技术栈
------
- Python 3.12 Protocol、dataclass、pathlib；
- 第 10 章 grpcio PerceptionClient；
- 第 11 章 TextRecognizer/RecognitionDocument；
- 第 4 章 ObservationRequest 与 Observation 契约。

调用流程
--------
TaskService 看到 waiting_observation
    -> ObservationProvider.observe(request)
    -> ReplayObservationProvider 或 GrpcObservationProvider
    -> ObservationResume
    -> VisionEvidenceAgent.extract()。

边界
----
回放器不读取图像像素，脚本识别器也不等于真实 OCR。它们只用于可重复教学和 API
验收；生产环境需要替换为真实 C++ 服务和 OCR/VLM 适配器。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from realsight.contracts import (
    Observation,
    ObservationRequest,
    ObservationStatus,
    QualitySignals,
)
from realsight.perception import PerceptionClient, PerceptionFailure
from realsight.vision import RecognitionDocument, TextRegion


class ObservationProvider(Protocol):
    """主工作流对感知端的最小依赖：一条请求换回一个合格 Observation。"""

    def observe(self, request: ObservationRequest) -> Observation:
        """执行有限 deadline 的观察，失败时抛出异常而非伪造 Evidence。"""

    def cancel(self, request_id: str, reason: str) -> bool:
        """尽力取消正在执行的观察；回放实现可安全地返回 False。"""

    def close(self) -> None:
        """幂等释放 provider 拥有的传输资源。"""


@dataclass(frozen=True, slots=True)
class ReplayObservationProvider:
    """把一张预录关键帧作为当前请求的 accepted Observation 返回。"""

    image_path: Path
    quality_score: float = 0.92

    def observe(self, request: ObservationRequest) -> Observation:
        """严格沿用请求 ID、目标和视角，避免回放数据掩盖契约错误。"""

        if not self.image_path.is_file():
            raise FileNotFoundError(f"replay image does not exist: {self.image_path}")
        return Observation(
            observation_id=f"{request.request_id}-replay-observation",
            request_id=request.request_id,
            target_id=request.target_id,
            view_type=request.view_type,
            captured_at=datetime.now(UTC),
            quality=QualitySignals(overall_score=self.quality_score),
            local_features=request.required_features,
            image_path=str(self.image_path),
            status=ObservationStatus.ACCEPTED,
        )

    def cancel(self, request_id: str, reason: str) -> bool:
        """回放没有后台采集任务，所以取消只表示没有待停止的外部调用。"""

        return False

    def close(self) -> None:
        """回放器不持有连接或后台线程。"""


@dataclass(slots=True)
class GrpcObservationProvider:
    """复用第 10 章客户端，将 C++ server stream 中的最终 Observation 交给主循环。"""

    client: PerceptionClient

    def observe(self, request: ObservationRequest) -> Observation:
        """消费进度流，返回第一条 accepted Observation；失败事件保留为显式异常。"""

        for item in self.client.observe(request):
            if isinstance(item, PerceptionFailure):
                raise RuntimeError(
                    f"C++ perception failed: {item.code}: {item.message}"
                )
            if (
                isinstance(item, Observation)
                and item.status is ObservationStatus.ACCEPTED
            ):
                return item
        raise RuntimeError(
            "C++ perception stream ended without an accepted observation"
        )

    def cancel(self, request_id: str, reason: str) -> bool:
        """将 API 取消请求转发给第 10 章的 Cancel RPC。"""

        return self.client.cancel(request_id, reason)

    def close(self) -> None:
        """关闭底层 gRPC channel。"""

        self.client.close()


class ScriptedLabelRecognizer:
    """教学 OCR 替身：确认 artifact 存在后返回可审计的固定背面标签文字区域。"""

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """返回 65W、USB PD 和输出档位，实际字段解析仍由 VisionEvidenceAgent 完成。"""

        if not image_path.is_file():
            raise FileNotFoundError(f"recognizer artifact does not exist: {image_path}")
        return RecognitionDocument(
            observation_id=observation_id,
            regions=(
                TextRegion(
                    region_id="replay-label-name",
                    text="Example USB-C Charger",
                    ocr_confidence=0.98,
                    left=0.10,
                    top=0.10,
                    right=0.90,
                    bottom=0.25,
                ),
                TextRegion(
                    region_id="replay-label-output",
                    text="Output: 5V=3A 9V=3A 20V=3.25A",
                    ocr_confidence=0.96,
                    left=0.10,
                    top=0.35,
                    right=0.90,
                    bottom=0.52,
                ),
                TextRegion(
                    region_id="replay-label-protocol",
                    text="65W Max USB PD 3.0 PPS",
                    ocr_confidence=0.95,
                    left=0.10,
                    top=0.62,
                    right=0.90,
                    bottom=0.78,
                ),
            ),
        )


class UnavailableTextRecognizer:
    """生产骨架的诚实默认值：没有配置 OCR 时返回明确失败，而非猜测标签内容。"""

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """提醒部署者注入真实 OCR/VLM 适配器。"""

        from realsight.vision import RecognitionFailure

        raise RecognitionFailure("no OCR/VLM TextRecognizer has been configured")


__all__ = [
    "GrpcObservationProvider",
    "ObservationProvider",
    "ReplayObservationProvider",
    "ScriptedLabelRecognizer",
    "UnavailableTextRecognizer",
]
