"""
真实设备评测记录与指标计算。

本模块不运行摄像头、OCR 或工作流，只读取已经人工标注的 JSONL。每行保存输入工件、
C++ 质量信号、OCR、Evidence、最终规则结果和耗时。指标由同一份原始记录复算，防止
README 中出现无法追溯的准确率。示例数据只能验证格式，不能当作项目成绩。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, field_validator

from realsight.compatibility import CompatibilityVerdict
from realsight.contracts import Identifier, NonEmptyText, StrictContract, UnitFloat

SampleCondition = Literal[
    "normal",
    "tilted",
    "mild_glare",
    "strong_glare",
    "blurred",
    "underexposed",
    "partially_occluded",
    "conflicting_print",
]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
NonNegativeFloat = Annotated[StrictFloat, Field(ge=0.0)]
PowerWatts = Annotated[StrictFloat, Field(gt=0.0, le=240.0)]


class ArtifactRecord(StrictContract):
    """数据集中的原始视频或关键帧引用；仓库可以只保存相对路径和哈希。"""

    path: NonEmptyText
    kind: Literal["video", "keyframe"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class GroundTruth(StrictContract):
    """人工核对的标签与期望规则类别，不由待测系统自身生成。"""

    power_w: PowerWatts
    protocol: NonEmptyText
    verdict: CompatibilityVerdict


class PerceptionMeasurement(StrictContract):
    """C++ 感知链的质量信号与该次运行的队列统计。"""

    accepted: StrictBool
    overall_score: UnitFloat
    sharpness: UnitFloat | None = None
    exposure: UnitFloat | None = None
    glare: UnitFloat | None = None
    target_ratio: UnitFloat | None = None
    issues: tuple[NonEmptyText, ...] = ()
    produced_frames: NonNegativeInt
    consumed_frames: NonNegativeInt
    dropped_frames: NonNegativeInt

    @field_validator("issues")
    @classmethod
    def issues_are_unique(cls, issues: tuple[str, ...]) -> tuple[str, ...]:
        if len(issues) != len(set(issues)):
            raise ValueError("perception issues must be unique")
        return issues


class OcrMeasurement(StrictContract):
    """OCR 原始文本与聚合置信度；空文本是合法失败样本。"""

    raw_text: tuple[str, ...] = ()
    mean_confidence: UnitFloat | None = None
    provider: NonEmptyText
    model: NonEmptyText


class EvidenceMeasurement(StrictContract):
    """本次任务最终账本中形成和冲突的字段名。"""

    fields: frozenset[NonEmptyText] = frozenset()
    conflicting_fields: frozenset[NonEmptyText] = frozenset()


class PredictedOutcome(StrictContract):
    """系统实际抽取的字段和确定性规则 verdict；没有结果时整段为 null。"""

    power_w: PowerWatts | None = None
    protocol: NonEmptyText | None = None
    verdict: CompatibilityVerdict


class StageLatency(StrictContract):
    """单样本各阶段墙钟耗时，单位毫秒。"""

    capture_ms: NonNegativeFloat
    perception_ms: NonNegativeFloat
    ocr_ms: NonNegativeFloat
    workflow_ms: NonNegativeFloat
    end_to_end_ms: NonNegativeFloat


class DeviceSample(StrictContract):
    """一行 JSONL 的完整、可审计真实设备样本。"""

    schema_version: Literal[1] = 1
    sample_id: Identifier
    condition: SampleCondition
    artifact: ArtifactRecord
    truth: GroundTruth
    perception: PerceptionMeasurement
    ocr: OcrMeasurement
    evidence: EvidenceMeasurement
    outcome: PredictedOutcome | None
    latency: StageLatency
    notes: str = Field(default="", max_length=2_048)


class PercentileSummary(StrictContract):
    """一个阶段的样本数、p50 与 p95。"""

    count: NonNegativeInt
    p50_ms: NonNegativeFloat
    p95_ms: NonNegativeFloat


class FrameTotals(StrictContract):
    """整个数据集的运行时帧计数。"""

    produced: NonNegativeInt
    consumed: NonNegativeInt
    dropped: NonNegativeInt
    drop_rate: UnitFloat


class DeviceEvaluationReport(StrictContract):
    """可直接序列化进作品集的真实指标；所有比例都以 0～1 表示。"""

    schema_version: Literal[1] = 1
    sample_count: NonNegativeInt
    condition_counts: dict[str, NonNegativeInt]
    keyframe_acceptance_rate: UnitFloat
    power_field_accuracy: UnitFloat
    protocol_field_accuracy: UnitFloat
    end_to_end_verdict_accuracy: UnitFloat
    false_positive_count: NonNegativeInt
    latency: dict[str, PercentileSummary]
    frames: FrameTotals


def _percentile(values: list[float], fraction: float) -> float:
    """使用线性插值计算百分位；排序和算法固定，跨机器可复算。"""

    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rate(correct: int, total: int) -> float:
    return round(correct / total, 6)


def evaluate_samples(samples: tuple[DeviceSample, ...]) -> DeviceEvaluationReport:
    """计算准入指标；拒绝空数据集和重复 ID，不用占位值伪造结果。"""

    if not samples:
        raise ValueError("device evaluation requires at least one sample")
    sample_ids = [sample.sample_id for sample in samples]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("device evaluation sample IDs must be unique")

    total = len(samples)
    accepted = sum(sample.perception.accepted for sample in samples)
    power_correct = sum(
        sample.outcome is not None
        and sample.outcome.power_w is not None
        and abs(sample.outcome.power_w - sample.truth.power_w) <= 0.5
        for sample in samples
    )
    protocol_correct = sum(
        sample.outcome is not None
        and sample.outcome.protocol is not None
        and sample.outcome.protocol.casefold().strip()
        == sample.truth.protocol.casefold().strip()
        for sample in samples
    )
    verdict_correct = sum(
        sample.outcome is not None and sample.outcome.verdict is sample.truth.verdict
        for sample in samples
    )
    false_positives = sum(
        sample.outcome is not None
        and sample.outcome.verdict is CompatibilityVerdict.CONDITIONS_MET
        and sample.truth.verdict is not CompatibilityVerdict.CONDITIONS_MET
        for sample in samples
    )

    latency_fields = {
        "capture": "capture_ms",
        "perception": "perception_ms",
        "ocr": "ocr_ms",
        "workflow": "workflow_ms",
        "end_to_end": "end_to_end_ms",
    }
    latency = {}
    for name, field_name in latency_fields.items():
        values = [float(getattr(sample.latency, field_name)) for sample in samples]
        latency[name] = PercentileSummary(
            count=total,
            p50_ms=round(_percentile(values, 0.50), 3),
            p95_ms=round(_percentile(values, 0.95), 3),
        )

    produced = sum(sample.perception.produced_frames for sample in samples)
    consumed = sum(sample.perception.consumed_frames for sample in samples)
    dropped = sum(sample.perception.dropped_frames for sample in samples)
    return DeviceEvaluationReport(
        sample_count=total,
        condition_counts=dict(
            sorted(Counter(sample.condition for sample in samples).items())
        ),
        keyframe_acceptance_rate=_rate(accepted, total),
        power_field_accuracy=_rate(power_correct, total),
        protocol_field_accuracy=_rate(protocol_correct, total),
        end_to_end_verdict_accuracy=_rate(verdict_correct, total),
        false_positive_count=false_positives,
        latency=latency,
        frames=FrameTotals(
            produced=produced,
            consumed=consumed,
            dropped=dropped,
            drop_rate=_rate(dropped, produced) if produced else 0.0,
        ),
    )


def load_jsonl(path: Path) -> tuple[DeviceSample, ...]:
    """逐行校验数据集，在错误信息中保留行号。"""

    samples: list[DeviceSample] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read device dataset {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            samples.append(DeviceSample.model_validate_json(stripped))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid device dataset line {line_number}: {exc}"
            ) from exc
    result = tuple(samples)
    # 复用 evaluate 的空集和唯一性前置条件，但不重复计算报告。
    if not result:
        raise ValueError("device evaluation requires at least one sample")
    if len({sample.sample_id for sample in result}) != len(result):
        raise ValueError("device evaluation sample IDs must be unique")
    return result


def render_markdown(report: DeviceEvaluationReport) -> str:
    """生成适合复制到 README 的指标表，并明确样本数。"""

    def percentage(value: float) -> str:
        return f"{value * 100:.1f}%"

    lines = [
        f"# RealSight 真实设备评测（n={report.sample_count}）",
        "",
        "| 指标 | 实测值 |",
        "|---|---:|",
        f"| 关键帧接受率 | {percentage(report.keyframe_acceptance_rate)} |",
        f"| 功率字段准确率 | {percentage(report.power_field_accuracy)} |",
        f"| 协议字段准确率 | {percentage(report.protocol_field_accuracy)} |",
        f"| 端到端 verdict 准确率 | {percentage(report.end_to_end_verdict_accuracy)} |",
        f"| 错误正结论 | {report.false_positive_count} |",
        f"| produced / consumed / dropped | {report.frames.produced} / "
        f"{report.frames.consumed} / {report.frames.dropped} |",
        f"| 丢帧率 | {percentage(report.frames.drop_rate)} |",
        "",
        "| 阶段 | p50 (ms) | p95 (ms) |",
        "|---|---:|---:|",
    ]
    for name, summary in report.latency.items():
        lines.append(f"| {name} | {summary.p50_ms:.3f} | {summary.p95_ms:.3f} |")
    lines.extend(["", "> 指标由 JSONL 原始记录复算；示例清单不得作为项目成绩。", ""])
    return "\n".join(lines)


__all__ = [
    "ArtifactRecord",
    "DeviceEvaluationReport",
    "DeviceSample",
    "EvidenceMeasurement",
    "GroundTruth",
    "OcrMeasurement",
    "PerceptionMeasurement",
    "PredictedOutcome",
    "StageLatency",
    "evaluate_samples",
    "load_jsonl",
    "render_markdown",
]
