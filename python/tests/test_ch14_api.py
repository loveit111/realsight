"""
第 14 章 FastAPI、WebSocket 和取消任务测试。

文件逻辑
--------
测试创建带回放 ObservationProvider 的真实 TaskService，并使用 FastAPI TestClient
验证 HTTP 任务创建、WebSocket 事件补发、用户恢复后的规则结论。第二组测试使用
无 provider 服务，保证取消等待中的任务后，旧 interrupt 再也不能恢复。

技术栈
------
- pytest 9、tmp_path；
- FastAPI TestClient/Starlette WebSocket 测试；
- asyncio.to_thread 所包装的同步 LangGraph 工作流；
- 第 11、12、13 章正式组件与教学回放夹具。

调用流程
--------
pytest -> POST create -> replay observation -> waiting_user
    -> WS events replay -> POST resume -> completed
pytest -> POST create -> POST cancel -> POST old resume -> 409。

边界
----
这些是单进程 API 合约测试，不启动 Uvicorn、不访问 OpenAI 或 C++，也不验证公网
网络、认证、TLS 或多实例消息队列。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from realsight.application.api import create_app
from realsight.application.task_service import TaskService
from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.vision import VisionEvidenceAgent
from realsight.workflow import DeterministicPlanner, MainAgentDependencies
from realsight.workflow.replay import ReplayObservationProvider, ScriptedLabelRecognizer
from starlette.websockets import WebSocketDisconnect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


def write_image(tmp_path: Path) -> Path:
    """写入真正存在的 PPM artifact，以便视觉层继续执行其文件边界检查。"""

    image_path = tmp_path / "api-label.ppm"
    image_path.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")
    return image_path


def dependencies() -> MainAgentDependencies:
    """构造完全离线的正式工作流依赖，不将测试改写为手工伪造规则结果。"""

    return MainAgentDependencies(
        planner=DeterministicPlanner(),
        catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
        vision_agent=VisionEvidenceAgent(ScriptedLabelRecognizer()),
        rules=UsbCCompatibilityRules(),
    )


def test_api_replays_events_resumes_and_returns_rule_bound_result(
    tmp_path: Path,
) -> None:
    """创建任务后自动回放标签观察，WebSocket 可重放事件，型号恢复后才会完成。"""

    service = TaskService.with_memory(
        dependencies(),
        observation_provider=ReplayObservationProvider(write_image(tmp_path)),
    )
    client = TestClient(create_app(service))
    created = client.post(
        "/api/v1/tasks",
        json={
            "session_id": "session-ch14-api",
            "charger_target_id": "charger-ch14-api",
            "laptop_target_id": "laptop-ch14-api",
        },
    )
    assert created.status_code == 201
    waiting = created.json()
    assert waiting["status"] == "waiting_user"
    assert waiting["interrupt_id"]
    assert waiting["compatibility"] is None

    with client.websocket_connect(
        "/api/v1/tasks/session-ch14-api/events?after_sequence=1"
    ) as websocket:
        event = websocket.receive_json()
    assert event["sequence"] == 2
    assert event["event_type"] == "action_required"

    completed = client.post(
        "/api/v1/tasks/session-ch14-api/resume",
        json={
            "interrupt_id": waiting["interrupt_id"],
            "kind": "laptop_model",
            "laptop_model": "ExampleBook 13",
            "source_id": "user-ch14-api",
        },
    )
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "completed"
    assert body["compatibility"]["verdict"] == "conditions_met"
    assert body["interrupt_id"] is None
    assert "条件" in body["final_answer"]

    with (
        client.websocket_connect(
            f"/api/v1/tasks/session-ch14-api/events?after_sequence={body['event_count']}"
        ) as websocket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        websocket.receive_json()
    assert closed.value.code == 1000


def test_api_cancel_rejects_old_resume() -> None:
    """取消等待 Observation 的会话后，来自旧界面的 resume 被拒绝为 409。"""

    service = TaskService.with_memory(dependencies())
    client = TestClient(create_app(service))
    created = client.post(
        "/api/v1/tasks",
        json={
            "session_id": "session-ch14-cancel",
            "charger_target_id": "charger-ch14-cancel",
            "laptop_target_id": "laptop-ch14-cancel",
        },
    )
    waiting = created.json()
    assert waiting["status"] == "waiting_observation"

    cancelled = client.post(
        "/api/v1/tasks/session-ch14-cancel/cancel",
        json={"reason": "test_cancel"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    rejected = client.post(
        "/api/v1/tasks/session-ch14-cancel/resume",
        json={
            "interrupt_id": waiting["interrupt_id"],
            "kind": "laptop_model",
            "laptop_model": "ExampleBook 13",
            "source_id": "late-client",
        },
    )
    assert rejected.status_code == 409
    assert "cancelled" in rejected.json()["detail"]
