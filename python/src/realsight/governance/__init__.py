"""
RealSight 运行治理层的包入口。

整体逻辑
--------
本层负责 capability 与可持久化预算，不负责 Agent 规划。第 7 章教学实现仍保留在
examples；正式主图使用本包的 ``GovernanceUsage`` 执行迭代、命令、观察、外部尝试和
成本上限。

技术栈
------
Pydantic 严格不可变契约与 LangGraph/SQLite checkpoint；导入本包不执行副作用。

调用流程
--------
application 配置 -> GovernanceUsage -> workflow.consume -> 新快照或 GovernanceViolation。
"""

from .policy import DEFAULT_CAPABILITIES, GovernanceUsage, GovernanceViolation

__all__ = ["DEFAULT_CAPABILITIES", "GovernanceUsage", "GovernanceViolation"]
