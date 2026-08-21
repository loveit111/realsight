"""
第 12 章本地规格检索与 USB-C 条件规则的确定性测试。

文件整体逻辑
------------
这些测试不访问互联网，也不调用 OCR。它们用真实的 Pydantic Evidence 和一个临时 JSON
目录验证：规格检索只接受 confirmed 的笔记本型号；规则会区分条件满足、功率受限、协议不
匹配、证据冲突和证据不足；相同协议集合的不同书写顺序不会被误报成冲突。

使用的技术栈
------------
- pytest 9：``tmp_path`` 隔离歧义目录，普通断言验证分支。
- Python 3.12 JSON/pathlib：读取真实教学规格夹具。
- Pydantic v2 Evidence 与 RealSight compatibility 正式包。

调用流程
--------
pytest -> build Evidence -> LocalSpecificationCatalog.lookup()
    -> 资料 Evidence -> UsbCCompatibilityRules.evaluate()
    -> 断言 verdict、来源 ID、缺口和冲突字段。

边界
----
测试只证明本项目规则的分支和来源链正确，不能证明虚构目录是厂商真值，也不能证明真实硬件
可以充电。真实资料审核和设备联调必须由后续项目验收单独覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue
from realsight.compatibility import (
    CompatibilityVerdict,
    LocalSpecificationCatalog,
    LookupStatus,
    UsbCCompatibilityResult,
    UsbCCompatibilityRules,
)
from realsight.contracts import Evidence, EvidenceStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


def make_evidence(
    evidence_id: str,
    *,
    target_id: str,
    field: str,
    value: JsonValue,
    status: EvidenceStatus = EvidenceStatus.CONFIRMED,
) -> Evidence:
    """用最少字段构造规则输入；测试重点是 value/status，不伪造外部服务。"""

    return Evidence(
        evidence_id=evidence_id,
        target_id=target_id,
        field=field,
        value=value,
        source_type="test",
        source_id="source-ch12-test",
        confidence=0.95,
        status=status,
    )


def charger_evidence(
    *,
    power: float = 65.0,
    protocols: list[str] | None = None,
    power_status: EvidenceStatus = EvidenceStatus.CONFIRMED,
) -> tuple[Evidence, ...]:
    """生成一组充电器证据；默认值能满足 ExampleBook 13 的推荐条件。"""

    protocol_value: list[JsonValue] = [
        protocol for protocol in (("usb_pd",) if protocols is None else protocols)
    ]
    return (
        make_evidence(
            "evidence-charger-power",
            target_id="charger-ch12-test",
            field="charger_max_power_w",
            value=power,
            status=power_status,
        ),
        make_evidence(
            "evidence-charger-protocol",
            target_id="charger-ch12-test",
            field="charger_protocol",
            value=protocol_value,
        ),
    )


def model_evidence(value: str = "ExampleBook 13") -> Evidence:
    """构造已由用户确认的笔记本型号 Evidence。"""

    return make_evidence(
        "evidence-user-model",
        target_id="laptop-ch12-test",
        field="laptop_model",
        value=value,
    )


def found_laptop_evidence() -> tuple[Evidence, ...]:
    """从真实教学目录检索 ExampleBook 13，并返回资料层的三条 Evidence。"""

    lookup = LocalSpecificationCatalog.from_json_file(CATALOG_PATH).lookup(
        model_evidence(), laptop_target_id="laptop-ch12-test"
    )
    assert lookup.status is LookupStatus.FOUND
    return lookup.evidence


def evaluate(
    charger: tuple[Evidence, ...],
    laptop: tuple[Evidence, ...] | None = None,
) -> UsbCCompatibilityResult:
    """减少各测试的样板代码，始终通过正式规则入口执行。"""

    return UsbCCompatibilityRules().evaluate(
        charger_target_id="charger-ch12-test",
        charger_evidence=charger,
        laptop_target_id="laptop-ch12-test",
        laptop_evidence=found_laptop_evidence() if laptop is None else laptop,
    )


def test_catalog_exact_match_creates_three_auditable_specification_evidence() -> None:
    """资料检索应从用户模型证据派生出三条来源可追踪的规格事实。"""

    lookup = LocalSpecificationCatalog.from_json_file(CATALOG_PATH).lookup(
        model_evidence("  examplebook   13  "), laptop_target_id="laptop-ch12-test"
    )

    assert lookup.status is LookupStatus.FOUND
    assert lookup.matched_model_id == "examplebook-13"
    assert {item.field for item in lookup.evidence} == {
        "laptop_minimum_power_w",
        "laptop_recommended_power_w",
        "laptop_required_protocol",
    }
    assert all(item.source_type == "local_spec_catalog" for item in lookup.evidence)
    assert all("evidence-user-model" in item.derived_from for item in lookup.evidence)


def test_catalog_not_found_is_a_structured_result_not_a_guessed_match() -> None:
    """陌生型号不会走相似匹配，也不会生成看似可信的资料 Evidence。"""

    lookup = LocalSpecificationCatalog.from_json_file(CATALOG_PATH).lookup(
        model_evidence("ExampleBook 14"), laptop_target_id="laptop-ch12-test"
    )

    assert lookup.status is LookupStatus.NOT_FOUND
    assert lookup.evidence == ()
    assert lookup.specification is None


def test_conditions_met_keeps_unknown_hardware_boundaries() -> None:
    """65W USB PD 满足 ExampleBook 13 推荐条件，但结果仍明确留下实际硬件未知项。"""

    result = evaluate(charger_evidence())

    assert result.verdict is CompatibilityVerdict.CONDITIONS_MET
    assert len(result.used_evidence_ids) == 5
    assert len(result.unknown_boundaries) == 3
    assert "线缆" in result.unknown_boundaries[0]


def test_power_between_minimum_and_recommended_is_limited_not_full_success() -> None:
    """45W 不低于最低 45W，却低于推荐 65W，应给出受限而不是成功承诺。"""

    result = evaluate(charger_evidence(power=45.0))

    assert result.verdict is CompatibilityVerdict.LIMITED_POWER
    assert "低于推荐值" in result.summary


def test_protocol_mismatch_precedes_power_comparison() -> None:
    """协议已不满足时，规则先说明协议问题，不能用足够瓦数掩盖它。"""

    result = evaluate(charger_evidence(power=100.0, protocols=["quick_charge"]))

    assert result.verdict is CompatibilityVerdict.PROTOCOL_MISMATCH


def test_conflicting_confirmed_power_blocks_rule_evaluation() -> None:
    """两条 confirmed 功率值不同是冲突，不允许规则选择较大的一项。"""

    conflicting = (
        *charger_evidence(),
        make_evidence(
            "evidence-charger-power-conflict",
            target_id="charger-ch12-test",
            field="charger_max_power_w",
            value=45.0,
        ),
    )
    result = evaluate(conflicting)

    assert result.verdict is CompatibilityVerdict.EVIDENCE_CONFLICT
    assert result.conflicting_fields == ("charger_max_power_w",)


def test_probable_power_is_missing_evidence_not_a_confirmed_rule_input() -> None:
    """视觉层的 probable 值保留给第 13 章复核，本章不会把它悄悄升级。"""

    result = evaluate(
        charger_evidence(power=65.0, power_status=EvidenceStatus.PROBABLE)
    )

    assert result.verdict is CompatibilityVerdict.INSUFFICIENT_EVIDENCE
    assert result.missing_fields == ("charger_max_power_w",)


def test_protocol_value_order_does_not_create_a_false_conflict() -> None:
    """同一协议集合的不同 JSON 列表顺序语义相同，规则应把它们共同视为支持证据。"""

    charger = (
        *charger_evidence(protocols=["usb_pd", "quick_charge"]),
        make_evidence(
            "evidence-charger-protocol-order",
            target_id="charger-ch12-test",
            field="charger_protocol",
            value=["quick_charge", "usb_pd"],
        ),
    )
    result = evaluate(charger)

    assert result.verdict is CompatibilityVerdict.CONDITIONS_MET


def test_ambiguous_alias_is_reported_without_selecting_a_record(tmp_path: Path) -> None:
    """两条记录共享一个别名时，目录必须返回候选列表而不是依赖 JSON 顺序。"""

    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    data["records"][1]["aliases"].append("Shared Alias")
    data["records"][0]["aliases"].append("Shared Alias")
    ambiguous_path = tmp_path / "ambiguous-specifications.json"
    ambiguous_path.write_text(json.dumps(data), encoding="utf-8")

    lookup = LocalSpecificationCatalog.from_json_file(ambiguous_path).lookup(
        model_evidence("Shared Alias"), laptop_target_id="laptop-ch12-test"
    )

    assert lookup.status is LookupStatus.AMBIGUOUS
    assert lookup.candidates == ("examplebook-13", "examplework-15")
    assert lookup.evidence == ()
