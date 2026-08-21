"""
RealSight 存储适配层的包入口。

整体逻辑
--------
本层拥有检查点、Evidence Ledger、Artifact Store 等持久化适配器，不让工作流节点直接
拼写 SQL。第 6 章教学实现仍在 examples，第 8 章先固定依赖方向。

技术栈
------
未来复用 SQLite、LangGraph checkpointer、pathlib 与 SHA-256；入口本身不打开数据库。

调用流程
--------
application/workflow -> storage 接口 -> SQLite/文件系统 -> 领域契约或持久化记录。
"""
