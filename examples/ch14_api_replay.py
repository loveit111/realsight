"""
第 14 章最小 API Demo：用 FastAPI TestClient 演示任务、恢复和事件流。

文件逻辑
--------
Demo 不启动真实摄像头或公网服务器。它把第 13 章回放 ObservationProvider 注入
TaskService，再通过真实 HTTP 路由创建任务。回放器自动完成背面标签观察，API
返回笔记本型号 interrupt；脚本随后调用 resume，读取最终规则结论，并订阅一次
WebSocket RunEvent 重放。

技术栈
------
- FastAPI TestClient 与 WebSocket；
- Python 3.12 tempfile/pathlib/json；
- 第 13 章 TaskService、ReplayObservationProvider、LangGraph checkpoint；
- 第 11、12 章视觉 Evidence、规格目录与 USB-C 规则。

调用流程
--------
POST /tasks -> 回放 Observation -> waiting_user
    -> WS /events?after_sequence=0 -> 第一条 RunEvent
    -> POST /resume(laptop_model) -> completed -> JSON summary。

边界
----
TestClient 证明 API 合约和事件顺序，不能代替真实 Uvicorn 网络部署、浏览器 UI、
认证或 C++ 摄像头联调。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from realsight.application.api import create_app
from realsight.application.task_service import TaskService
from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.vision import VisionEvidenceAgent
from realsight.workflow import DeterministicPlanner, MainAgentDependencies
from realsight.workflow.replay import ReplayObservationProvider, ScriptedLabelRecognizer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_image(directory: Path) -> Path:
    """创建真实的最小图片 artifact，确保回放器和视觉层会检查文件路径。"""

    image_path = directory / "api-back-label.ppm"
    image_path.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")
    return image_path


def main() -> int:
    """通过 HTTP 和 WebSocket 驱动离线服务，检查最终结论不越过规则边界。"""

    with tempfile.TemporaryDirectory(prefix="realsight-ch14-") as temporary_directory:
        image_path = create_image(Path(temporary_directory))
        dependencies = MainAgentDependencies(
            planner=DeterministicPlanner(),
            catalog=LocalSpecificationCatalog.from_json_file(
                PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"
            ),
            vision_agent=VisionEvidenceAgent(ScriptedLabelRecognizer()),
            rules=UsbCCompatibilityRules(),
        )
        service = TaskService.with_memory(
            dependencies, observation_provider=ReplayObservationProvider(image_path)
        )
        client = TestClient(create_app(service))
        created = client.post(
            "/api/v1/tasks",
            json={
                "session_id": "session-ch14-demo",
                "charger_target_id": "charger-ch14-demo",
                "laptop_target_id": "laptop-ch14-demo",
            },
        )
        created.raise_for_status()
        waiting = created.json()
        assert waiting["status"] == "waiting_user"
        assert waiting["interrupt_id"] is not None

        # WebSocket 从 sequence 0 开始补发，客户端可以在断线后改用最后收到的序号续接。
        with client.websocket_connect(
            "/api/v1/tasks/session-ch14-demo/events?after_sequence=0"
        ) as websocket:
            first_event = websocket.receive_json()
        completed = client.post(
            "/api/v1/tasks/session-ch14-demo/resume",
            json={
                "interrupt_id": waiting["interrupt_id"],
                "kind": "laptop_model",
                "laptop_model": "ExampleBook 13",
                "source_id": "user-ch14-demo",
            },
        )
        completed.raise_for_status()
        result = completed.json()

    summary = {
        "first_event_type": first_event["event_type"],
        "status": result["status"],
        "verdict": result["compatibility"]["verdict"],
        "event_count": result["event_count"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "completed" or summary["verdict"] != "conditions_met":
        return 1
    print("CH14_DEMO_OK http=created,resumed websocket=event-replay")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
