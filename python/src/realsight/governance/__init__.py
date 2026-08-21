"""
RealSight 运行治理层的包入口。

整体逻辑
--------
本层负责权限、幂等、预算、超时、重试和审计，不负责 Agent 规划。第 8 章只确定模块
所有权；第 7 章教学实现仍保留在 examples，待接口稳定后再按职责拆入本包。

技术栈
------
未来复用 asyncio、Pydantic 与 SQLite；本入口当前不执行任何副作用。

调用流程
--------
application/workflow -> governance middleware -> 被治理的 services operation -> 审计结果。
"""
