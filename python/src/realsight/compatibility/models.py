"""
第 12 章资料检索和 USB-C 规则的正式数据模型。

文件整体逻辑
------------
这一文件定义三类不能混淆的数据：本地规格库中的 ``LaptopSpecification``、一次模型查询
的 ``SpecificationLookupResult``、以及规则层最终给出的 ``UsbCCompatibilityResult``。
它们都使用第 4 章的严格 Pydantic 契约，并把规格资料转成已有的 ``Evidence``，而不是
在规则函数里暗藏没有来源的字典。

使用的技术栈
------------
- Python 3.12：Enum、Literal、tuple 和类型标注。
- Pydantic v2：StrictContract、字段范围、跨字段验证和 JSON 友好输出。
- RealSight contracts：Evidence 是跨视觉、资料和规则层共用的事实载体。

调用流程
--------
本地 JSON -> SpecificationCatalogDocument -> LaptopSpecification
    -> SpecificationLookupResult（含资料 Evidence）
    -> UsbCCompatibilityRules
    -> UsbCCompatibilityResult。

边界
----
这些模型不读取文件、不发网络请求、不计算 USB-C 结果。它们只约束资料、检索和结论的
形状，不能用字段校验替代厂商资料核验或真实设备测试。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, StrictFloat, field_validator, model_validator

from realsight.contracts import (
    Evidence,
    Identifier,
    NonEmptyText,
    StrictContract,
    UnitFloat,
)


class LookupStatus(StrEnum):
    """本地规格检索的四种受控结果；未知型号不是 Python 异常。"""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INVALID_QUERY = "invalid_query"


class CompatibilityVerdict(StrEnum):
    """规则层的结果类别；名称刻意不使用“已实际充电成功”。"""

    CONDITIONS_MET = "conditions_met"
    LIMITED_POWER = "limited_power"
    INSUFFICIENT_POWER = "insufficient_power"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EVIDENCE_CONFLICT = "evidence_conflict"


class LaptopSpecification(StrictContract):
    """一条经过人工整理的笔记本 USB-C 充电规格及其资料来源。"""

    schema_version: Literal[1] = 1
    model_id: Identifier
    display_name: NonEmptyText
    aliases: tuple[NonEmptyText, ...]
    minimum_power_w: StrictFloat = Field(gt=0.0, le=240.0)
    recommended_power_w: StrictFloat = Field(gt=0.0, le=240.0)
    required_protocols: tuple[NonEmptyText, ...]
    source_document_id: Identifier
    source_revision: NonEmptyText
    source_checked_at: AwareDatetime
    confidence: UnitFloat = 0.95

    @field_validator("aliases", "required_protocols")
    @classmethod
    def non_empty_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """别名和协议都必须存在且不重复，避免目录查询和规则输出不稳定。"""

        if not values:
            raise ValueError("aliases and required_protocols must not be empty")
        normalized = [value.casefold().strip() for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "aliases and required_protocols must not contain duplicates"
            )
        return values

    @model_validator(mode="after")
    def minimum_power_does_not_exceed_recommended_power(self) -> LaptopSpecification:
        """最低可运行功率不能高于推荐功率，否则规则没有可解释的中间区间。"""

        if self.minimum_power_w > self.recommended_power_w:
            raise ValueError("minimum_power_w must be <= recommended_power_w")
        return self


class SpecificationCatalogDocument(StrictContract):
    """磁盘 JSON 文件的整体契约；它是本章本地检索的可审计数据入口。"""

    schema_version: Literal[1] = 1
    catalog_id: Identifier
    records: tuple[LaptopSpecification, ...]

    @field_validator("records")
    @classmethod
    def record_ids_are_unique(
        cls, records: tuple[LaptopSpecification, ...]
    ) -> tuple[LaptopSpecification, ...]:
        """目录允许别名歧义并交给 lookup 报告，但不允许两个记录使用同一 model_id。"""

        if not records:
            raise ValueError("specification catalog must contain at least one record")
        record_ids = [record.model_id for record in records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("specification catalog model IDs must be unique")
        return records


class SpecificationLookupResult(StrictContract):
    """一次资料检索的结构化事实，保留查不到和歧义的正常业务分支。"""

    schema_version: Literal[1] = 1
    catalog_id: Identifier
    laptop_target_id: Identifier
    model_evidence_id: Identifier
    status: LookupStatus
    message: NonEmptyText
    matched_model_id: Identifier | None = None
    candidates: tuple[Identifier, ...] = ()
    specification: LaptopSpecification | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def result_shape_matches_status(self) -> SpecificationLookupResult:
        """成功结果必须完整且可追溯，非成功结果不能夹带另一台机器的规格证据。"""

        if self.status is LookupStatus.FOUND:
            if self.specification is None or self.matched_model_id is None:
                raise ValueError(
                    "found lookup requires a specification and matched_model_id"
                )
            if self.specification.model_id != self.matched_model_id:
                raise ValueError("matched_model_id must equal specification.model_id")
            if len(self.evidence) != 3:
                raise ValueError(
                    "found lookup must produce exactly three specification evidence"
                )
            for item in self.evidence:
                if item.target_id != self.laptop_target_id:
                    raise ValueError(
                        "specification evidence target does not match lookup"
                    )
                if item.source_type != "local_spec_catalog":
                    raise ValueError(
                        "specification evidence must use local_spec_catalog"
                    )
                if self.model_evidence_id not in item.derived_from:
                    raise ValueError(
                        "specification evidence must derive from model evidence"
                    )
        elif (
            self.specification is not None
            or self.evidence
            or self.matched_model_id is not None
        ):
            raise ValueError(
                "non-found lookup must not contain specification or evidence"
            )
        if self.status is LookupStatus.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("ambiguous lookup requires at least two candidates")
        if self.status is not LookupStatus.AMBIGUOUS and self.candidates:
            raise ValueError("only ambiguous lookup may contain candidates")
        return self


class UsbCCompatibilityResult(StrictContract):
    """确定性规则的可解释输出；证据 ID 与未知边界都是结果的一部分。"""

    schema_version: Literal[1] = 1
    charger_target_id: Identifier
    laptop_target_id: Identifier
    verdict: CompatibilityVerdict
    summary: NonEmptyText
    used_evidence_ids: tuple[Identifier, ...] = ()
    missing_fields: tuple[NonEmptyText, ...] = ()
    conflicting_fields: tuple[NonEmptyText, ...] = ()
    conditions: tuple[NonEmptyText, ...] = ()
    unknown_boundaries: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def result_matches_verdict(self) -> UsbCCompatibilityResult:
        """阻止把缺证据或冲突伪装成“条件满足”的漂亮结论。"""

        if len(set(self.used_evidence_ids)) != len(self.used_evidence_ids):
            raise ValueError("used_evidence_ids must be unique")
        if self.verdict is CompatibilityVerdict.EVIDENCE_CONFLICT:
            if not self.conflicting_fields:
                raise ValueError("evidence_conflict requires conflicting_fields")
        elif self.conflicting_fields:
            raise ValueError("only evidence_conflict may contain conflicting_fields")
        if self.verdict is CompatibilityVerdict.INSUFFICIENT_EVIDENCE:
            if not self.missing_fields:
                raise ValueError("insufficient_evidence requires missing_fields")
        elif self.missing_fields:
            raise ValueError("only insufficient_evidence may contain missing_fields")
        if self.verdict is CompatibilityVerdict.CONDITIONS_MET and not self.conditions:
            raise ValueError("conditions_met requires explicit conditions")
        return self


__all__ = [
    "CompatibilityVerdict",
    "LaptopSpecification",
    "LookupStatus",
    "SpecificationCatalogDocument",
    "SpecificationLookupResult",
    "UsbCCompatibilityResult",
]
