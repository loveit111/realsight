"""
第 13 章主 Agent 完整循环测试。

文件逻辑
--------
测试用真正的 LangGraph interrupt/checkpoint 和第 11、12 章正式模块验证：主流程
先等待标签观察，再等待用户型号，随后才检索资料并执行规则。还验证 OpenAIPlanner
即使由真实模型选择工具，也会被转换为既有 Action，模型不能直接返回兼容性文本。

技术栈
------
- pytest 9 与 tmp_path；
- LangGraph InMemorySaver 和 interrupt/resume；
- Pydantic Observation/Evidence 契约；
- OpenAIPlanner 的注入式假客户端，不联网、不需要 API Key。

调用流程
--------
pytest -> build graph -> pause -> resume Observation -> pause -> resume laptop model
    -> assert rule result / final answer / events；
pytest -> fake Responses API function call -> assert constrained Action。

边界
----
测试使用教学型号和脚本 OCR，只验证工程控制流与证据边界，不验证真实视觉识别或
真实硬件兼容性。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.contracts import ActionType
from realsight.vision import VisionEvidenceAgent
from realsight.workflow import (
    DeterministicPlanner,
    MainAgentDependencies,
    OpenAIPlanner,
    build_main_agent_graph,
    cancel_main_agent,
    get_active_interrupt,
    get_state,
    graph_config,
    initial_state,
    resume_main_agent,
)
from realsight.workflow.replay import ReplayObservationProvider, ScriptedLabelRecognizer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


def write_image(tmp_path: Path) -> Path:
    """写入一个最小 PPM artifact；VisionEvidenceAgent 会检查该路径真实存在。"""

    path = tmp_path / "back-label.ppm"
    path.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    return path


def dependencies() -> MainAgentDependencies:
    """测试不调用网络，依然使用正式视觉、目录和规则实现。"""

    return MainAgentDependencies(
        planner=DeterministicPlanner(),
        catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
        vision_agent=VisionEvidenceAgent(ScriptedLabelRecognizer()),
        rules=UsbCCompatibilityRules(),
    )


def test_main_agent_pauses_twice_then_completes_rule_bound_answer(
    tmp_path: Path,
) -> None:
    """观察和用户型号都必须经过 interrupt；规则结果到位后才允许 completed。"""

    state = initial_state(
        session_id="session-ch13-test",
        charger_target_id="charger-ch13-test",
        laptop_target_id="laptop-ch13-test",
    )
    graph, _ = build_main_agent_graph(dependencies())
    graph.invoke(state, config=graph_config(state.session.thread_id))

    waiting_observation = get_state(graph, state.session.thread_id)
    observation_interrupt = get_active_interrupt(graph, state.session.thread_id)
    assert waiting_observation.session.status.value == "waiting_observation"
    assert waiting_observation.pending_request is not None

    observation = ReplayObservationProvider(write_image(tmp_path)).observe(
        waiting_observation.pending_request
    )
    waiting_user = resume_main_agent(
        graph,
        thread_id=state.session.thread_id,
        interrupt_id=observation_interrupt.id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )
    assert waiting_user.session.status.value == "waiting_user"
    assert {"charger_max_power_w", "charger_protocol"} <= set(
        waiting_user.belief.confirmed
    )

    user_interrupt = get_active_interrupt(graph, state.session.thread_id)
    completed = resume_main_agent(
        graph,
        thread_id=state.session.thread_id,
        interrupt_id=user_interrupt.id,
        payload={
            "kind": "laptop_model",
            "value": "ExampleBook 13",
            "source_id": "user-ch13-test",
        },
    )
    assert completed.session.status.value == "completed"
    assert completed.compatibility_result is not None
    assert completed.compatibility_result.verdict.value == "conditions_met"
    assert completed.final_answer is not None
    assert "conditions_met" in completed.final_answer
    assert completed.laptop_model_evidence is not None
    assert completed.laptop_model_evidence.target_id == "laptop-ch13-test"
    assert all(
        event.sequence == index + 1 for index, event in enumerate(completed.events)
    )


def test_wrong_observation_resume_keeps_interrupt_available(tmp_path: Path) -> None:
    """恢复验证在 Command 之前进行，错误 request_id 不会消费正确的 checkpoint。"""

    state = initial_state(
        session_id="session-ch13-invalid",
        charger_target_id="charger-ch13-invalid",
        laptop_target_id="laptop-ch13-invalid",
    )
    graph, _ = build_main_agent_graph(dependencies())
    graph.invoke(state, config=graph_config(state.session.thread_id))
    waiting = get_state(graph, state.session.thread_id)
    active = get_active_interrupt(graph, state.session.thread_id)
    assert waiting.pending_request is not None
    wrong = ReplayObservationProvider(write_image(tmp_path)).observe(
        waiting.pending_request
    )
    wrong = wrong.model_copy(update={"request_id": "other-request"})

    with pytest.raises(ValueError, match="request_id"):
        resume_main_agent(
            graph,
            thread_id=state.session.thread_id,
            interrupt_id=active.id,
            payload={
                "kind": "observation",
                "observation": wrong.model_dump(mode="json"),
            },
        )
    assert get_active_interrupt(graph, state.session.thread_id).id == active.id


def test_cancelled_task_rejects_future_resume() -> None:
    """取消写入状态后，旧页面带来的恢复请求不能重新启动任务。"""

    state = initial_state(
        session_id="session-ch13-cancel",
        charger_target_id="charger-ch13-cancel",
        laptop_target_id="laptop-ch13-cancel",
    )
    graph, _ = build_main_agent_graph(dependencies())
    graph.invoke(state, config=graph_config(state.session.thread_id))
    active = get_active_interrupt(graph, state.session.thread_id)
    cancelled = cancel_main_agent(
        graph, thread_id=state.session.thread_id, reason="user_cancelled"
    )
    assert cancelled.session.status.value == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        resume_main_agent(
            graph,
            thread_id=state.session.thread_id,
            interrupt_id=active.id,
            payload={
                "kind": "laptop_model",
                "value": "ExampleBook 13",
                "source_id": "x",
            },
        )


def test_openai_planner_translates_function_call_to_existing_action() -> None:
    """注入假 Responses 返回值，验证模型只能从当前允许工具中形成 Action。"""

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        def create(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            call = SimpleNamespace(
                type="function_call",
                name="ask_user",
                arguments='{"reason":"需要笔记本型号","question":"请提供完整笔记本型号"}',
            )
            return SimpleNamespace(output=(call,))

    responses = FakeResponses()
    planner = OpenAIPlanner(client=SimpleNamespace(responses=responses))
    state = initial_state(
        session_id="session-ch13-openai",
        charger_target_id="charger-ch13-openai",
        laptop_target_id="laptop-ch13-openai",
    )
    action = planner.plan(state)

    assert action.action_type is ActionType.ASK_USER
    assert responses.kwargs is not None
    assert "tools" in responses.kwargs
