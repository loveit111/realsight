"""
第 8 章兼容入口：把旧的 ``contracts.models`` 导入转发到正式包。

这个文件的整体逻辑
------------------
第 4～7 章的课程示例已经从 ``contracts.models`` 导入稳定领域类型。如果直接删除旧
模块，历史示例和 120 项回归测试会一起失效；如果复制一套模型继续维护，又会产生两个
名字相同但 Python 身份不同的类。本兼容入口只重新导出正式 ``realsight.contracts``
中的同一批类，因此旧代码和新代码使用的是同一个类对象。

技术栈与调用流程
----------------
- Python 包导入系统：旧模块导入被转发到 src 布局中的正式包。
- 公共接口 ``__all__``：只暴露领域契约，不暴露实现依赖。

旧示例 -> contracts.models -> realsight.contracts.models -> Pydantic 契约

新代码不要继续依赖本文件；迁移窗口结束后可以删除它。
"""

from realsight.contracts.models import *  # noqa: F403
from realsight.contracts.models import __all__ as __all__
