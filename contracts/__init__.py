"""
第 8 章旧课程契约兼容包。

整体逻辑
--------
第 4～7 章使用 ``contracts.models``，正式工程使用 ``realsight.contracts``。本包只为
旧示例提供过渡导入；它不拥有模型实现，因此不会形成两套同名 Pydantic 类。

技术栈
------
Python 包转发和 ``__all__``；模型实现仍是正式包中的 Pydantic v2 契约。

调用流程
--------
旧示例 -> ``contracts`` -> ``contracts.models`` -> ``realsight.contracts.models``。
新代码应直接导入 ``realsight.contracts``，迁移完成后删除这一层。
"""

from contracts.models import *  # noqa: F403
from contracts.models import __all__ as __all__
