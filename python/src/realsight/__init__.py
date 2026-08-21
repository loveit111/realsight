"""
RealSight 正式 Python 包的最外层公共入口。

整体逻辑
--------
这个文件只公布最常用的稳定领域契约和包版本，不在导入时创建数据库、网络连接或工作流。
保持入口轻量，可以让 CLI、测试和后续 FastAPI 服务安全地导入包。

技术栈
------
- Python ``src`` 包布局：只有安装后的 ``realsight`` 才能被可靠导入。
- Pydantic 契约：实际类型由 ``realsight.contracts`` 提供。

调用流程
--------
调用方 ``import realsight`` -> 本入口 -> ``realsight.contracts`` -> 稳定领域模型。
需要配置、存储或工作流时，应显式导入对应子包，不通过这里制造隐藏依赖。
"""

from realsight.contracts import Evidence, Observation, ObservationRequest, TaskSession

__all__ = ["Evidence", "Observation", "ObservationRequest", "TaskSession"]
__version__ = "0.1.0"
