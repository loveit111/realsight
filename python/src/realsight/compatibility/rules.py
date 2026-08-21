"""
第 12 章 USB-C 充电条件规则：只根据可追溯 Evidence 做确定性判断。

文件整体逻辑
------------
视觉子 Agent 已提供充电器的最大输出功率和协议候选；本地资料目录已提供笔记本的最低/推荐
功率和所需协议。本模块先检查每个字段是否只有一个 confirmed 值，再按固定顺序判断冲突、
缺证据、协议、最低功率和推荐功率。结果始终附带使用的 Evidence ID、条件和未验证边界，
因此不会把“条件满足”伪装成已经接上真实设备后的充电成功。

使用的技术栈
------------
- Python 3.12 ``dataclass``/``json``：实现纯函数式的字段归并和稳定值比较。
- Pydantic v2：通过 UsbCCompatibilityResult 保证结论与缺失/冲突字段一致。
- RealSight Evidence：统一消费视觉与资料来源，不在规则里读取图片或 JSON 文件。

调用流程
--------
第 11 章 charger Evidence + 第 12 章 catalog Evidence
    -> UsbCCompatibilityRules.evaluate()
    -> resolve_field() 检查 confirmed / probable / conflict
    -> conflict -> missing -> protocol -> minimum power -> recommended power
    -> UsbCCompatibilityResult
    -> 第 13 章将结果或缺口放入工作流。

边界
----
本规则不协商真实 USB PD、不读取线缆 e-marker、不判断多口共享功率、不控制设备，也不使用
模型猜测缺失值。它只针对课程 MVP 的单充电器、人工提供笔记本型号、已知 USB PD 条件做
静态判断；真实充电结果仍取决于端口、线缆、系统状态和实际协商。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import JsonValue

from realsight.contracts import Evidence, EvidenceStatus

from .models import CompatibilityVerdict, UsbCCompatibilityResult


class RuleInputError(ValueError):
    """规则输入违反领域契约时抛出；这是程序/适配错误，不应被当成兼容结论。"""


@dataclass(frozen=True, slots=True)
class _ResolvedField:
    """规则内部的字段归并结果；它保留所有参与判断的 Evidence。"""

    field: str
    value: JsonValue | None
    evidence: tuple[Evidence, ...]
    is_missing: bool = False
    is_conflicting: bool = False


class UsbCCompatibilityRules:
    """课程 MVP 的纯确定性 USB-C 条件规则，不持有文件、网络或数据库状态。"""

    _CHARGER_FIELDS = ("charger_max_power_w", "charger_protocol")
    _LAPTOP_FIELDS = (
        "laptop_minimum_power_w",
        "laptop_recommended_power_w",
        "laptop_required_protocol",
    )
    _UNKNOWN_BOUNDARIES = (
        "未验证 USB-C 线缆的电流、功率和 e-marker 能力。",
        "未验证笔记本实际端口角色、系统状态和 USB PD 协商结果。",
        "未验证充电器多口共享功率、温度保护和实际输出状态。",
    )

    def evaluate(
        self,
        *,
        charger_target_id: str,
        charger_evidence: tuple[Evidence, ...],
        laptop_target_id: str,
        laptop_evidence: tuple[Evidence, ...],
    ) -> UsbCCompatibilityResult:
        """以固定、可审计顺序比较功率和协议，绝不从 Evidence 之外补猜输入。"""

        self._validate_targets(charger_target_id, charger_evidence, role="charger")
        self._validate_targets(laptop_target_id, laptop_evidence, role="laptop")
        resolved = {
            field: self._resolve_field(charger_evidence, field)
            for field in self._CHARGER_FIELDS
        }
        resolved.update(
            {
                field: self._resolve_field(laptop_evidence, field)
                for field in self._LAPTOP_FIELDS
            }
        )

        conflicts = tuple(
            field for field, item in resolved.items() if item.is_conflicting
        )
        if conflicts:
            return self._result(
                charger_target_id,
                laptop_target_id,
                verdict=CompatibilityVerdict.EVIDENCE_CONFLICT,
                summary="关键功率或协议证据存在冲突，不能执行 USB-C 兼容判断。",
                resolved=resolved,
                conflicting_fields=conflicts,
            )

        missing = tuple(field for field, item in resolved.items() if item.is_missing)
        if missing:
            return self._result(
                charger_target_id,
                laptop_target_id,
                verdict=CompatibilityVerdict.INSUFFICIENT_EVIDENCE,
                summary="关键功率或协议证据不完整，不能给出 USB-C 兼容结论。",
                resolved=resolved,
                missing_fields=missing,
            )

        charger_power = self._power_value(resolved["charger_max_power_w"])
        laptop_minimum_power = self._power_value(resolved["laptop_minimum_power_w"])
        laptop_recommended_power = self._power_value(
            resolved["laptop_recommended_power_w"]
        )
        charger_protocols = self._protocol_values(resolved["charger_protocol"])
        laptop_required_protocols = self._protocol_values(
            resolved["laptop_required_protocol"]
        )

        if not laptop_required_protocols.issubset(charger_protocols):
            return self._result(
                charger_target_id,
                laptop_target_id,
                verdict=CompatibilityVerdict.PROTOCOL_MISMATCH,
                summary="充电器已知协议不满足笔记本资料要求的 USB-C 充电协议。",
                resolved=resolved,
                conditions=(
                    "需使用支持笔记本所需协议的充电器或补充更准确的协议证据。",
                ),
            )
        if charger_power < laptop_minimum_power:
            return self._result(
                charger_target_id,
                laptop_target_id,
                verdict=CompatibilityVerdict.INSUFFICIENT_POWER,
                summary="充电器最大输出功率低于笔记本资料的最低 USB-C 充电功率。",
                resolved=resolved,
                conditions=(
                    f"充电器 {charger_power:g}W 小于最低要求 {laptop_minimum_power:g}W。",
                ),
            )
        if charger_power < laptop_recommended_power:
            return self._result(
                charger_target_id,
                laptop_target_id,
                verdict=CompatibilityVerdict.LIMITED_POWER,
                summary="协议和最低功率条件满足，但功率低于推荐值，可能只能在轻负载下充电或出现充电变慢。",
                resolved=resolved,
                conditions=(
                    f"充电器 {charger_power:g}W 不低于最低要求 {laptop_minimum_power:g}W。",
                    f"充电器 {charger_power:g}W 低于推荐值 {laptop_recommended_power:g}W。",
                ),
            )
        return self._result(
            charger_target_id,
            laptop_target_id,
            verdict=CompatibilityVerdict.CONDITIONS_MET,
            summary="已知 USB PD 协议和功率条件满足；仍需在真实线缆与端口上验证实际协商。",
            resolved=resolved,
            conditions=(
                "充电器已知协议包含笔记本资料要求的协议。",
                f"充电器 {charger_power:g}W 不低于推荐值 {laptop_recommended_power:g}W。",
            ),
        )

    @staticmethod
    def _validate_targets(
        target_id: str,
        evidence: tuple[Evidence, ...],
        *,
        role: str,
    ) -> None:
        """规则调用方必须先分好充电器与笔记本证据，不能混入另一个现实目标。"""

        for item in evidence:
            if item.target_id != target_id:
                raise RuleInputError(
                    f"{role} evidence {item.evidence_id} belongs to {item.target_id}, "
                    f"not {target_id}"
                )

    def _resolve_field(
        self,
        evidence: tuple[Evidence, ...],
        field: str,
    ) -> _ResolvedField:
        """确认字段只有一个 confirmed 值；probable/unknown 是缺口，显式或隐式矛盾都阻断规则。"""

        matching = tuple(item for item in evidence if item.field == field)
        confirmed = tuple(
            item for item in matching if item.status is EvidenceStatus.CONFIRMED
        )
        has_explicit_conflict = any(
            item.status is EvidenceStatus.CONFLICT for item in matching
        )
        if has_explicit_conflict or self._different_confirmed_values(field, confirmed):
            return _ResolvedField(
                field=field,
                value=None,
                evidence=matching,
                is_conflicting=True,
            )
        if not confirmed:
            return _ResolvedField(
                field=field,
                value=None,
                evidence=matching,
                is_missing=True,
            )
        return _ResolvedField(field=field, value=confirmed[0].value, evidence=confirmed)

    def _different_confirmed_values(
        self,
        field: str,
        evidence: tuple[Evidence, ...],
    ) -> bool:
        """同字段多个 confirmed 只有值不一致时才冲突；协议列表按集合比较而非书写顺序。"""

        canonical_values = {
            self._canonical_value(field, item.value) for item in evidence
        }
        return len(canonical_values) > 1

    def _canonical_value(self, field: str, value: JsonValue) -> str:
        """把 JSON 值变成稳定比较键，并对协议字段忽略列表顺序。"""

        if field in {"charger_protocol", "laptop_required_protocol"}:
            protocols = self._protocol_values_from_json(value, field)
            return json.dumps(sorted(protocols), ensure_ascii=False)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _power_value(resolved: _ResolvedField) -> float:
        """把已确认功率转为安全 float；bool 和字符串数字都被拒绝，防止静默类型转换。"""

        value = resolved.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleInputError(f"{resolved.field} must contain a JSON number")
        numeric_value = float(value)
        if not 0.0 < numeric_value <= 240.0:
            raise RuleInputError(f"{resolved.field} must be between zero and 240 watts")
        return numeric_value

    def _protocol_values(self, resolved: _ResolvedField) -> frozenset[str]:
        """取得已确认协议集合；具体输入校验集中在同一辅助函数。"""

        return self._protocol_values_from_json(resolved.value, resolved.field)

    @staticmethod
    def _protocol_values_from_json(
        value: JsonValue | None,
        field: str,
    ) -> frozenset[str]:
        """接受历史单字符串和当前 JSON 字符串列表，但拒绝空值和非字符串元素。"""

        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = []
            for item in value:
                if not isinstance(item, str):
                    raise RuleInputError(
                        f"{field} must contain a protocol string or string list"
                    )
                values.append(item)
        else:
            raise RuleInputError(
                f"{field} must contain a protocol string or string list"
            )
        normalized = frozenset(
            item.casefold().strip() for item in values if item.strip()
        )
        if not normalized:
            raise RuleInputError(
                f"{field} must contain at least one non-empty protocol"
            )
        return normalized

    @staticmethod
    def _result(
        charger_target_id: str,
        laptop_target_id: str,
        *,
        verdict: CompatibilityVerdict,
        summary: str,
        resolved: dict[str, _ResolvedField],
        missing_fields: tuple[str, ...] = (),
        conflicting_fields: tuple[str, ...] = (),
        conditions: tuple[str, ...] = (),
    ) -> UsbCCompatibilityResult:
        """统一收集参与判断的证据 ID 和固定未知边界，避免不同分支漏掉审计信息。"""

        evidence_ids = tuple(
            dict.fromkeys(
                item.evidence_id
                for resolved_field in resolved.values()
                for item in resolved_field.evidence
            )
        )
        return UsbCCompatibilityResult(
            charger_target_id=charger_target_id,
            laptop_target_id=laptop_target_id,
            verdict=verdict,
            summary=summary,
            used_evidence_ids=evidence_ids,
            missing_fields=missing_fields,
            conflicting_fields=conflicting_fields,
            conditions=conditions,
            unknown_boundaries=UsbCCompatibilityRules._UNKNOWN_BOUNDARIES,
        )


__all__ = ["RuleInputError", "UsbCCompatibilityRules"]
