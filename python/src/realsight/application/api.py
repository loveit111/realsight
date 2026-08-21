"""
第 14 章 FastAPI 与 WebSocket 接口。

文件逻辑
--------
本模块把 TaskService 暴露为本机教学 API：创建任务、读取状态、恢复中断、取消任务，
以及按 RunEvent.sequence 重放和持续推送 WebSocket 事件。HTTP 数据传输只使用 JSON，
浏览器从不直连 C++ gRPC；C++ 仍由服务端适配器处理。

技术栈
------
- FastAPI 与 Pydantic v2：请求验证、OpenAPI、HTTP 错误；
- asyncio.to_thread：同步 LangGraph/gRPC 教学实现不会堵塞事件循环；
- WebSocket：按 sequence 轮询 checkpoint 中的 RunEvent；
- 第 13 章 TaskService：唯一业务入口。

调用流程
--------
browser/CLI -> POST /api/v1/tasks -> TaskService -> checkpoint
browser/CLI -> POST resume/cancel -> TaskService -> LangGraph Command/update_state
browser -> WS events?after_sequence=N -> checkpoint events -> JSON event stream。

边界
----
这是单进程本地 API，没有鉴权、TLS、跨实例消息总线或生产级限流。不要将它直接暴露
到公共网络；生产部署必须在本层之外增加认证、反向代理和共享事件基础设施。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, model_validator

from realsight.application.task_service import TaskNotFoundError, TaskService
from realsight.contracts import Observation


class CreateTaskRequest(BaseModel):
    """创建 USB-C 教学任务的最小输入；型号本身在后续 interrupt 中提供。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    charger_target_id: str = Field(default="charger-api-demo", min_length=1, max_length=128)
    laptop_target_id: str = Field(default="laptop-api-demo", min_length=1, max_length=128)
    intent: str = Field(
        default="判断 USB-C 充电器与笔记本的已知兼容条件", min_length=1, max_length=1024
    )

    @model_validator(mode="after")
    def target_ids_differ(self) -> CreateTaskRequest:
        """两个现实对象必须不同，否则 Evidence.target_id 的边界没有意义。"""

        if self.charger_target_id == self.laptop_target_id:
            raise ValueError("charger_target_id and laptop_target_id must differ")
        return self


class ResumeTaskRequest(BaseModel):
    """恢复当前唯一 interrupt 的两种 API 载荷。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=128)
    kind: Literal["observation", "laptop_model"]
    observation: Observation | None = None
    laptop_model: str | None = Field(default=None, min_length=1, max_length=1024)
    source_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def payload_matches_kind(self) -> ResumeTaskRequest:
        """在 HTTP 边界拒绝混合载荷，工作流仍会再次验证 request_id 和目标。"""

        if self.kind == "observation":
            if self.observation is None or self.laptop_model is not None:
                raise ValueError("observation resume requires only observation")
        elif self.laptop_model is None or self.source_id is None or self.observation is not None:
            raise ValueError("laptop_model resume requires laptop_model and source_id")
        return self

    def workflow_payload(self) -> dict[str, Any]:
        """将 API DTO 显式变为第 13 章 ResumePayload，而不是透传原始 body。"""

        if self.kind == "observation":
            assert self.observation is not None
            return {"kind": "observation", "observation": self.observation.model_dump(mode="json")}
        assert self.laptop_model is not None and self.source_id is not None
        return {
            "kind": "laptop_model",
            "value": self.laptop_model,
            "source_id": self.source_id,
        }


class CancelTaskRequest(BaseModel):
    """取消原因会写入审计事件；它不应包含密钥或原始图像内容。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="cancelled_by_user", min_length=1, max_length=512)


def _project_root() -> Path:
    """定位仓库根目录，供默认离线 API 创建正式组件而非 examples 代码。"""

    return Path(__file__).resolve().parents[4]


def _state_response(service: TaskService, session_id: str) -> dict[str, Any]:
    """将内部 Pydantic 状态收敛为 API 安全摘要，不返回运行时连接或密钥。"""

    state = service.get_task(session_id)
    return {
        "session_id": state.session.session_id,
        "status": state.session.status.value,
        "charger_target_id": state.target.target_id,
        "laptop_target_id": state.laptop_target.target_id,
        "missing_fields": list(state.missing_fields),
        "pending_action": (
            state.pending_actions[0].model_dump(mode="json") if state.pending_actions else None
        ),
        "interrupt_id": service.current_interrupt_id(session_id),
        "lookup_status": state.lookup_status.value if state.lookup_status else None,
        "compatibility": (
            state.compatibility_result.model_dump(mode="json")
            if state.compatibility_result is not None
            else None
        ),
        "final_answer": state.final_answer,
        "event_count": len(state.events),
    }


def _http_error(exc: Exception) -> HTTPException:
    """不暴露 Python 栈信息，仅给客户端可恢复的 404 或 409 语义。"""

    if isinstance(exc, TaskNotFoundError):
        return HTTPException(status_code=404, detail="task session was not found")
    return HTTPException(status_code=409, detail=str(exc))


def create_app(service: TaskService | None = None) -> FastAPI:
    """创建可注入 TaskService 的 FastAPI 应用，测试可替换为回放服务。"""

    active_service = service or TaskService.with_memory(
        TaskService.default_dependencies(_project_root())
    )
    app = FastAPI(title="RealSight Teaching API", version="0.1.0")

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(request: CreateTaskRequest) -> dict[str, Any]:
        """在工作线程执行同步图，返回第一个暂停或自动回放后的状态。"""

        try:
            state = await asyncio.to_thread(
                active_service.create_task,
                session_id=request.session_id,
                charger_target_id=request.charger_target_id,
                laptop_target_id=request.laptop_target_id,
                intent=request.intent,
            )
            return _state_response(active_service, state.session.session_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _http_error(exc) from exc

    @app.get("/api/v1/tasks/{session_id}")
    async def get_task(session_id: str) -> dict[str, Any]:
        """读取当前 checkpoint 状态，不触发规划或外部服务调用。"""

        try:
            return await asyncio.to_thread(_state_response, active_service, session_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/tasks/{session_id}/resume")
    async def resume_task(session_id: str, request: ResumeTaskRequest) -> dict[str, Any]:
        """恢复当前 interrupt；第 13 章会再次校验请求 ID、目标和视角。"""

        try:
            state = await asyncio.to_thread(
                active_service.resume_task,
                session_id=session_id,
                interrupt_id=request.interrupt_id,
                payload=request.workflow_payload(),
            )
            return _state_response(active_service, state.session.session_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/tasks/{session_id}/cancel")
    async def cancel_task(session_id: str, request: CancelTaskRequest) -> dict[str, Any]:
        """取消任务并在有 C++ provider 时尽力转发 Cancel RPC。"""

        try:
            state = await asyncio.to_thread(
                active_service.cancel_task, session_id=session_id, reason=request.reason
            )
            return _state_response(active_service, state.session.session_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise _http_error(exc) from exc

    @app.websocket("/api/v1/tasks/{session_id}/events")
    async def task_events(websocket: WebSocket, session_id: str, after_sequence: int = 0) -> None:
        """从 checkpoint 补发事件并轮询新事件；单进程 MVP 不需要额外消息代理。"""

        await websocket.accept()
        last_sequence = max(after_sequence, 0)
        try:
            while True:
                try:
                    state = await asyncio.to_thread(active_service.get_task, session_id)
                except TaskNotFoundError:
                    await websocket.close(code=4404, reason="task not found")
                    return
                for event in state.events:
                    if event.sequence > last_sequence:
                        await websocket.send_json(event.model_dump(mode="json"))
                        last_sequence = event.sequence
                if state.session.status.value in {"completed", "cancelled", "failed"}:
                    await websocket.close(code=1000)
                    return
                await asyncio.sleep(0.15)
        except WebSocketDisconnect:
            return

    return app


# 供 ``uvicorn realsight.application.api:app`` 与 OpenAPI 工具使用的离线默认应用。
app = create_app()


__all__ = ["app", "create_app"]
