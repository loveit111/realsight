"""
RealSight 正式领域契约包的公共入口。

整体逻辑
--------
这里只重新导出 ``models.py`` 明确列出的公共类型，让调用者不依赖内部文件组织。

技术栈
------
Python ``__all__`` 控制公共 API；Pydantic v2 负责模型验证和 JSON 序列化。

调用流程
--------
外部 JSON/适配器 -> ``realsight.contracts`` -> Pydantic 模型 -> 工作流、存储或服务层。
"""

from realsight.contracts.models import *  # noqa: F403
from realsight.contracts.models import __all__ as __all__
