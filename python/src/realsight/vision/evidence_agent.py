"""
第 11 章视觉证据子 Agent：从一张合格关键帧的 OCR 文本生成可追溯 Evidence。

文件整体逻辑
------------
第 10 章输出的 ``Observation`` 只说明 C++ 已经选出一张值得处理的图片，并没有说明
图片里的文字是什么。本模块先检查 Observation 状态、视角、质量和图片工件，再通过
``TextRecognizer`` 接收 OCR 或多模态模型返回的文字区域。随后只为本课程 USB-C MVP
支持的字段执行确定性归一化：完整标签文本、最大输出功率和协议集合。

每条 Evidence 都保留来源 Observation ID、原始 OCR 文字、相对文字框、识别置信度与
归一化方法。无法读出、低置信度、功率候选冲突和不支持的字段会变成 ``EvidenceGap``，
而不是被猜成一个看似确定的数值。调用者将在第 13 章把 gap 转成下一次观察或询问用户
的 Action。

使用的技术栈
------------
- Python 3.12：``Protocol`` 定义 OCR/VLM 可替换端口；``re`` 处理范围受限的标签格式；
  ``hashlib`` 生成稳定、短小的 Evidence ID。
- Pydantic v2：复用第 4 章的 Observation/Evidence 契约，并校验视觉中间结果。
- 不依赖 OCR、OpenCV 或大模型包：教学 Demo 注入脚本化识别器，真实部署只需实现
  ``TextRecognizer``，不需要修改证据归一化与审计逻辑。

调用流程
--------
第 10 章 gRPC adapter 得到 accepted Observation
    -> VisionEvidenceAgent.extract(observation, required_features)
    -> 验证状态、视角、质量、image_path
    -> TextRecognizer.recognize(image_path, observation_id)
    -> RecognitionDocument（文字 + 相对位置 + OCR 置信度）
    -> 确定性归一化为 Evidence，或产生 EvidenceGap
    -> 第 12/13 章消费结果。

边界
----
本文件不自行调用摄像头、gRPC、数据库或模型 API；不把 OCR 置信度解释为“充电兼容概率”；
不把候选 Evidence 写入 BeliefState；不做笔记本规格检索或 USB-C 规则裁决。正则仅覆盖
本课程的受限 USB-C 标签格式，不能拿去声称支持任意商品标签或通用 OCR。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, JsonValue, field_validator, model_validator

from realsight.contracts import (
    Evidence,
    EvidenceStatus,
    FieldName,
    Identifier,
    NonEmptyText,
    Observation,
    ObservationStatus,
    StrictContract,
    UnitFloat,
    ViewType,
)


class TextRegion(StrictContract):
    """一段 OCR 文字及其在图片中的相对位置；坐标范围始终为 0 到 1。"""

    schema_version: Literal[1] = 1
    region_id: Identifier
    text: NonEmptyText
    ocr_confidence: UnitFloat
    left: UnitFloat
    top: UnitFloat
    right: UnitFloat
    bottom: UnitFloat

    @model_validator(mode="after")
    def bounds_are_non_empty(self) -> TextRegion:
        """文字框右下角必须在左上角之后，避免保存退化区域。"""

        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("text region must have positive relative width and height")
        return self


class RecognitionDocument(StrictContract):
    """一个识别后端针对一张 Observation 返回的完整文字文档。"""

    schema_version: Literal[1] = 1
    observation_id: Identifier
    regions: tuple[TextRegion, ...]
    provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("regions")
    @classmethod
    def region_ids_are_unique(
        cls, regions: tuple[TextRegion, ...]
    ) -> tuple[TextRegion, ...]:
        """空文档可以表示“没有可读文字”，但已返回的区域 ID 不能重复。"""

        region_ids = [region.region_id for region in regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("recognition document region IDs must be unique")
        return regions


class EvidenceGap(StrictContract):
    """视觉层发现的证据缺口，供后续规划器决定是否重新观察或询问用户。"""

    schema_version: Literal[1] = 1
    observation_id: Identifier
    target_id: Identifier
    field: FieldName
    code: NonEmptyText
    message: NonEmptyText
    suggested_view: ViewType | None = None


class VisionExtraction(StrictContract):
    """一次视觉处理的完整输出：原始识别文档、候选证据和未解决缺口。"""

    schema_version: Literal[1] = 1
    observation_id: Identifier
    target_id: Identifier
    document: RecognitionDocument | None = None
    evidence: tuple[Evidence, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()

    @model_validator(mode="after")
    def output_references_the_same_observation_and_target(self) -> VisionExtraction:
        """防止另一次观察的 OCR 文字或另一目标的 Evidence 被混入当前结果。"""

        if (
            self.document is not None
            and self.document.observation_id != self.observation_id
        ):
            raise ValueError("recognition document observation_id does not match")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("vision extraction evidence IDs must be unique")
        for item in self.evidence:
            if item.target_id != self.target_id:
                raise ValueError("vision evidence target_id does not match")
            if (
                item.source_type != "vision_ocr"
                or item.source_id != self.observation_id
            ):
                raise ValueError("vision evidence must point to its source observation")
        for gap in self.gaps:
            if (
                gap.observation_id != self.observation_id
                or gap.target_id != self.target_id
            ):
                raise ValueError("vision gap does not match extraction identity")
        return self


class RecognitionFailure(RuntimeError):
    """OCR/VLM 后端的可预期失败；调用者可把它转成可规划的证据缺口。"""


class TextRecognizer(Protocol):
    """可替换的文字识别端口；实现可以是本地 OCR、远程服务或多模态模型适配器。"""

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """读取一张已落盘图片，并返回属于指定 Observation 的文字区域。"""


@dataclass(frozen=True, slots=True)
class _PowerCandidate:
    """归一化过程内部使用的功率候选；不作为跨模块契约暴露。"""

    watts: float
    regions: tuple[TextRegion, ...]
    method: str


_SUPPORTED_FEATURES = frozenset(
    {
        "charger_label",
        "charger_max_power_w",
        "charger_protocol",
    }
)
_WATT_PATTERN = re.compile(r"(?<![A-Za-z0-9.])(\d{1,3}(?:\.\d{1,2})?)\s*W\b", re.I)
_OUTPUT_PROFILE_PATTERN = re.compile(
    r"(\d{1,3}(?:\.\d{1,2})?)\s*V\s*(?:=|x|\*|×)\s*"
    r"(\d{1,2}(?:\.\d{1,2})?)\s*A\b",
    re.I,
)
_PD_PATTERN = re.compile(r"\b(?:usb[-\s]?pd|power\s+delivery|pd\s*\d)", re.I)
_PPS_PATTERN = re.compile(r"\bpps\b", re.I)
_QC_PATTERN = re.compile(r"\b(?:quick\s*charge|qc\s*\d)", re.I)


class VisionEvidenceAgent:
    """把合格背标 Observation 转为候选 Evidence 的受限视觉子 Agent。"""

    def __init__(
        self,
        recognizer: TextRecognizer,
        *,
        min_observation_quality: float = 0.65,
        min_recognition_confidence: float = 0.70,
        confirmed_confidence: float = 0.85,
    ) -> None:
        """保存可替换识别器，并把三个阈值限制在有意义的单调区间。"""

        if not 0.0 <= min_observation_quality <= 1.0:
            raise ValueError("min_observation_quality must be between zero and one")
        if not 0.0 <= min_recognition_confidence <= 1.0:
            raise ValueError("min_recognition_confidence must be between zero and one")
        if not min_recognition_confidence <= confirmed_confidence <= 1.0:
            raise ValueError(
                "confirmed_confidence must be >= min_recognition_confidence and <= 1"
            )
        self._recognizer = recognizer
        self._min_observation_quality = min_observation_quality
        self._min_recognition_confidence = min_recognition_confidence
        self._confirmed_confidence = confirmed_confidence

    def extract(
        self,
        observation: Observation,
        required_features: tuple[str, ...],
    ) -> VisionExtraction:
        """验证输入、调用识别器，并将支持字段分别转成 Evidence 或 EvidenceGap。"""

        requested = tuple(required_features)
        initial_gaps = self._unsupported_feature_gaps(observation, requested)
        supported = tuple(field for field in requested if field in _SUPPORTED_FEATURES)

        # 全部字段都超出当前子 Agent 能力时，连图片工件和 OCR 服务也不该占用。
        if not supported:
            return VisionExtraction(
                observation_id=observation.observation_id,
                target_id=observation.target_id,
                gaps=initial_gaps,
            )

        # Observation 不是 accepted 时，C++ 已经给出“不适合解释”的结果，不能尝试 OCR。
        if observation.status is not ObservationStatus.ACCEPTED:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="observation_not_accepted",
                message="该 Observation 未被 C++ 质量链路接受，不能从中生成视觉证据。",
                gaps=initial_gaps,
            )
        if observation.view_type is not ViewType.BACK_LABEL:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="wrong_view_type",
                message="USB-C 充电标签证据需要 back_label 视角，请重新拍摄背面标签。",
                gaps=initial_gaps,
            )
        if observation.quality.overall_score < self._min_observation_quality:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="observation_quality_too_low",
                message="关键帧整体质量不足，先请求更清晰、低反光的背面标签视角。",
                gaps=initial_gaps,
            )
        if observation.image_path is None:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="image_artifact_missing",
                message="Observation 没有图片工件路径，无法启动文字识别。",
                gaps=initial_gaps,
            )

        image_path = Path(observation.image_path)
        if not image_path.is_file():
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="image_artifact_not_found",
                message="Observation 指向的图片工件不存在，不能把旧 OCR 结果挪用到本次观察。",
                gaps=initial_gaps,
            )

        try:
            document = self._recognizer.recognize(
                image_path, observation_id=observation.observation_id
            )
        except RecognitionFailure as exc:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="recognition_failed",
                message=f"文字识别服务未返回可用结果：{exc}",
                gaps=initial_gaps,
            )
        if document.observation_id != observation.observation_id:
            raise ValueError("recognizer returned a document for another observation")
        if not document.regions:
            return self._all_supported_fields_are_gaps(
                observation,
                supported,
                code="no_readable_text",
                message="图片中没有得到可读文字，请调整角度、对焦或补光后重试。",
                gaps=initial_gaps,
                document=document,
            )

        evidence: list[Evidence] = []
        gaps = list(initial_gaps)
        for field in supported:
            try:
                item, field_gap = self._extract_field(observation, document, field)
            except RecognitionFailure:
                # OCR 文本存在但支撑该字段的区域太不可靠时，不降级成一个看似可用的
                # probable 数值；把“再拍一次”交给后续规划器。
                item = None
                field_gap = self._gap(
                    observation,
                    field,
                    "recognition_confidence_too_low",
                    "相关文字区域的 OCR 置信度不足，不能生成该字段的证据。",
                )
            if item is not None:
                evidence.append(item)
            if field_gap is not None:
                gaps.append(field_gap)
        return VisionExtraction(
            observation_id=observation.observation_id,
            target_id=observation.target_id,
            document=document,
            evidence=tuple(evidence),
            gaps=tuple(gaps),
        )

    def _extract_field(
        self,
        observation: Observation,
        document: RecognitionDocument,
        field: str,
    ) -> tuple[Evidence | None, EvidenceGap | None]:
        """按字段选择最小归一化器，使每种失败原因独立可追踪。"""

        if field == "charger_label":
            return self._extract_label(observation, document), None
        if field == "charger_max_power_w":
            return self._extract_max_power(observation, document)
        if field == "charger_protocol":
            return self._extract_protocols(observation, document)
        # supported 的来源被集中定义；该分支仅作为未来修改时的防御检查。
        raise AssertionError(f"unhandled supported visual feature: {field}")

    def _extract_label(
        self,
        observation: Observation,
        document: RecognitionDocument,
    ) -> Evidence:
        """保留标签全文，不把品牌/型号猜测成独立事实。"""

        raw_text = "\n".join(region.text for region in document.regions)
        return self._build_evidence(
            observation,
            field="charger_label",
            value=raw_text,
            regions=document.regions,
            normalizer="label_text_join",
            extra_metadata={},
            provider_metadata=document.provider_metadata,
        )

    def _extract_max_power(
        self,
        observation: Observation,
        document: RecognitionDocument,
    ) -> tuple[Evidence | None, EvidenceGap | None]:
        """优先用输出电压/电流档位求最大值，并显式处理不一致的额定瓦数。"""

        direct_candidates: list[_PowerCandidate] = []
        profile_candidates: list[_PowerCandidate] = []
        for region in document.regions:
            for match in _WATT_PATTERN.finditer(region.text):
                watts = float(match.group(1))
                if 0.0 < watts <= 240.0:
                    direct_candidates.append(
                        _PowerCandidate(watts, (region,), "explicit_wattage")
                    )
            for match in _OUTPUT_PROFILE_PATTERN.finditer(region.text):
                volts = float(match.group(1))
                amps = float(match.group(2))
                watts = round(volts * amps, 2)
                if 0.0 < watts <= 240.0:
                    profile_candidates.append(
                        _PowerCandidate(watts, (region,), "voltage_current_product")
                    )

        profile_max = self._maximum_candidate(profile_candidates)
        direct_values = {candidate.watts for candidate in direct_candidates}
        direct_max = self._maximum_candidate(direct_candidates)
        if profile_max is not None and direct_max is not None:
            # 电源标签可能同时写“65W Max”和“20V=3.25A”。它们相同时是互相支持；
            # 不同时不能猜测哪一个属于目标端口，交给后续再次观察或资料交叉验证。
            if abs(profile_max.watts - direct_max.watts) > 0.01:
                return None, self._gap(
                    observation,
                    "charger_max_power_w",
                    "conflicting_power_candidates",
                    "额定瓦数与电压乘电流得到的最大输出功率不一致，不能自动选择其中一项。",
                )
            regions = self._unique_regions((*profile_max.regions, *direct_max.regions))
            return (
                self._build_evidence(
                    observation,
                    field="charger_max_power_w",
                    value=profile_max.watts,
                    regions=regions,
                    normalizer="matching_explicit_wattage_and_output_profile",
                    extra_metadata={
                        "direct_wattage_candidates": self._json_floats(direct_values),
                        "output_profile_wattage_candidates": self._json_floats(
                            {candidate.watts for candidate in profile_candidates}
                        ),
                    },
                    provider_metadata=document.provider_metadata,
                ),
                None,
            )
        if profile_max is not None:
            return (
                self._build_evidence(
                    observation,
                    field="charger_max_power_w",
                    value=profile_max.watts,
                    regions=profile_max.regions,
                    normalizer="maximum_output_voltage_current_product",
                    extra_metadata={
                        "output_profile_wattage_candidates": self._json_floats(
                            {candidate.watts for candidate in profile_candidates}
                        )
                    },
                    provider_metadata=document.provider_metadata,
                ),
                None,
            )
        if len(direct_values) == 1 and direct_max is not None:
            return (
                self._build_evidence(
                    observation,
                    field="charger_max_power_w",
                    value=direct_max.watts,
                    regions=direct_max.regions,
                    normalizer="single_explicit_wattage",
                    extra_metadata={},
                    provider_metadata=document.provider_metadata,
                ),
                None,
            )
        if len(direct_values) > 1:
            return None, self._gap(
                observation,
                "charger_max_power_w",
                "ambiguous_power_candidates",
                "标签出现多个瓦数且没有完整输出档位，无法确定哪一个是该充电器的最大输出。",
            )
        return None, self._gap(
            observation,
            "charger_max_power_w",
            "power_not_found",
            "未从标签文字中找到可验证的额定瓦数或电压/电流输出档位。",
        )

    def _extract_protocols(
        self,
        observation: Observation,
        document: RecognitionDocument,
    ) -> tuple[Evidence | None, EvidenceGap | None]:
        """识别协议集合；协议可以并存，因此 value 是有稳定顺序的 JSON 数组。"""

        matched_regions: list[TextRegion] = []
        protocols: list[str] = []
        extensions: list[str] = []
        for region in document.regions:
            text = region.text
            matched = False
            if _PD_PATTERN.search(text) or _PPS_PATTERN.search(text):
                protocols.append("usb_pd")
                matched = True
            if _PPS_PATTERN.search(text):
                extensions.append("pps")
                matched = True
            if _QC_PATTERN.search(text):
                protocols.append("quick_charge")
                matched = True
            if matched:
                matched_regions.append(region)
        normalized_protocols = self._ordered_unique(protocols)
        if not normalized_protocols:
            return None, self._gap(
                observation,
                "charger_protocol",
                "protocol_not_found",
                "未从标签文字中找到 USB PD、PPS 或 Quick Charge 协议标记。",
            )
        return (
            self._build_evidence(
                observation,
                field="charger_protocol",
                value=self._json_strings(normalized_protocols),
                regions=self._unique_regions(tuple(matched_regions)),
                normalizer="protocol_marker_set",
                extra_metadata={
                    "extensions": self._json_strings(self._ordered_unique(extensions))
                },
                provider_metadata=document.provider_metadata,
            ),
            None,
        )

    def _build_evidence(
        self,
        observation: Observation,
        *,
        field: str,
        value: JsonValue,
        regions: tuple[TextRegion, ...],
        normalizer: str,
        extra_metadata: dict[str, JsonValue],
        provider_metadata: dict[str, JsonValue],
    ) -> Evidence:
        """统一构造带文字框、原文和置信度来源的 Evidence。"""

        confidence = min(
            observation.quality.overall_score,
            *(region.ocr_confidence for region in regions),
        )
        if confidence < self._min_recognition_confidence:
            # 这个异常只用于防止内部调用者遗漏 gap 分支；公开 extract 会在下方字段路径
            # 先转换低置信度为 EvidenceGap。
            raise RecognitionFailure(
                "candidate confidence is below the configured threshold"
            )
        status = (
            EvidenceStatus.CONFIRMED
            if confidence >= self._confirmed_confidence
            else EvidenceStatus.PROBABLE
        )
        metadata = self._source_metadata(
            observation,
            regions,
            normalizer=normalizer,
            extra_metadata=extra_metadata,
            provider_metadata=provider_metadata,
        )
        digest_input = f"{observation.observation_id}|{field}|{value!r}".encode()
        evidence_id = "evidence-vision-" + hashlib.sha256(digest_input).hexdigest()[:16]
        return Evidence(
            evidence_id=evidence_id,
            target_id=observation.target_id,
            field=field,
            value=value,
            source_type="vision_ocr",
            source_id=observation.observation_id,
            confidence=confidence,
            status=status,
            derived_from=tuple(region.region_id for region in regions),
            source_metadata=metadata,
        )

    def _source_metadata(
        self,
        observation: Observation,
        regions: tuple[TextRegion, ...],
        *,
        normalizer: str,
        extra_metadata: dict[str, JsonValue],
        provider_metadata: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """把区域坐标与原文装入 JSON 元数据，证据账本无需再次读取图片也能审计。"""

        region_records: list[JsonValue] = []
        for region in regions:
            region_records.append(
                {
                    "region_id": region.region_id,
                    "raw_text": region.text,
                    "ocr_confidence": region.ocr_confidence,
                    "bounds": [region.left, region.top, region.right, region.bottom],
                }
            )
        metadata: dict[str, JsonValue] = {
            "observation_id": observation.observation_id,
            "observation_quality": observation.quality.overall_score,
            "normalizer": normalizer,
            "regions": region_records,
            "recognizer": dict(provider_metadata),
        }
        metadata.update(extra_metadata)
        return metadata

    def _all_supported_fields_are_gaps(
        self,
        observation: Observation,
        supported: tuple[str, ...],
        *,
        code: str,
        message: str,
        gaps: tuple[EvidenceGap, ...],
        document: RecognitionDocument | None = None,
    ) -> VisionExtraction:
        """当输入无法进入识别阶段时，为每个受支持请求字段留下同一可行动缺口。"""

        all_gaps = [*gaps]
        all_gaps.extend(
            self._gap(observation, field, code, message) for field in supported
        )
        return VisionExtraction(
            observation_id=observation.observation_id,
            target_id=observation.target_id,
            document=document,
            gaps=tuple(all_gaps),
        )

    def _unsupported_feature_gaps(
        self,
        observation: Observation,
        requested: tuple[str, ...],
    ) -> tuple[EvidenceGap, ...]:
        """显式报告当前课程未支持的字段，避免调用方误以为它被静默忽略。"""

        return tuple(
            self._gap(
                observation,
                field,
                "unsupported_visual_feature",
                "当前视觉子 Agent 只支持 charger_label、charger_max_power_w 和 charger_protocol。",
            )
            for field in requested
            if field not in _SUPPORTED_FEATURES
        )

    @staticmethod
    def _maximum_candidate(
        candidates: list[_PowerCandidate],
    ) -> _PowerCandidate | None:
        """从一个输出档位集合中选择最大值；相同最大值的区域在调用方合并。"""

        return max(candidates, key=lambda candidate: candidate.watts, default=None)

    @staticmethod
    def _unique_regions(regions: tuple[TextRegion, ...]) -> tuple[TextRegion, ...]:
        """按 region_id 去重并保留首次出现顺序，保证证据元数据的输出稳定。"""

        seen: set[str] = set()
        unique: list[TextRegion] = []
        for region in regions:
            if region.region_id not in seen:
                seen.add(region.region_id)
                unique.append(region)
        return tuple(unique)

    @staticmethod
    def _ordered_unique(values: list[str]) -> list[str]:
        """保留协议首次出现顺序，避免 set 导致 JSON 输出在不同运行中抖动。"""

        return list(dict.fromkeys(values))

    @staticmethod
    def _json_floats(values: set[float]) -> list[JsonValue]:
        """把数值候选显式写成 JsonValue 列表，既保证排序也满足严格静态类型检查。"""

        return [value for value in sorted(values)]

    @staticmethod
    def _json_strings(values: list[str]) -> list[JsonValue]:
        """把协议字符串显式提升为 JsonValue，避免 Python 不变 list 的类型歧义。"""

        return [value for value in values]

    @staticmethod
    def _gap(
        observation: Observation,
        field: str,
        code: str,
        message: str,
    ) -> EvidenceGap:
        """统一建立可追踪、可规划的背面标签证据缺口。"""

        return EvidenceGap(
            observation_id=observation.observation_id,
            target_id=observation.target_id,
            field=field,
            code=code,
            message=message,
            suggested_view=ViewType.BACK_LABEL,
        )


__all__ = [
    "EvidenceGap",
    "RecognitionDocument",
    "RecognitionFailure",
    "TextRecognizer",
    "TextRegion",
    "VisionEvidenceAgent",
    "VisionExtraction",
]
