"""
第 14 章的任务应用服务：将同步 LangGraph 工作流变成 API 可调用的用例。

文件逻辑
--------
TaskService 是 HTTP 与领域工作流之间的薄层。它创建任务、恢复 interrupt、读取
checkpoint、取消任务，并且可选地把 pending Observation 交给回放器或第 10 章的
C++ gRPC 适配器自动完成。服务不解释 USB-C、不解析 OCR，也不接受模型任意代码。

技术栈
------
- Python 3.12 dataclass、uuid、sqlite3；
- LangGraph InMemorySaver 或 SqliteSaver；
- 第 13 章 MainAgentState/workflow；
- 第 10 章感知 Provider、第 11 章 VisionEvidenceAgent、第 12 章规则模块。

调用流程
--------
FastAPI -> TaskService.create_task()/resume_task()/cancel_task()
    -> LangGraph checkpoint
    -> 可选 ObservationProvider -> resume_main_agent()
    -> MainAgentState 和 RunEvent
    -> FastAPI 查询或 WebSocket 重放事件。

边界
----
本服务是单进程教学 MVP：WebSocket 通过 checkpoint 轮询事件，多个 API 实例之间
没有共享事件总线。生产多副本部署需引入独立数据库、队列、认证与事件分发系统。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.contracts import SessionStatus
from realsight.governance import GovernanceUsage
from realsight.vision import TextRecognizer, VisionEvidenceAgent
from realsight.workflow import (
    DeterministicPlanner,
    MainAgentDependencies,
    MainAgentState,
    Planner,
    build_main_agent_graph,
    cancel_main_agent,
    get_active_interrupt,
    get_state,
    graph_config,
    initial_state,
    make_checkpoint_serializer,
    resume_main_agent,
)
from realsight.workflow.replay import ObservationProvider, UnavailableTextRecognizer


class TaskNotFoundError(KeyError):
    """API 层将其映射为 404，而不是把 LangGraph 的内部错误泄露给客户端。"""


@dataclass(slots=True)
class TaskService:
    """任务用例协调器；graph 的状态才是唯一事实来源，内存中不复制业务状态。"""

    graph: Any
    observation_provider: ObservationProvider | None = None
    sqlite_connection: sqlite3.Connection | None = None
    max_automatic_observations: int = 4
    governance_usage: GovernanceUsage = field(default_factory=GovernanceUsage)

    @classmethod
    def with_memory(
        cls,
        dependencies: MainAgentDependencies,
        *,
        observation_provider: ObservationProvider | None = None,
        governance_usage: GovernanceUsage | None = None,
    ) -> TaskService:
        """为测试和离线 Demo 构建内存 checkpoint 服务。"""

        graph, _ = build_main_agent_graph(dependencies)
        usage = governance_usage or GovernanceUsage()
        return cls(
            graph=graph,
            observation_provider=observation_provider,
            max_automatic_observations=usage.max_observations,
            governance_usage=usage,
        )

    @classmethod
    def with_sqlite(
        cls,
        database_path: Path,
        dependencies: MainAgentDependencies,
        *,
        observation_provider: ObservationProvider | None = None,
        governance_usage: GovernanceUsage | None = None,
    ) -> TaskService:
        """为单进程 API 打开 SQLite checkpoint；连接随 service 生命周期关闭。"""

        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            database_path, timeout=5.0, check_same_thread=False
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        saver = SqliteSaver(connection, serde=make_checkpoint_serializer())
        graph, _ = build_main_agent_graph(dependencies, checkpointer=saver)
        usage = governance_usage or GovernanceUsage()
        return cls(
            graph=graph,
            observation_provider=observation_provider,
            sqlite_connection=connection,
            max_automatic_observations=usage.max_observations,
            governance_usage=usage,
        )

    @staticmethod
    def default_dependencies(
        project_root: Path,
        *,
        planner: Planner | None = None,
        recognizer: TextRecognizer | None = None,
        planner_cost_units: int = 0,
    ) -> MainAgentDependencies:
        """构建诚实的服务器默认依赖；未配置 OCR 时工作流只会报告 gap。"""

        return MainAgentDependencies(
            planner=planner or DeterministicPlanner(),
            catalog=LocalSpecificationCatalog.from_json_file(
                project_root / "test-data" / "ch12" / "laptop-specifications.json"
            ),
            vision_agent=VisionEvidenceAgent(recognizer or UnavailableTextRecognizer()),
            rules=UsbCCompatibilityRules(),
            planner_cost_units=planner_cost_units,
        )

    def create_task(
        self,
        *,
        session_id: str | None,
        charger_target_id: str,
        laptop_target_id: str,
        intent: str,
    ) -> MainAgentState:
        """创建并运行到第一个暂停或终态；session_id 缺省时生成可 URL 使用的 UUID。"""

        created_session_id = session_id or f"task-{uuid.uuid4().hex}"
        state = initial_state(
            session_id=created_session_id,
            charger_target_id=charger_target_id,
            laptop_target_id=laptop_target_id,
            intent=intent,
            governance_usage=self.governance_usage,
        )
        self.graph.invoke(state, config=graph_config(created_session_id))
        return self._drive_automatic_observations(self.get_task(created_session_id))

    def get_task(self, session_id: str) -> MainAgentState:
        """读取任务状态，并统一将未知 thread 翻译为应用层错误。"""

        try:
            return get_state(self.graph, session_id)
        except KeyError as exc:
            raise TaskNotFoundError(session_id) from exc

    def current_interrupt_id(self, session_id: str) -> str | None:
        """终态没有 interrupt；暂停态返回当前 checkpoint 的真实 ID。"""

        state = self.get_task(session_id)
        if state.session.status not in {
            SessionStatus.WAITING_OBSERVATION,
            SessionStatus.WAITING_USER,
        }:
            return None
        return get_active_interrupt(self.graph, session_id).id

    def resume_task(
        self,
        *,
        session_id: str,
        interrupt_id: str,
        payload: dict[str, Any],
    ) -> MainAgentState:
        """恢复一个已验证 interrupt，并在配置了 provider 时继续自动处理观察。"""

        self.get_task(session_id)  # 先统一抛出 TaskNotFoundError。
        state = resume_main_agent(
            self.graph,
            thread_id=session_id,
            interrupt_id=interrupt_id,
            payload=payload,
        )
        return self._drive_automatic_observations(state)

    def cancel_task(self, *, session_id: str, reason: str) -> MainAgentState:
        """若 C++ 观察正在进行，先尽力转发取消，再把本地任务设为终态。"""

        state = self.get_task(session_id)
        if self.observation_provider is not None and state.pending_request is not None:
            self.observation_provider.cancel(state.pending_request.request_id, reason)
        return cancel_main_agent(self.graph, thread_id=session_id, reason=reason)

    def _drive_automatic_observations(self, state: MainAgentState) -> MainAgentState:
        """回放或 C++ provider 存在时自动消费观察暂停；用户型号暂停仍保留给客户端。"""

        if self.observation_provider is None:
            return state
        for _ in range(self.max_automatic_observations):
            if state.session.status is not SessionStatus.WAITING_OBSERVATION:
                return state
            request = state.pending_request
            if request is None:
                raise RuntimeError("waiting observation state has no pending request")
            interrupt_id = get_active_interrupt(self.graph, state.session.thread_id).id
            observation = self.observation_provider.observe(request)
            state = resume_main_agent(
                self.graph,
                thread_id=state.session.thread_id,
                interrupt_id=interrupt_id,
                payload={
                    "kind": "observation",
                    "observation": observation.model_dump(mode="json"),
                },
            )
        if state.session.status is not SessionStatus.WAITING_OBSERVATION:
            return state
        raise RuntimeError("automatic observation limit reached")

    def close(self) -> None:
        """关闭由 with_sqlite 创建的连接；内存服务没有外部资源需要释放。"""

        if self.observation_provider is not None:
            self.observation_provider.close()
            self.observation_provider = None
        if self.sqlite_connection is not None:
            self.sqlite_connection.close()
            self.sqlite_connection = None


__all__ = ["TaskNotFoundError", "TaskService"]
