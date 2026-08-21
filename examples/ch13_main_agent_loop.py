"""
第 13 章最小 Demo：可恢复的主 Agent 完整证据循环。

文件逻辑
--------
这个脚本用一张临时背面标签图片模拟第 10 章 C++ 关键帧，用脚本识别器模拟第
11 章 OCR，并使用第 12 章虚构教学目录。它刻意经历两次 LangGraph interrupt：
先暂停等待 Observation，再暂停等待用户提供笔记本型号。恢复后，目录检索和
USB-C 规则自动运行，最终输出带 Evidence ID 与未知边界的条件结论。

技术栈
------
- Python 3.12 tempfile/json/pathlib；
- LangGraph StateGraph、InMemory checkpoint、interrupt/resume；
- Pydantic contracts、VisionEvidenceAgent、LocalSpecificationCatalog；
- DeterministicPlanner 和 UsbCCompatibilityRules。

调用流程
--------
initial_state -> graph.invoke -> observation interrupt
    -> ReplayObservationProvider -> resume_main_agent -> laptop-model interrupt
    -> LaptopModelResume -> catalog lookup -> deterministic rules -> final answer。

边界
----
ExampleBook 13 与标签文本均为教学夹具。此 Demo 证明数据、暂停和规则链正确，
不能证明真实充电器、线缆或笔记本在物理世界一定可以充电。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.vision import VisionEvidenceAgent
from realsight.workflow import (
    DeterministicPlanner,
    MainAgentDependencies,
    build_main_agent_graph,
    get_active_interrupt,
    get_state,
    graph_config,
    initial_state,
    resume_main_agent,
)
from realsight.workflow.replay import ReplayObservationProvider, ScriptedLabelRecognizer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


def create_demo_image(directory: Path) -> Path:
    """创建一个真实存在的极小 PPM artifact，让视觉层的路径检查真正执行。"""

    image_path = directory / "charger-back-label.ppm"
    image_path.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")
    return image_path


def build_dependencies() -> MainAgentDependencies:
    """组装离线可运行的主 Agent 依赖；真实模型只需替换 planner。"""

    return MainAgentDependencies(
        planner=DeterministicPlanner(),
        catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
        vision_agent=VisionEvidenceAgent(ScriptedLabelRecognizer()),
        rules=UsbCCompatibilityRules(),
    )


def main() -> int:
    """运行两次恢复并打印最终受规则约束的状态摘要。"""

    state = initial_state(
        session_id="session-ch13-demo",
        charger_target_id="charger-ch13-demo",
        laptop_target_id="laptop-ch13-demo",
    )
    graph, _ = build_main_agent_graph(build_dependencies())
    config = graph_config(state.session.thread_id)

    # 第一次 invoke 只会规划并暂停，不在 wait 节点之前执行感知或 OCR。
    graph.invoke(state, config=config)
    waiting_observation = get_state(graph, state.session.thread_id)
    observation_interrupt = get_active_interrupt(graph, state.session.thread_id)
    assert waiting_observation.pending_request is not None

    with tempfile.TemporaryDirectory(prefix="realsight-ch13-") as temporary_directory:
        image_path = create_demo_image(Path(temporary_directory))
        observation = ReplayObservationProvider(image_path).observe(
            waiting_observation.pending_request
        )
        # 第二次调用会把 Observation 转为视觉 Evidence，随后在用户型号处再次暂停。
        waiting_user = resume_main_agent(
            graph,
            thread_id=state.session.thread_id,
            interrupt_id=observation_interrupt.id,
            payload={"kind": "observation", "observation": observation.model_dump(mode="json")},
        )

        user_interrupt = get_active_interrupt(graph, state.session.thread_id)
        completed = resume_main_agent(
            graph,
            thread_id=state.session.thread_id,
            interrupt_id=user_interrupt.id,
            payload={
                "kind": "laptop_model",
                "value": "ExampleBook 13",
                "source_id": "user-ch13-demo",
            },
        )

    summary = {
        "first_interrupt": observation_interrupt.id,
        "second_interrupt": user_interrupt.id,
        "waiting_user_status": waiting_user.session.status.value,
        "status": completed.session.status.value,
        "confirmed_charger_fields": sorted(completed.belief.confirmed),
        "lookup_status": completed.lookup_status.value if completed.lookup_status else None,
        "verdict": completed.compatibility_result.verdict.value,
        "used_evidence": list(completed.compatibility_result.used_evidence_ids),
        "answer": completed.final_answer,
        "event_count": len(completed.events),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "completed" or summary["verdict"] != "conditions_met":
        return 1
    print("CH13_DEMO_OK verdict=conditions_met interrupts=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
