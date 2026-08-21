"""
RealSight USB-C 资料检索与确定性兼容规则包的公共入口。

文件整体逻辑
------------
第 12 章把“视觉层读到的充电器证据”和“本地资料库中的笔记本规格证据”交给同一套纯
Python 规则。这个入口只导出稳定的目录、检索结果、规则引擎和结论模型；调用方无需依赖
内部 JSON 读取、ID 生成或字段归一化细节。

使用的技术栈
------------
- Python 3.12 包边界与类型标注。
- Pydantic v2 领域契约，由内部模块复用第 4 章 Evidence。
- JSON 本地规格目录和确定性 USB-C/PD 条件规则。

调用流程
--------
用户提供的 laptop_model Evidence
    -> LocalSpecificationCatalog.lookup()
    -> 资料来源 Evidence
    -> UsbCCompatibilityRules.evaluate()
    -> UsbCCompatibilityResult
    -> 第 13 章主工作流决定继续观察、询问或生成回答。

边界
----
本包不调用互联网、不执行模糊搜索、不控制硬件、不调用大模型，也不写入 BeliefState。
``conditions_met`` 仅表示已知功率和协议条件满足，不等价于已在真实笔记本上完成充电测试。
"""

from .catalog import LocalSpecificationCatalog
from .models import (
    CompatibilityVerdict,
    LaptopSpecification,
    LookupStatus,
    SpecificationLookupResult,
    UsbCCompatibilityResult,
)
from .rules import RuleInputError, UsbCCompatibilityRules

__all__ = [
    "CompatibilityVerdict",
    "LaptopSpecification",
    "LocalSpecificationCatalog",
    "LookupStatus",
    "RuleInputError",
    "SpecificationLookupResult",
    "UsbCCompatibilityResult",
    "UsbCCompatibilityRules",
]
