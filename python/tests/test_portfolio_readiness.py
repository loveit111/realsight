"""
真实 OCR 接口、正式配置与治理预算的作品集就绪测试。

这些测试刻意不安装或下载 PaddleOCR 模型：适配器通过结构化假 pipeline 验证第三方结果
转换，完整视觉归一化继续使用正式 ``VisionEvidenceAgent``。治理测试使用真正 LangGraph
interrupt/checkpoint，确保越权和预算耗尽进入 FAILED 终态，而不是只测试一个孤立计数器。
真实摄像头和模型测试使用 ``integration`` marker 另行运行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import realsight.application.serve as serve_module
from realsight.application.serve import (
    _observation_provider_from_settings,
    _recognizer_from_settings,
)
from realsight.application.task_service import TaskService
from realsight.compatibility import LocalSpecificationCatalog, UsbCCompatibilityRules
from realsight.config import load_settings
from realsight.contracts import Action, RunEventType
from realsight.governance import GovernanceUsage, GovernanceViolation
from realsight.vision import (
    PaddleOcrTextRecognizer,
    RecognitionFailure,
    VisionEvidenceAgent,
)
from realsight.workflow import (
    DeterministicPlanner,
    MainAgentDependencies,
    MainAgentState,
    build_main_agent_graph,
    get_active_interrupt,
    get_state,
    graph_config,
    initial_state,
    resume_main_agent,
)
from realsight.workflow.replay import (
    GrpcObservationProvider,
    ReplayObservationProvider,
    ScriptedLabelRecognizer,
    UnavailableTextRecognizer,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = PROJECT_ROOT / "test-data" / "ch12" / "laptop-specifications.json"


class FakeResult:
    """模拟 PaddleOCR 3.x Result.json。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.json = {"res": payload}


class FakePipeline:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.calls = 0

    def predict(self, input_path: str) -> tuple[FakeResult, ...]:
        assert input_path
        self.calls += 1
        return (self.result,)


class ExplodingPipeline:
    def predict(self, input_path: str) -> tuple[FakeResult, ...]:
        raise RuntimeError(f"backend failed for {input_path}")


def write_image(tmp_path: Path) -> Path:
    path = tmp_path / "label.ppm"
    path.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    return path


def paddle_payload(
    *,
    text: str = "Output: 20V=3.25A 65W USB PD",
    score: float = 0.96,
) -> dict[str, Any]:
    return {
        "rec_texts": [text],
        "rec_scores": [score],
        "rec_polys": [[[10, 20], [90, 20], [90, 40], [10, 40]]],
    }


def dependencies() -> MainAgentDependencies:
    return MainAgentDependencies(
        planner=DeterministicPlanner(),
        catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
        vision_agent=VisionEvidenceAgent(ScriptedLabelRecognizer()),
        rules=UsbCCompatibilityRules(),
    )


def waiting_observation_graph(
    usage: GovernanceUsage,
) -> tuple[Any, str]:
    state = initial_state(
        session_id=f"session-{usage.policy_id}",
        charger_target_id=f"charger-{usage.policy_id}",
        laptop_target_id=f"laptop-{usage.policy_id}",
        governance_usage=usage,
    )
    graph, _ = build_main_agent_graph(dependencies())
    graph.invoke(state, config=graph_config(state.session.thread_id))
    return graph, state.session.thread_id


def test_paddle_adapter_normalizes_regions_and_preserves_provider_metadata(
    tmp_path: Path,
) -> None:
    pipeline = FakePipeline(FakeResult(paddle_payload()))
    recognizer = PaddleOcrTextRecognizer(
        pipeline=pipeline,
        image_size_reader=lambda _: (100, 100),
        package_version="3.7.0-test",
    )
    document = recognizer.recognize(
        write_image(tmp_path), observation_id="observation-ocr-test"
    )

    assert pipeline.calls == 1
    assert len(document.regions) == 1
    region = document.regions[0]
    assert (region.left, region.top, region.right, region.bottom) == (
        0.1,
        0.2,
        0.9,
        0.4,
    )
    assert document.provider_metadata["provider"] == "paddleocr"
    assert document.provider_metadata["package_version"] == "3.7.0-test"


def test_paddle_metadata_reaches_final_evidence(tmp_path: Path) -> None:
    image_path = write_image(tmp_path)
    recognizer = PaddleOcrTextRecognizer(
        pipeline=FakePipeline(FakeResult(paddle_payload())),
        image_size_reader=lambda _: (100, 100),
        package_version="3.7.0-test",
    )
    provider = ReplayObservationProvider(image_path, quality_score=0.95)
    state = initial_state(
        session_id="session-ocr-evidence",
        charger_target_id="charger-ocr-evidence",
        laptop_target_id="laptop-ocr-evidence",
    )
    graph, _ = build_main_agent_graph(
        MainAgentDependencies(
            planner=DeterministicPlanner(),
            catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
            vision_agent=VisionEvidenceAgent(recognizer),
            rules=UsbCCompatibilityRules(),
        )
    )
    graph.invoke(state, config=graph_config(state.session.thread_id))
    waiting = get_state(graph, state.session.thread_id)
    assert waiting.pending_request is not None
    observation = provider.observe(waiting.pending_request)
    resumed = resume_main_agent(
        graph,
        thread_id=state.session.thread_id,
        interrupt_id=get_active_interrupt(graph, state.session.thread_id).id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )

    power = resumed.belief.confirmed["charger_max_power_w"]
    recognizer_metadata = power.source_metadata["recognizer"]
    assert isinstance(recognizer_metadata, dict)
    assert recognizer_metadata["provider"] == "paddleocr"


def test_paddle_adapter_returns_empty_document_for_no_text(tmp_path: Path) -> None:
    recognizer = PaddleOcrTextRecognizer(
        pipeline=FakePipeline(
            FakeResult({"rec_texts": [], "rec_scores": [], "rec_polys": []})
        ),
        image_size_reader=lambda _: (100, 100),
    )
    document = recognizer.recognize(
        write_image(tmp_path), observation_id="observation-empty-ocr"
    )
    assert document.regions == ()


def test_paddle_adapter_rejects_mismatched_result_lengths(tmp_path: Path) -> None:
    recognizer = PaddleOcrTextRecognizer(
        pipeline=FakePipeline(
            FakeResult(
                {
                    "rec_texts": ["65W"],
                    "rec_scores": [],
                    "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
                }
            )
        ),
        image_size_reader=lambda _: (100, 100),
    )
    with pytest.raises(RecognitionFailure, match="lengths differ"):
        recognizer.recognize(
            write_image(tmp_path), observation_id="observation-invalid-ocr"
        )


def test_paddle_adapter_wraps_backend_failure(tmp_path: Path) -> None:
    recognizer = PaddleOcrTextRecognizer(
        pipeline=ExplodingPipeline(),
        image_size_reader=lambda _: (100, 100),
    )
    with pytest.raises(RecognitionFailure, match="inference failed"):
        recognizer.recognize(
            write_image(tmp_path), observation_id="observation-failed-ocr"
        )


def test_paddle_adapter_rejects_collapsed_coordinates(tmp_path: Path) -> None:
    payload = paddle_payload()
    payload["rec_polys"] = [[[200, 10], [210, 10], [210, 20], [200, 20]]]
    recognizer = PaddleOcrTextRecognizer(
        pipeline=FakePipeline(FakeResult(payload)),
        image_size_reader=lambda _: (100, 100),
    )
    with pytest.raises(RecognitionFailure, match="collapses"):
        recognizer.recognize(
            write_image(tmp_path), observation_id="observation-bad-coordinates"
        )


def test_low_ocr_confidence_cannot_become_evidence(tmp_path: Path) -> None:
    image_path = write_image(tmp_path)
    recognizer = PaddleOcrTextRecognizer(
        pipeline=FakePipeline(FakeResult(paddle_payload(score=0.20))),
        image_size_reader=lambda _: (100, 100),
    )
    provider = ReplayObservationProvider(image_path, quality_score=0.95)
    state = initial_state(
        session_id="session-low-ocr",
        charger_target_id="charger-low-ocr",
        laptop_target_id="laptop-low-ocr",
    )
    graph, _ = build_main_agent_graph(
        MainAgentDependencies(
            planner=DeterministicPlanner(),
            catalog=LocalSpecificationCatalog.from_json_file(CATALOG_PATH),
            vision_agent=VisionEvidenceAgent(recognizer),
            rules=UsbCCompatibilityRules(),
        )
    )
    graph.invoke(state, config=graph_config(state.session.thread_id))
    waiting = get_state(graph, state.session.thread_id)
    assert waiting.pending_request is not None
    observation = provider.observe(waiting.pending_request)
    resumed = resume_main_agent(
        graph,
        thread_id=state.session.thread_id,
        interrupt_id=get_active_interrupt(graph, state.session.thread_id).id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )
    assert "charger_max_power_w" not in resumed.belief.confirmed
    assert "charger_max_power_w" in resumed.belief.unknown
    assert resumed.session.status.value == "waiting_observation"


def test_real_runtime_configuration_is_typed_and_overridable(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.toml"
    config_path.write_text(
        """
[runtime]
data_dir = "data"
log_level = "INFO"
[agent]
max_iterations = 7
[perception]
provider = "grpc"
address = "127.0.0.1:50052"
[vision]
provider = "paddleocr"
engine = "onnxruntime"
ocr_version = "PP-OCRv6"
[governance]
policy_id = "portfolio-test"
allowed_capabilities = ["perception.observe", "rules.usb_c"]
max_commands = 5
max_observations = 2
max_external_attempts = 6
max_cost_units = 3
""",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path,
        environ={"REALSIGHT_PERCEPTION_ADDRESS": "localhost:50100"},
    )
    assert settings.perception.provider == "grpc"
    assert settings.perception.address == "localhost:50100"
    assert settings.vision.provider == "paddleocr"
    assert settings.agent.max_iterations == 7
    assert settings.governance.max_commands == 5
    assert settings.governance.max_observations == 2
    assert settings.governance.max_external_attempts == 6
    assert settings.governance.max_cost_units == 3


def test_runtime_factories_cover_unavailable_and_real_provider_combinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(
        PROJECT_ROOT / "config" / "realsight.example.toml", environ={}
    )
    assert _observation_provider_from_settings(settings) is None
    assert isinstance(_recognizer_from_settings(settings), UnavailableTextRecognizer)

    class FakeOcr:
        def __init__(self, *, engine: str, ocr_version: str) -> None:
            self.engine = engine
            self.ocr_version = ocr_version

    monkeypatch.setattr(serve_module, "PaddleOcrTextRecognizer", FakeOcr)
    real_settings = settings.model_copy(
        update={
            "perception": settings.perception.model_copy(update={"provider": "grpc"}),
            "vision": settings.vision.model_copy(update={"provider": "paddleocr"}),
        }
    )
    provider = _observation_provider_from_settings(real_settings)
    recognizer = _recognizer_from_settings(real_settings)
    assert isinstance(provider, GrpcObservationProvider)
    assert isinstance(recognizer, FakeOcr)
    assert recognizer.engine == "onnxruntime"
    assert recognizer.ocr_version == "PP-OCRv6"
    provider.close()


def test_denied_perception_capability_becomes_failed_terminal() -> None:
    usage = GovernanceUsage(
        policy_id="deny-perception",
        allowed_capabilities=frozenset({"rules.usb_c"}),
    )
    graph, thread_id = waiting_observation_graph(usage)
    failed = get_state(graph, thread_id)

    assert failed.session.status.value == "failed"
    assert failed.pending_actions == ()
    assert failed.events[-1].event_type is RunEventType.TASK_FAILED
    assert failed.events[-1].data["code"] == "capability_denied"


def test_external_attempt_budget_stops_before_ocr(tmp_path: Path) -> None:
    usage = GovernanceUsage(
        policy_id="external-one",
        max_external_attempts=1,
    )
    graph, thread_id = waiting_observation_graph(usage)
    waiting = get_state(graph, thread_id)
    assert waiting.pending_request is not None
    observation = ReplayObservationProvider(write_image(tmp_path)).observe(
        waiting.pending_request
    )
    failed = resume_main_agent(
        graph,
        thread_id=thread_id,
        interrupt_id=get_active_interrupt(graph, thread_id).id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )

    assert failed.session.status.value == "failed"
    assert failed.events[-1].data["code"] == "max_external_attempts_exceeded"


def test_iteration_budget_prevents_cross_resume_loop(tmp_path: Path) -> None:
    usage = GovernanceUsage(policy_id="one-iteration", max_iterations=1)
    graph, thread_id = waiting_observation_graph(usage)
    waiting = get_state(graph, thread_id)
    assert waiting.pending_request is not None
    observation = ReplayObservationProvider(write_image(tmp_path)).observe(
        waiting.pending_request
    )
    failed = resume_main_agent(
        graph,
        thread_id=thread_id,
        interrupt_id=get_active_interrupt(graph, thread_id).id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )

    assert failed.session.status.value == "failed"
    assert failed.events[-1].data["code"] == "max_iterations_exceeded"
    with pytest.raises(ValueError, match="failed"):
        resume_main_agent(
            graph,
            thread_id=thread_id,
            interrupt_id="consumed-interrupt",
            payload={"kind": "laptop_model", "value": "x", "source_id": "x"},
        )


def test_command_budget_is_enforced_by_the_graph(tmp_path: Path) -> None:
    usage = GovernanceUsage(policy_id="one-command", max_commands=1)
    graph, thread_id = waiting_observation_graph(usage)
    waiting = get_state(graph, thread_id)
    assert waiting.pending_request is not None
    observation = ReplayObservationProvider(write_image(tmp_path)).observe(
        waiting.pending_request
    )
    failed = resume_main_agent(
        graph,
        thread_id=thread_id,
        interrupt_id=get_active_interrupt(graph, thread_id).id,
        payload={
            "kind": "observation",
            "observation": observation.model_dump(mode="json"),
        },
    )
    assert failed.session.status.value == "failed"
    assert failed.events[-1].data["code"] == "max_commands_exceeded"


def test_exhausted_command_budget_stops_before_planner_call() -> None:
    class MustNotRunPlanner:
        def plan(self, state: MainAgentState) -> Action:
            raise AssertionError(
                f"planner unexpectedly called for {state.session.session_id}"
            )

    base = dependencies()
    state = initial_state(
        session_id="session-command-precheck",
        charger_target_id="charger-command-precheck",
        laptop_target_id="laptop-command-precheck",
        governance_usage=GovernanceUsage(
            policy_id="command-precheck",
            max_commands=1,
            commands_used=1,
        ),
    )
    graph, _ = build_main_agent_graph(
        MainAgentDependencies(
            planner=MustNotRunPlanner(),
            catalog=base.catalog,
            vision_agent=base.vision_agent,
            rules=base.rules,
        )
    )
    graph.invoke(state, config=graph_config(state.session.thread_id))
    failed = get_state(graph, state.session.thread_id)
    assert failed.events[-1].data["code"] == "max_commands_exceeded"


def test_model_cost_budget_is_enforced_before_planner_call() -> None:
    base = dependencies()
    governed_dependencies = MainAgentDependencies(
        planner=base.planner,
        catalog=base.catalog,
        vision_agent=base.vision_agent,
        rules=base.rules,
        planner_cost_units=2,
    )
    state = initial_state(
        session_id="session-model-cost",
        charger_target_id="charger-model-cost",
        laptop_target_id="laptop-model-cost",
        governance_usage=GovernanceUsage(policy_id="one-cost", max_cost_units=1),
    )
    graph, _ = build_main_agent_graph(governed_dependencies)
    graph.invoke(state, config=graph_config(state.session.thread_id))
    failed = get_state(graph, state.session.thread_id)
    assert failed.session.status.value == "failed"
    assert failed.events[-1].data["code"] == "max_cost_units_exceeded"


def test_observation_budget_is_enforced_atomically() -> None:
    usage = GovernanceUsage(policy_id="one-observation", max_observations=1)
    once = usage.consume(observations=1)
    with pytest.raises(GovernanceViolation) as error:
        once.consume(observations=1)
    assert error.value.code == "max_observations_exceeded"
    assert once.observations_used == 1


def test_automatic_observation_loop_ends_in_governed_failure(tmp_path: Path) -> None:
    base = dependencies()
    incomplete_dependencies = MainAgentDependencies(
        planner=base.planner,
        catalog=base.catalog,
        vision_agent=VisionEvidenceAgent(UnavailableTextRecognizer()),
        rules=base.rules,
    )
    service = TaskService.with_memory(
        incomplete_dependencies,
        observation_provider=ReplayObservationProvider(write_image(tmp_path)),
        governance_usage=GovernanceUsage(
            policy_id="two-observations",
            max_observations=2,
        ),
    )
    failed = service.create_task(
        session_id="session-auto-budget",
        charger_target_id="charger-auto-budget",
        laptop_target_id="laptop-auto-budget",
        intent="test governed automatic observations",
    )
    assert failed.session.status.value == "failed"
    assert failed.events[-1].data["code"] == "max_observations_exceeded"
    assert failed.governance_usage.observations_used == 2


def test_governance_consume_is_atomic() -> None:
    usage = GovernanceUsage(policy_id="atomic", max_commands=1)
    once = usage.consume(commands=1)
    with pytest.raises(GovernanceViolation, match="exhausted"):
        once.consume(commands=1, external_attempts=1)
    assert once.commands_used == 1
    assert once.external_attempts_used == 0
