"""
RealSight 应用用例层的包入口。

整体逻辑
--------
这一层未来组合工作流、存储、治理和服务端口，表达“启动任务、恢复任务、取消任务”等
完整用例；它不拥有视觉算法或数据库 SQL。第 8 章先建立所有权边界，不提前搬入逻辑。

技术栈
------
当前仅使用 Python 包系统；第 13～14 章会由 LangGraph 和 FastAPI 调用这里的用例函数。

调用流程
--------
CLI/API -> application 用例 -> workflow/governance -> services/storage。
"""

from .task_service import TaskNotFoundError, TaskService

__all__ = ["TaskNotFoundError", "TaskService"]
