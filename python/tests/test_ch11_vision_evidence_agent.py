"""
第 11 章视觉证据子 Agent 的确定性单元测试。

文件整体逻辑
------------
测试用很小的可控 ``TextRecognizer`` 替身模拟 OCR 返回值，集中验证视觉层的工程语义：
有效标签能产生可追溯 Evidence；功率矛盾、未知协议和低 OCR 置信度会留下明确 gap；
被 C++ 拒绝的 Observation 不应浪费一次 OCR 调用。它不测试真实 OCR 引擎准确率，也不
启动 gRPC 或摄像头。

使用的技术栈
------------
- pytest 9：普通断言与 ``tmp_path`` 隔离图片工件。
- Pydantic v2：真实构造 Observation、Evidence 与视觉中间契约。
- Python 3.12：小型记录型识别器，证明 Agent 的端口调用行为。

调用流程
--------
pytest -> make_observation() / make_document()
    -> RecordingRecognizer
    -> VisionEvidenceAgent.extract()
    -> 断言 Evidence、EvidenceGap、来源与调用次数。

边界
----
本文件的 OCR 文本是测试夹具，不能代表真实充电器标签的识别效果。真实图片、gRPC 流与
C++ 关键帧选择分别由第 9、10 章测试和 Demo 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from realsight.contracts import (
    Observation,
    ObservationStatus,
    QualitySignals,
    ViewType,
)
from realsight.vision import (
    RecognitionDocument,
    TextRegion,
    VisionEvidenceAgent,
)


class RecordingRecognizer:
    """测试替身：记录 agent 是否调用它，并返回预先固定的识别文档。"""

    def __init__(self, regions: tuple[TextRegion, ...]) -> None:
        self._regions = regions
        self.calls = 0

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """测试中图片必须存在，避免不小心绕过 artifact 入口检查。"""

        assert image_path.is_file()
        self.calls += 1
        return RecognitionDocument(observation_id=observation_id, regions=self._regions)


def make_region(
    region_id: str,
    text: str,
    *,
    confidence: float = 0.96,
) -> TextRegion:
    """构造一个位置固定、只改变文本和 OCR 置信度的文字区域。"""

    return TextRegion(
        region_id=region_id,
        text=text,
        ocr_confidence=confidence,
        left=0.10,
        top=0.10,
        right=0.90,
        bottom=0.25,
    )


def make_observation(
    image_path: Path,
    *,
    status: ObservationStatus = ObservationStatus.ACCEPTED,
    quality: float = 0.92,
) -> Observation:
    """构造第 10 章类型一致的 Observation；拒绝状态必须附带原因。"""

    return Observation(
        observation_id="observation-ch11-test",
        request_id="request-ch11-test",
        target_id="charger-ch11-test",
        view_type=ViewType.BACK_LABEL,
        captured_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        quality=QualitySignals(overall_score=quality),
        image_path=str(image_path),
        status=status,
        failure_reason="underexposed"
        if status is not ObservationStatus.ACCEPTED
        else None,
    )


def image_artifact(tmp_path: Path) -> Path:
    """写入一个最小工件文件；视觉层只验证路径，具体解码属于识别后端。"""

    path = tmp_path / "back-label.ppm"
    path.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    return path


def test_valid_label_creates_auditable_power_and_protocol_evidence(
    tmp_path: Path,
) -> None:
    """65W 与 20V x 3.25A 互相印证，USB PD/PPS 进入证据及元数据。"""

    recognizer = RecordingRecognizer(
        (
            make_region("region-name", "Example USB-C Charger"),
            make_region("region-output", "Output: 5V=3A 9V=3A 20V=3.25A"),
            make_region("region-protocol", "65W Max USB PD 3.0 PPS"),
        )
    )
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path)),
        ("charger_label", "charger_max_power_w", "charger_protocol"),
    )

    evidence = {item.field: item for item in result.evidence}
    assert recognizer.calls == 1
    assert result.gaps == ()
    assert evidence["charger_max_power_w"].value == 65.0
    assert evidence["charger_protocol"].value == ["usb_pd"]
    assert evidence["charger_protocol"].source_metadata["extensions"] == ["pps"]
    assert evidence["charger_max_power_w"].source_id == "observation-ch11-test"
    assert evidence["charger_max_power_w"].source_metadata["regions"]
    assert evidence["charger_max_power_w"].confidence == 0.92


def test_conflicting_direct_and_output_power_becomes_gap(tmp_path: Path) -> None:
    """额定 65W 与 20V x 2A=40W 不一致时，agent 不擅自选择较大的数字。"""

    recognizer = RecordingRecognizer(
        (make_region("region-power", "65W Max Output: 20V=2A"),)
    )
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path)), ("charger_max_power_w",)
    )

    assert result.evidence == ()
    assert [gap.code for gap in result.gaps] == ["conflicting_power_candidates"]


def test_unknown_protocol_becomes_gap_instead_of_false_negative(tmp_path: Path) -> None:
    """未见协议标记只表示当前观察未知，绝不能推断为“不支持 PD”。"""

    recognizer = RecordingRecognizer((make_region("region-text", "Output: 20V=3A"),))
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path)), ("charger_protocol",)
    )

    assert result.evidence == ()
    assert [gap.code for gap in result.gaps] == ["protocol_not_found"]


def test_low_ocr_confidence_becomes_gap(tmp_path: Path) -> None:
    """清晰图片中的低置信文字仍不能作为字段证据，质量与识别置信度不可互相替代。"""

    recognizer = RecordingRecognizer(
        (make_region("region-power", "65W", confidence=0.50),)
    )
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path), quality=0.95),
        ("charger_max_power_w",),
    )

    assert result.evidence == ()
    assert [gap.code for gap in result.gaps] == ["recognition_confidence_too_low"]


def test_rejected_observation_does_not_call_recognizer(tmp_path: Path) -> None:
    """第 10 章已拒绝的关键帧直接变成可重试 gap，避免将资源浪费在无效工件上。"""

    recognizer = RecordingRecognizer((make_region("region-power", "65W"),))
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path), status=ObservationStatus.REJECTED),
        ("charger_max_power_w",),
    )

    assert recognizer.calls == 0
    assert result.evidence == ()
    assert [gap.code for gap in result.gaps] == ["observation_not_accepted"]


def test_unsupported_feature_is_not_silently_dropped(tmp_path: Path) -> None:
    """视觉子 Agent 当前不提取笔记本规格，必须把边界变成清晰 gap。"""

    recognizer = RecordingRecognizer((make_region("region-label", "65W"),))
    result = VisionEvidenceAgent(recognizer).extract(
        make_observation(image_artifact(tmp_path)), ("laptop_model",)
    )

    assert recognizer.calls == 0
    assert result.evidence == ()
    assert [gap.code for gap in result.gaps] == ["unsupported_visual_feature"]
