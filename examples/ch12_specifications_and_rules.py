"""
第 12 章最小 Demo：本地规格资料 Evidence 加确定性 USB-C 条件规则。

文件整体逻辑
------------
第 11 章已经把充电器背面标签转换为视觉 Evidence。本 Demo 不重跑 OCR，而是构造等价的
65W/USB PD 视觉结果；再把用户人工给出的笔记本型号作为 ``laptop_model`` Evidence，查询
教学用本地 JSON 规格目录。目录返回最低功率、推荐功率和所需协议三条资料 Evidence，最后
由纯规则引擎给出 ``conditions_met``，同时保留线缆、端口和实际协商仍未知的边界。

使用的技术栈
------------
- Python 3.12 ``pathlib``/``json``：定位本地教学目录并显示结构化结果。
- Pydantic v2 Evidence：视觉、用户和资料来源共享同一事实格式。
- LocalSpecificationCatalog：可审计、精确匹配的本地资料检索。
- UsbCCompatibilityRules：不使用模型的 USB-C/PD 功率与协议条件判断。

调用流程
--------
模拟的第 11 章 charger Evidence + 用户 laptop_model Evidence
    -> LocalSpecificationCatalog.from_json_file()
    -> catalog.lookup()
    -> 三条 local_spec_catalog Evidence
    -> UsbCCompatibilityRules.evaluate()
    -> JSON 输出 + CH12_DEMO_OK。

边界
----
JSON 中的 ExampleBook 型号是虚构教学夹具，不是厂商规格。Demo 的 ``conditions_met`` 只表示
本项目已知的协议与推荐功率条件满足；它没有检测真实线缆、端口、系统电量或 USB PD 协商。
"""

from __future__ import annotations

import json
from pathlib import Path

from realsight.compatibility import (
    CompatibilityVerdict,
    LocalSpecificationCatalog,
    LookupStatus,
    UsbCCompatibilityRules,
)
from realsight.contracts import Evidence, EvidenceStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


def build_charger_evidence() -> tuple[Evidence, ...]:
    """模拟第 11 章已验证的视觉结果；本章只消费 Evidence，不回头读取图片。"""

    return (
        Evidence(
            evidence_id="evidence-vision-ch12-power",
            target_id="charger-ch12-demo",
            field="charger_max_power_w",
            value=65.0,
            source_type="vision_ocr",
            source_id="observation-ch12-demo",
            confidence=0.92,
            status=EvidenceStatus.CONFIRMED,
            source_metadata={"normalizer": "teaching-demo-from-ch11"},
        ),
        Evidence(
            evidence_id="evidence-vision-ch12-protocol",
            target_id="charger-ch12-demo",
            field="charger_protocol",
            value=["usb_pd"],
            source_type="vision_ocr",
            source_id="observation-ch12-demo",
            confidence=0.92,
            status=EvidenceStatus.CONFIRMED,
            source_metadata={"extensions": ["pps"]},
        ),
    )


def build_laptop_model_evidence() -> Evidence:
    """代表用户明确输入的型号；用户输入和资料检索证据故意使用不同来源类型。"""

    return Evidence(
        evidence_id="evidence-user-ch12-model",
        target_id="laptop-ch12-demo",
        field="laptop_model",
        value="ExampleBook 13",
        source_type="user_input",
        source_id="user-ch12-demo",
        confidence=1.0,
        status=EvidenceStatus.CONFIRMED,
    )


def main() -> int:
    """执行一次本地资料检索和规则判断；任何非预期状态都用退出码暴露。"""

    catalog = LocalSpecificationCatalog.from_json_file(CATALOG_PATH)
    laptop_model = build_laptop_model_evidence()
    lookup = catalog.lookup(laptop_model, laptop_target_id="laptop-ch12-demo")
    if lookup.status is not LookupStatus.FOUND:
        print("第 12 章 Demo 没有在教学目录中找到笔记本规格")
        return 1

    result = UsbCCompatibilityRules().evaluate(
        charger_target_id="charger-ch12-demo",
        charger_evidence=build_charger_evidence(),
        laptop_target_id="laptop-ch12-demo",
        laptop_evidence=lookup.evidence,
    )
    print(
        json.dumps(
            {
                "model_evidence": laptop_model.model_dump(mode="json"),
                "lookup": lookup.model_dump(mode="json"),
                "compatibility": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if result.verdict is not CompatibilityVerdict.CONDITIONS_MET:
        print("第 12 章 Demo 没有得到条件满足结论")
        return 1
    if not result.unknown_boundaries or len(result.used_evidence_ids) != 5:
        print("第 12 章 Demo 丢失未知边界或证据追踪信息")
        return 1
    print("CH12_DEMO_OK verdict=conditions_met evidence=5 boundaries=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
