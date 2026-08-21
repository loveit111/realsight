"""
第 11 章最小 Demo：把一张背面标签 Observation 转为可审计的 USB-C 视觉 Evidence。

文件整体逻辑
------------
本 Demo 先创建一个真实存在的极小 PPM 图片工件和一个 accepted ``Observation``。为了
不在第 11 章提前安装大型 OCR 或调用云端模型，``ScriptedTextRecognizer`` 固定返回一份
模拟的 OCR 文字区域；随后真正执行的部分是 ``VisionEvidenceAgent``：验证输入、合并
质量与 OCR 置信度、计算 20V x 3.25A = 65W、归一化 USB PD/PPS，并产出带来源元数据的
Evidence。最后打印 JSON，并用退出码验证核心字段。

使用的技术栈
------------
- Python 3.12 标准库：``tempfile`` 创建自动清理的教学工件，``json`` 打印结果。
- Pydantic v2 领域模型：Observation、Evidence 及第 11 章视觉中间类型。
- 第 11 章 TextRecognizer Protocol：脚本化替身与未来真实 OCR/VLM 共享同一个接口。

调用流程
--------
命令行
    -> create_fixture_image() 创建最小图片工件
    -> build_observation() 创建第 10 章风格的 accepted Observation
    -> ScriptedTextRecognizer 返回 OCR 文字区域
    -> VisionEvidenceAgent.extract()
    -> Evidence / EvidenceGap JSON
    -> 检查功率、USB PD 与来源元数据，打印 CH11_DEMO_OK。

边界
----
脚本化识别器没有读取图片像素，所以它不是 OCR 准确率验证；它只让你在没有模型、网络和
GPU 的条件下练习“关键帧 -> 原文区域 -> 结构化证据”的完整控制流。真实 OCR/VLM 只需
实现 TextRecognizer，不应绕过 VisionEvidenceAgent 直接写入 Evidence 或兼容性结论。
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from realsight.contracts import Observation, QualitySignals, ViewType
from realsight.vision import (
    RecognitionDocument,
    TextRegion,
    VisionEvidenceAgent,
)


class ScriptedTextRecognizer:
    """教学替身：确认工件存在后返回固定 OCR 输出，故意不假装做了真实图像识别。"""

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """返回一组有位置和置信度的背面标签文字区域。"""

        if not image_path.is_file():
            raise RuntimeError("demo image artifact unexpectedly disappeared")
        return RecognitionDocument(
            observation_id=observation_id,
            regions=(
                TextRegion(
                    region_id="region-ch11-name",
                    text="Example USB-C Charger",
                    ocr_confidence=0.98,
                    left=0.10,
                    top=0.12,
                    right=0.88,
                    bottom=0.24,
                ),
                TextRegion(
                    region_id="region-ch11-output",
                    text="Output: 5V=3A 9V=3A 15V=3A 20V=3.25A",
                    ocr_confidence=0.96,
                    left=0.10,
                    top=0.34,
                    right=0.92,
                    bottom=0.51,
                ),
                TextRegion(
                    region_id="region-ch11-protocol",
                    text="65W Max  USB PD 3.0  PPS",
                    ocr_confidence=0.94,
                    left=0.10,
                    top=0.61,
                    right=0.90,
                    bottom=0.75,
                ),
            ),
        )


def create_fixture_image(directory: Path) -> Path:
    """创建有效但极小的 PPM 图片；仅用于验证 agent 不接受不存在的工件路径。"""

    image_path = directory / "back-label.ppm"
    image_path.write_text(
        "P3\n2 2\n255\n255 255 255  0 0 0\n0 0 0  255 255 255\n",
        encoding="ascii",
    )
    return image_path


def build_observation(image_path: Path) -> Observation:
    """构造与第 10 章 gRPC adapter 输出字段一致的 accepted 背面标签观察。"""

    return Observation(
        observation_id="observation-ch11-demo",
        request_id="request-ch11-demo",
        target_id="charger-ch11-demo",
        view_type=ViewType.BACK_LABEL,
        captured_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        quality=QualitySignals(
            overall_score=0.92,
            sharpness=0.95,
            exposure=0.90,
            glare=0.89,
        ),
        image_path=str(image_path),
        source_frame_sequence=17,
        source_position_ms=680,
    )


def main() -> int:
    """执行无外部依赖的视觉证据链，并在关键字段不正确时返回非零。"""

    with tempfile.TemporaryDirectory(prefix="realsight-ch11-") as temporary_directory:
        image_path = create_fixture_image(Path(temporary_directory))
        observation = build_observation(image_path)
        agent = VisionEvidenceAgent(ScriptedTextRecognizer())
        result = agent.extract(
            observation,
            (
                "charger_label",
                "charger_max_power_w",
                "charger_protocol",
            ),
        )

    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    evidence_by_field = {item.field: item for item in result.evidence}
    power = evidence_by_field.get("charger_max_power_w")
    protocols = evidence_by_field.get("charger_protocol")
    if power is None or power.value != 65.0:
        print("第 11 章 Demo 未得到 65W 功率证据")
        return 1
    if protocols is None or protocols.value != ["usb_pd"]:
        print("第 11 章 Demo 未得到 USB PD 协议证据")
        return 1
    if power.source_id != observation.observation_id or not power.source_metadata:
        print("第 11 章 Demo 丢失 Observation 来源或 OCR 审计元数据")
        return 1
    if result.gaps:
        print("第 11 章 Demo 出现了不应存在的证据缺口")
        return 1
    print("CH11_DEMO_OK fields=charger_label,charger_max_power_w,charger_protocol")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
