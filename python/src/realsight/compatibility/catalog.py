"""
第 12 章本地笔记本规格目录：将人工核验的 JSON 资料转成来源明确的 Evidence。

文件整体逻辑
------------
真实的网络检索会受到供应商页面变化、权限、版权、网络、隐私和结果排序影响，不适合作为
一个月教学 MVP 的确定性基础。本模块读取一份版本化本地 JSON 目录，对用户提供的
``laptop_model`` Evidence 做严格的规范化精确匹配；成功后不直接给出兼容结论，而是生成
最低功率、推荐功率和所需协议三条资料 Evidence。查不到或别名歧义同样返回结构化结果。

使用的技术栈
------------
- Python 3.12 ``json``/``pathlib``/``re``：读取结构化目录并做保守的空白与大小写规范化。
- Pydantic v2：SpecificationCatalogDocument 和 Evidence 的运行时校验。
- hashlib SHA-256：生成稳定的资料 Evidence ID，避免调用次数影响审计结果。

调用流程
--------
laptop_model Evidence + JSON 文件路径
    -> LocalSpecificationCatalog.from_json_file()
    -> lookup(model_evidence, laptop_target_id)
    -> 精确别名匹配
    -> found：三条 local_spec_catalog Evidence
    -> not_found / ambiguous / invalid_query：无 Evidence 的结构化结果
    -> 第 12 章规则层或第 13 章规划层。

边界
----
本模块不联网、不爬网页、不做模糊匹配、不选择歧义型号，也不验证目录中的资料真伪。目录
记录必须由人引用并审核真实厂商资料；本章的教学夹具使用虚构型号，绝不能当作真实设备规格。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import JsonValue

from realsight.contracts import Evidence, EvidenceStatus

from .models import (
    LaptopSpecification,
    LookupStatus,
    SpecificationCatalogDocument,
    SpecificationLookupResult,
)


class LocalSpecificationCatalog:
    """内存中的只读规格目录；创建后查询不修改 JSON 文件或记录内容。"""

    def __init__(self, document: SpecificationCatalogDocument) -> None:
        """保存已校验目录，并建立“规范化术语 -> 多个候选记录”的索引。"""

        self._document = document
        self._terms_to_records: dict[str, tuple[LaptopSpecification, ...]] = (
            self._build_term_index(document.records)
        )

    @classmethod
    def from_json_file(cls, path: Path) -> LocalSpecificationCatalog:
        """读取并验证 UTF-8 JSON；I/O 或格式错误保留为启动配置错误，而不是检索未命中。"""

        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"cannot load specification catalog {path}: {exc}"
            ) from exc
        return cls(SpecificationCatalogDocument.model_validate(raw_data))

    def lookup(
        self,
        model_evidence: Evidence,
        *,
        laptop_target_id: str,
    ) -> SpecificationLookupResult:
        """依据一条 confirmed 的 laptop_model Evidence 查目录，并将规格转成三条 Evidence。"""

        if model_evidence.target_id != laptop_target_id:
            raise ValueError("model evidence target_id must match laptop_target_id")
        if model_evidence.field != "laptop_model":
            raise ValueError(
                "catalog lookup requires evidence.field to be laptop_model"
            )
        if model_evidence.status is not EvidenceStatus.CONFIRMED:
            return self._result(
                model_evidence,
                laptop_target_id,
                status=LookupStatus.INVALID_QUERY,
                message="笔记本型号尚未被确认，不能据此检索规格目录。",
            )
        if not isinstance(model_evidence.value, str):
            return self._result(
                model_evidence,
                laptop_target_id,
                status=LookupStatus.INVALID_QUERY,
                message="laptop_model Evidence 的 value 必须是非空字符串。",
            )
        normalized_query = self._normalize_term(model_evidence.value)
        if not normalized_query:
            return self._result(
                model_evidence,
                laptop_target_id,
                status=LookupStatus.INVALID_QUERY,
                message="笔记本型号查询为空，不能检索规格目录。",
            )

        matches = self._terms_to_records.get(normalized_query, ())
        if not matches:
            return self._result(
                model_evidence,
                laptop_target_id,
                status=LookupStatus.NOT_FOUND,
                message="本地规格目录中没有该型号；请人工补充型号或经过核验的资料。",
            )
        if len(matches) > 1:
            return self._result(
                model_evidence,
                laptop_target_id,
                status=LookupStatus.AMBIGUOUS,
                message="该型号别名对应多条规格，不能自动选择其中一台笔记本。",
                candidates=tuple(record.model_id for record in matches),
            )

        specification = matches[0]
        return SpecificationLookupResult(
            catalog_id=self._document.catalog_id,
            laptop_target_id=laptop_target_id,
            model_evidence_id=model_evidence.evidence_id,
            status=LookupStatus.FOUND,
            message="已从本地人工核验规格目录找到精确型号匹配。",
            matched_model_id=specification.model_id,
            specification=specification,
            evidence=self._specification_evidence(
                specification,
                model_evidence=model_evidence,
                laptop_target_id=laptop_target_id,
                normalized_query=normalized_query,
            ),
        )

    @staticmethod
    def _normalize_term(value: str) -> str:
        """只消除大小写和连续空白；不做编辑距离或“看起来像”的危险模糊匹配。"""

        return re.sub(r"\s+", " ", value.casefold()).strip()

    @classmethod
    def _build_term_index(
        cls,
        records: tuple[LaptopSpecification, ...],
    ) -> dict[str, tuple[LaptopSpecification, ...]]:
        """建立允许歧义的索引，让 lookup 显式报告问题而不是依赖文件顺序选第一条。"""

        mutable_index: dict[str, list[LaptopSpecification]] = {}
        for record in records:
            for term in (record.model_id, record.display_name, *record.aliases):
                normalized_term = cls._normalize_term(term)
                matches = mutable_index.setdefault(normalized_term, [])
                # model_id、display_name 和 alias 可以规范化成同一个术语；这仍是一条
                # 记录，不能把它错误报告成“两个候选型号”。
                if all(existing.model_id != record.model_id for existing in matches):
                    matches.append(record)
        return {term: tuple(items) for term, items in mutable_index.items()}

    def _result(
        self,
        model_evidence: Evidence,
        laptop_target_id: str,
        *,
        status: LookupStatus,
        message: str,
        candidates: tuple[str, ...] = (),
    ) -> SpecificationLookupResult:
        """构造未命中、歧义或无效查询的统一返回值；它们是可规划的业务状态。"""

        return SpecificationLookupResult(
            catalog_id=self._document.catalog_id,
            laptop_target_id=laptop_target_id,
            model_evidence_id=model_evidence.evidence_id,
            status=status,
            message=message,
            candidates=candidates,
        )

    def _specification_evidence(
        self,
        specification: LaptopSpecification,
        *,
        model_evidence: Evidence,
        laptop_target_id: str,
        normalized_query: str,
    ) -> tuple[Evidence, ...]:
        """将一条规格记录拆成规则可分别消费的三条事实，避免把大对象塞进一个字段。"""

        common_metadata: dict[str, JsonValue] = {
            "catalog_id": self._document.catalog_id,
            "model_id": specification.model_id,
            "display_name": specification.display_name,
            "matched_query": normalized_query,
            "source_revision": specification.source_revision,
            "source_checked_at": specification.source_checked_at.isoformat(),
        }
        return (
            self._build_evidence(
                specification,
                model_evidence=model_evidence,
                laptop_target_id=laptop_target_id,
                field="laptop_minimum_power_w",
                value=specification.minimum_power_w,
                metadata=common_metadata,
            ),
            self._build_evidence(
                specification,
                model_evidence=model_evidence,
                laptop_target_id=laptop_target_id,
                field="laptop_recommended_power_w",
                value=specification.recommended_power_w,
                metadata=common_metadata,
            ),
            self._build_evidence(
                specification,
                model_evidence=model_evidence,
                laptop_target_id=laptop_target_id,
                field="laptop_required_protocol",
                value=self._json_strings(list(specification.required_protocols)),
                metadata=common_metadata,
            ),
        )

    @staticmethod
    def _build_evidence(
        specification: LaptopSpecification,
        *,
        model_evidence: Evidence,
        laptop_target_id: str,
        field: str,
        value: JsonValue,
        metadata: dict[str, JsonValue],
    ) -> Evidence:
        """为同一资料字段创建稳定 ID；同一目录重复查询不会生成另一份含义相同的事实。"""

        digest = hashlib.sha256(
            f"{specification.source_document_id}|{specification.model_id}|{field}|{value!r}".encode()
        ).hexdigest()[:16]
        return Evidence(
            evidence_id=f"evidence-spec-{digest}",
            target_id=laptop_target_id,
            field=field,
            value=value,
            source_type="local_spec_catalog",
            source_id=specification.source_document_id,
            confidence=specification.confidence,
            status=EvidenceStatus.CONFIRMED,
            derived_from=(model_evidence.evidence_id,),
            source_metadata=dict(metadata),
        )

    @staticmethod
    def _json_strings(values: list[str]) -> list[JsonValue]:
        """显式构造 JsonValue 列表，使严格类型检查知道协议值可安全序列化。"""

        return [value for value in values]


__all__ = ["LocalSpecificationCatalog"]
