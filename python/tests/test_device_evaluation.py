"""真实设备评测记录的格式、指标和报告测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from realsight.compatibility import CompatibilityVerdict
from realsight.evaluation.device_dataset import (
    ArtifactRecord,
    DeviceSample,
    EvidenceMeasurement,
    GroundTruth,
    OcrMeasurement,
    PerceptionMeasurement,
    PredictedOutcome,
    StageLatency,
    evaluate_samples,
    load_jsonl,
    render_markdown,
)


def sample(
    sample_id: str,
    *,
    condition: str,
    accepted: bool,
    truth_verdict: CompatibilityVerdict,
    outcome: PredictedOutcome | None,
    power_w: float = 65.0,
    latency_ms: float = 100.0,
) -> DeviceSample:
    return DeviceSample.model_validate(
        {
            "sample_id": sample_id,
            "condition": condition,
            "artifact": ArtifactRecord(
                path=f"artifacts/{sample_id}.jpg", kind="keyframe"
            ),
            "truth": GroundTruth(
                power_w=power_w,
                protocol="USB PD",
                verdict=truth_verdict,
            ),
            "perception": PerceptionMeasurement(
                accepted=accepted,
                overall_score=0.8,
                produced_frames=10,
                consumed_frames=8,
                dropped_frames=2,
            ),
            "ocr": OcrMeasurement(
                raw_text=("65W USB PD",) if accepted else (),
                mean_confidence=0.9 if accepted else None,
                provider="paddleocr",
                model="PP-OCRv6",
            ),
            "evidence": EvidenceMeasurement(
                fields=frozenset({"charger_max_power_w", "charger_protocol"})
                if accepted
                else frozenset()
            ),
            "outcome": outcome,
            "latency": StageLatency(
                capture_ms=latency_ms / 5,
                perception_ms=latency_ms / 5,
                ocr_ms=latency_ms / 5,
                workflow_ms=latency_ms / 5,
                end_to_end_ms=latency_ms,
            ),
        }
    )


def test_evaluation_reports_reproducible_rates_latency_and_false_positives() -> None:
    correct = PredictedOutcome(
        power_w=65.0,
        protocol="usb pd",
        verdict=CompatibilityVerdict.CONDITIONS_MET,
    )
    unsafe = PredictedOutcome(
        power_w=30.0,
        protocol="USB PD",
        verdict=CompatibilityVerdict.CONDITIONS_MET,
    )
    report = evaluate_samples(
        (
            sample(
                "sample-normal",
                condition="normal",
                accepted=True,
                truth_verdict=CompatibilityVerdict.CONDITIONS_MET,
                outcome=correct,
                latency_ms=100.0,
            ),
            sample(
                "sample-glare",
                condition="strong_glare",
                accepted=False,
                truth_verdict=CompatibilityVerdict.INSUFFICIENT_EVIDENCE,
                outcome=unsafe,
                power_w=65.0,
                latency_ms=300.0,
            ),
        )
    )

    assert report.sample_count == 2
    assert report.keyframe_acceptance_rate == 0.5
    assert report.power_field_accuracy == 0.5
    assert report.protocol_field_accuracy == 1.0
    assert report.end_to_end_verdict_accuracy == 0.5
    assert report.false_positive_count == 1
    assert report.latency["end_to_end"].p50_ms == 200.0
    assert report.latency["end_to_end"].p95_ms == 290.0
    assert report.frames.drop_rate == 0.2
    assert "错误正结论 | 1" in render_markdown(report)


def test_jsonl_loader_rejects_duplicate_sample_ids(tmp_path: Path) -> None:
    item = sample(
        "duplicate",
        condition="normal",
        accepted=False,
        truth_verdict=CompatibilityVerdict.INSUFFICIENT_EVIDENCE,
        outcome=None,
    )
    path = tmp_path / "samples.jsonl"
    line = item.model_dump_json()
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_jsonl(path)
