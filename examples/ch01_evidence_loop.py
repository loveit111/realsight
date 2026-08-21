"""
第 1 章：用“证据闭环”判断一个 USB-C 充电器是否满足设备要求。

这个文件做什么
------------
它演示 RealSight 最底层、也最重要的业务骨架：系统不凭印象直接回答，而是先
列出判断所需证据，发现缺口后规划下一步观察/询问/检索动作，证据齐全后再运行
一条确定性的兼容规则，最后给出带来源和未知边界的回答。

主要逻辑
--------
1. 用 BeliefState 保存当前已经确认、未知或冲突的事实。
2. missing_evidence_fields() 找出 USB-C 判断还缺哪些字段。
3. plan_next_actions() 把缺口变成 request_view、ask_user、
   retrieve_knowledge 或 run_rules 等结构化 Action。
4. add_label_evidence() 模拟 OCR 后处理：保存原始标签文字，用正则表达式提取
   电压/电流档位，计算功率，并通过 derived_from 保留证据派生链。
5. evaluate_compatibility() 先做证据门禁，再检查接口、USB-PD 协议以及目标电压
   档位的功率是否达标。
6. build_evidence_answer() 把规则结果整理成人能读懂的结论、依据和限制。

实际使用的技术栈
----------------
- Python 标准库：dataclasses、Enum/类型提示（来自共享领域模块）。
- re：从标签文本中解析诸如 ``20V/3.25A`` 的输出档位。
- json：把 dataclass/Enum 转换后打印，便于观察中间状态。
- 确定性规则：功率使用 ``电压 × 电流`` 计算，不交给大模型猜测。

调用流程
--------
直接运行本文件
    -> run_demo()
    -> build_demo_belief() 建立只有“USB-C 接口”证据的初始状态
    -> missing_evidence_fields() + plan_next_actions() 展示证据缺口
    -> add_label_evidence() 注入模拟标签观察并生成派生证据
    -> add_demo_device_evidence() 注入模拟设备规格
    -> plan_next_actions() 发现证据齐全，返回 run_rules
    -> evaluate_compatibility() 执行 MVP 规则
    -> build_evidence_answer() + RunEvent 输出结果

重要边界
--------
本章没有真的调用摄像头、OCR 引擎、知识库、大模型、LangGraph 或检查点；
Observation 和设备规格都是演示数据。它验证的是证据管理与规则逻辑，而不是
现实硬件的完整电气安全，也不能据此判断充电器真伪、线缆能力或长期可靠性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 同时支持两种运行方式：
# 1) 作为包导入时使用相对导入；2) 直接执行本文件时退回同目录绝对导入。
try:
    from .realsight_domain import (
        Action,
        ActionType,
        BeliefState,
        Evidence,
        EvidenceStatus,
        Observation,
        ObservationRequest,
        RealityObject,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )
except ImportError:
    from realsight_domain import (  # type: ignore[no-redef]
        Action,
        ActionType,
        BeliefState,
        Evidence,
        EvidenceStatus,
        Observation,
        ObservationRequest,
        RealityObject,
        RunEvent,
        RunEventType,
        TaskSession,
        to_primitive,
    )


@dataclass(frozen=True)
class PowerProfile:
    """标签中的一个输出档位，例如 20V × 3.25A = 65W。"""

    voltage_v: float
    current_a: float
    power_w: float


@dataclass(frozen=True)
class CompatibilityResult:
    """MVP 规则的结构化结果；None 表示证据不足，而不是“不兼容”。"""

    rule_name: str
    decision: str
    meets_mvp_charging_requirements: bool | None
    port_status: str
    protocol_status: str
    power_status: str
    reasons: tuple[str, ...]
    evidence_references: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


# 完整业务流程想收集的字段，包括用于审计的原始 OCR 和最大功率。
REQUIRED_EVIDENCE_FIELDS = (
    "source_port_type",
    "back_label_ocr_text",
    "power_profiles",
    "maximum_output_power_w",
    "supported_protocol",
    "target_device_model",
    "target_device_port_type",
    "target_device_minimum_power_w",
    "target_device_required_voltage_v",
    "target_device_protocol",
)

# 最终兼容规则真正读取的字段；原始 OCR 和最大功率不直接参与判定。
RULE_INPUT_FIELDS = (
    "source_port_type",
    "power_profiles",
    "supported_protocol",
    "target_device_port_type",
    "target_device_minimum_power_w",
    "target_device_required_voltage_v",
    "target_device_protocol",
)

MVP_RULE_NAME = "meets_mvp_charging_requirements_v1"


def missing_evidence_fields(belief: BeliefState) -> list[str]:
    """返回尚未 confirmed 的必需字段；冲突字段也按缺失处理。"""

    return [
        name
        for name in REQUIRED_EVIDENCE_FIELDS
        if name not in belief.confirmed or name in belief.conflicts
    ]


def plan_next_actions(belief: BeliefState) -> list[Action]:
    """把证据缺口转换成机器可执行、界面也可展示的下一步 Action。"""

    missing = set(missing_evidence_fields(belief))
    actions: list[Action] = []

    # 接口形状必须看接口特写，不能从背面标签或设备型号推断。
    if "source_port_type" in missing:
        request = ObservationRequest(
            target_id=belief.target_id,
            view_type="port_closeup",
            required_features=("connector_shape",),
            instruction="请将充电器输出接口靠近摄像头，并保持画面清晰。",
            reason="需要确认充电器输出接口是否为 USB-C。",
        )
        actions.append(
            Action(
                action_type=ActionType.REQUEST_VIEW,
                target_id=belief.target_id,
                reason=request.reason,
                payload=to_primitive(request),
            )
        )

    # 下面四个字段都来自同一张背面标签，因此合并成一次观察请求。
    label_fields = {
        "back_label_ocr_text",
        "power_profiles",
        "maximum_output_power_w",
        "supported_protocol",
    }
    missing_label_fields = sorted(missing & label_fields)
    if missing_label_fields:
        request = ObservationRequest(
            target_id=belief.target_id,
            view_type="back_label",
            required_features=tuple(missing_label_fields),
            instruction="请将充电器翻到有输出参数文字的一面，减少反光并保持稳定。",
            reason="需要从背面标签确认输出档位和充电协议。",
        )
        actions.append(
            Action(
                action_type=ActionType.REQUEST_VIEW,
                target_id=belief.target_id,
                reason=request.reason,
                payload=to_primitive(request),
            )
        )

    # 没有完整型号就先问用户；有型号后，才有资格查询官方设备规格。
    if "target_device_model" in missing:
        actions.append(
            Action(
                action_type=ActionType.ASK_USER,
                target_id=belief.target_id,
                reason="需要准确型号才能查询笔记本接口、协议、电压档位和最低功率要求。",
                payload={"question": "请提供笔记本的完整型号、代际或机器类型编号。"},
            )
        )
    elif missing & {
        "target_device_port_type",
        "target_device_minimum_power_w",
        "target_device_required_voltage_v",
        "target_device_protocol",
    }:
        actions.append(
            Action(
                action_type=ActionType.RETRIEVE_KNOWLEDGE,
                target_id=belief.target_id,
                reason="已经获得设备型号，需要查询设备官方规格。",
                payload={
                    "device_model": belief.confirmed_value("target_device_model"),
                    "required_fields": sorted(
                        missing
                        & {
                            "target_device_port_type",
                            "target_device_minimum_power_w",
                            "target_device_required_voltage_v",
                            "target_device_protocol",
                        }
                    ),
                },
            )
        )

    # 只有业务要求的全部证据均已确认时，规划器才开放规则执行入口。
    if not missing:
        actions.append(
            Action(
                action_type=ActionType.RUN_RULES,
                target_id=belief.target_id,
                reason="MVP 充电判断所需证据已经齐全。",
                payload={"rule": MVP_RULE_NAME},
            )
        )

    return actions


# 命名分组 voltage/current 会捕获 “数字 V / 数字 A”，并允许小数和空格。
PROFILE_PATTERN = re.compile(
    r"(?P<voltage>\d+(?:\.\d+)?)\s*V\s*/\s*(?P<current>\d+(?:\.\d+)?)\s*A",
    re.IGNORECASE,
)


def parse_power_profiles(label_text: str) -> list[PowerProfile]:
    """扫描标签中的所有电压/电流档位，并确定性地计算每档功率。"""

    profiles: list[PowerProfile] = []
    for match in PROFILE_PATTERN.finditer(label_text):
        voltage = float(match.group("voltage"))
        current = float(match.group("current"))
        # 电功率 P = U × I；round(..., 2) 避免浮点展示出现过长小数。
        profiles.append(
            PowerProfile(
                voltage_v=voltage,
                current_a=current,
                power_w=round(voltage * current, 2),
            )
        )
    return profiles


def infer_protocol(label_text: str) -> str | None:
    """从标签关键字识别 USB-PD；没有明确文字时保持未知（返回 None）。"""

    # 统一大小写和下划线，降低不同标签写法带来的匹配差异。
    normalized = label_text.upper().replace("_", " ")
    if (
        "USB POWER DELIVERY" in normalized
        or "USB-PD" in normalized
        or " PD " in f" {normalized} "
    ):
        return "USB-PD"
    return None


def add_label_evidence(
    belief: BeliefState,
    observation: Observation,
    label_text: str,
) -> list[PowerProfile]:
    """把一次背面标签 Observation 和 OCR 文本写成可追溯的多层证据。

    返回解析后的档位只是为了方便调用方展示；真正的状态变化已经写入 belief。
    """

    # 防止把另一个现实物体的观察混进当前目标，检查失败前不会修改 belief。
    if observation.target_id != belief.target_id:
        raise ValueError("Observation target does not match the belief state target")

    belief.observed_views.add(observation.view_type)
    # 第一层：保存未经解析的 OCR 原文，它是后续档位/协议证据的根来源。
    raw_evidence = Evidence(
        evidence_id=f"ev-{observation.observation_id}-ocr",
        target_id=belief.target_id,
        field="back_label_ocr_text",
        value=label_text,
        source_type="observation_ocr",
        source_id=observation.observation_id,
        confidence=0.90,
    )
    belief.add_evidence(raw_evidence)

    # 第二层：从 OCR 文本中解析结构化功率档位。
    profiles = parse_power_profiles(label_text)
    profile_evidence_id = f"ev-{observation.observation_id}-profiles"
    if not profiles:
        # “没有解析出来”不是“确认不存在”，所以写 UNKNOWN 而非空的 CONFIRMED。
        belief.add_evidence(
            Evidence(
                evidence_id=profile_evidence_id,
                target_id=belief.target_id,
                field="power_profiles",
                value=None,
                source_type="power_profile_parser_v1",
                source_id="power_profile_parser_v1",
                confidence=0.0,
                status=EvidenceStatus.UNKNOWN,
                derived_from=(raw_evidence.evidence_id,),
            )
        )
        belief.add_evidence(
            Evidence(
                evidence_id=f"ev-{observation.observation_id}-max-power",
                target_id=belief.target_id,
                field="maximum_output_power_w",
                value=None,
                source_type="power_calculator_v1",
                source_id="power_calculator_v1",
                confidence=0.0,
                status=EvidenceStatus.UNKNOWN,
                derived_from=(profile_evidence_id,),
            )
        )
    else:
        profile_evidence = Evidence(
            evidence_id=profile_evidence_id,
            target_id=belief.target_id,
            field="power_profiles",
            value=tuple(profiles),
            source_type="parsed_value",
            source_id="power_profile_parser_v1",
            confidence=0.96,
            derived_from=(raw_evidence.evidence_id,),
        )
        belief.add_evidence(profile_evidence)
        # 第三层：最大功率是从档位证据算出的派生值，而非 OCR 直接事实。
        maximum_power = max(profile.power_w for profile in profiles)
        belief.add_evidence(
            Evidence(
                evidence_id=f"ev-{observation.observation_id}-max-power",
                target_id=belief.target_id,
                field="maximum_output_power_w",
                value=maximum_power,
                source_type="calculated_value",
                source_id="power_calculator_v1",
                confidence=0.96,
                derived_from=(profile_evidence.evidence_id,),
            )
        )

    # 协议同样必须有明确标签文字；仅看到 USB-C 接口不能推出 USB-PD。
    protocol = infer_protocol(label_text)
    belief.add_evidence(
        Evidence(
            evidence_id=f"ev-{observation.observation_id}-protocol",
            target_id=belief.target_id,
            field="supported_protocol",
            value=protocol,
            source_type="parsed_value",
            source_id="protocol_parser_v1",
            confidence=0.92 if protocol else 0.0,
            status=EvidenceStatus.CONFIRMED if protocol else EvidenceStatus.UNKNOWN,
            derived_from=(raw_evidence.evidence_id,),
        )
    )
    return profiles


def evaluate_compatibility(belief: BeliefState) -> CompatibilityResult:
    """执行课程版 USB-C 兼容规则，并返回每个判断维度的详细结果。

    这里刻意区分三种含义：证据不足 -> None；证据完整但不满足 -> False；
    证据完整且满足 -> True。这样不会把“尚不知道”错误地说成“不兼容”。
    """

    # 门禁 1：任一规则输入存在冲突时立即停止，不能擅自挑选一个值。
    conflicts = sorted(set(RULE_INPUT_FIELDS) & belief.conflicts.keys())
    if conflicts:
        return CompatibilityResult(
            rule_name=MVP_RULE_NAME,
            decision="insufficient_evidence",
            meets_mvp_charging_requirements=None,
            port_status="unknown",
            protocol_status="unknown",
            power_status="unknown",
            reasons=("关键证据存在冲突，不能运行最终规则。",),
            evidence_references=_evidence_references(belief, conflicts),
            unknowns=tuple(conflicts),
        )

    # 门禁 2：规则所需字段必须全部进入 confirmed，probable/unknown 都不够。
    missing = [name for name in RULE_INPUT_FIELDS if name not in belief.confirmed]
    if missing:
        return CompatibilityResult(
            rule_name=MVP_RULE_NAME,
            decision="insufficient_evidence",
            meets_mvp_charging_requirements=None,
            port_status="unknown",
            protocol_status="unknown",
            power_status="unknown",
            reasons=("MVP 规则所需证据尚未收集完整。",),
            evidence_references=_evidence_references(belief, RULE_INPUT_FIELDS),
            unknowns=tuple(missing),
        )

    # 经过门禁后这些值一定存在；统一大小写后再比较，避免 USB-C/usb-c 的差异。
    source_port = str(belief.confirmed_value("source_port_type")).upper()
    target_port = str(belief.confirmed_value("target_device_port_type")).upper()
    source_protocol = str(belief.confirmed_value("supported_protocol")).upper()
    target_protocol = str(belief.confirmed_value("target_device_protocol")).upper()
    target_power = float(belief.confirmed_value("target_device_minimum_power_w"))
    target_voltage = float(belief.confirmed_value("target_device_required_voltage_v"))
    profiles = tuple(belief.confirmed_value("power_profiles"))

    # 接口与协议分别检查，便于最终回答指出具体失败维度。
    port_ok = source_port == target_port == "USB-C"
    protocol_ok = source_protocol == target_protocol == "USB-PD"
    # 不能只看“最大功率”：必须先找到设备要求的电压档位，再比较该档位功率。
    matching_profiles = [
        profile
        for profile in profiles
        if abs(profile.voltage_v - target_voltage) < 0.01
    ]
    qualifying_profiles = [
        profile for profile in matching_profiles if profile.power_w >= target_power
    ]
    power_ok = bool(qualifying_profiles)
    meets_requirements = port_ok and protocol_ok and power_ok

    # 下面三条分支把功率失败细分为：足够、同电压但功率不足、缺少所需电压。
    if qualifying_profiles:
        best_profile = max(qualifying_profiles, key=lambda profile: profile.power_w)
        profile_reason = (
            f"功率档位：存在 {best_profile.voltage_v:g}V/{best_profile.current_a:g}A "
            f"({best_profile.power_w:g}W)，满足设备 {target_voltage:g}V、至少 {target_power:g}W 的要求。"
        )
        power_status = "sufficient"
    elif matching_profiles:
        best_profile = max(matching_profiles, key=lambda profile: profile.power_w)
        profile_reason = (
            f"功率档位：存在 {target_voltage:g}V 档，但最高仅 {best_profile.power_w:g}W，"
            f"低于设备最低 {target_power:g}W。"
        )
        power_status = "insufficient"
    else:
        profile_reason = f"功率档位：未发现设备要求的 {target_voltage:g}V 输出档位。"
        power_status = "required_voltage_profile_missing"

    # reasons 是面向人的解释，布尔/状态字段则方便程序继续处理。
    reasons = (
        f"接口：充电器 {source_port}，设备要求 {target_port}。",
        f"协议：充电器 {source_protocol}，设备要求 {target_protocol}。",
        profile_reason,
    )
    return CompatibilityResult(
        rule_name=MVP_RULE_NAME,
        decision=(
            "meets_mvp_charging_requirements"
            if meets_requirements
            else "does_not_meet_mvp_charging_requirements"
        ),
        meets_mvp_charging_requirements=meets_requirements,
        port_status="compatible" if port_ok else "incompatible",
        protocol_status="compatible" if protocol_ok else "incompatible",
        power_status=power_status,
        reasons=reasons,
        evidence_references=_evidence_references(belief, RULE_INPUT_FIELDS),
        unknowns=(
            "未验证端口方向、单端口额定值与多端口同时使用时的功率降额。",
            "未验证线缆额定能力、PPS/完整 PDO 协商、真伪、质量与长期可靠性。",
        ),
    )


def _evidence_references(
    belief: BeliefState,
    field_names: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """为指定字段生成证据来源列表，并递归列出所有父证据。

    例如 maximum_power -> power_profiles -> raw OCR。visited 用来去重，也能避免
    错误数据形成循环引用时无限递归。
    """

    references: list[str] = []
    visited: set[str] = set()

    def append_reference(evidence: Evidence) -> None:
        """深度优先添加父证据，再添加当前证据，使输出符合来源顺序。"""

        if evidence.evidence_id in visited:
            return
        visited.add(evidence.evidence_id)
        for parent_id in evidence.derived_from:
            parent = belief.ledger.get(parent_id)
            if parent is not None:
                append_reference(parent)
        parent_text = (
            f", derived_from={','.join(evidence.derived_from)}"
            if evidence.derived_from
            else ""
        )
        references.append(
            f"{evidence.field} <- {evidence.evidence_id} "
            f"({evidence.source_type}:{evidence.source_id}{parent_text})"
        )

    for field_name in field_names:
        for evidence in belief.evidence_for(field_name):
            append_reference(evidence)
    return tuple(references)


def build_evidence_answer(result: CompatibilityResult) -> str:
    """把结构化规则结果组装成“结论 + 证据 + 来源 + 未知边界”的文本。"""

    # None/True/False 分别对应证据不足、满足、不满足，三者不能混为一谈。
    if result.meets_mvp_charging_requirements is None:
        conclusion = "当前证据不足，暂时不能给出课程 MVP 充电判断。"
    elif result.meets_mvp_charging_requirements:
        conclusion = "根据课程 MVP 规则，当前证据支持满足充电要求。"
    else:
        conclusion = "根据课程 MVP 规则，当前证据显示不满足充电要求。"

    # 每个元组元素单独成为一个 Markdown 风格列表项，方便终端或界面展示。
    evidence_lines = "\n".join(f"- {reason}" for reason in result.reasons)
    reference_lines = "\n".join(f"- {item}" for item in result.evidence_references)
    unknown_lines = "\n".join(f"- {item}" for item in result.unknowns)
    return (
        f"{conclusion}\n\n证据：\n{evidence_lines}"
        f"\n\n来源：\n{reference_lines or '- 尚无可引用来源'}"
        f"\n\n未知边界：\n{unknown_lines or '- 无'}"
    )


def print_json(value: Any) -> None:
    """以保留中文、带缩进的 JSON 格式打印任意领域对象。"""

    print(json.dumps(to_primitive(value), ensure_ascii=False, indent=2))


def build_demo_belief() -> BeliefState:
    """创建演示初始状态：只确认充电器的输出接口是 USB-C。"""

    belief = BeliefState(target_id="charger-01")
    belief.add_evidence(
        Evidence(
            evidence_id="ev-front-port",
            target_id=belief.target_id,
            field="source_port_type",
            value="USB-C",
            source_type="visible_feature",
            source_id="obs-front-01",
            confidence=0.96,
        )
    )
    belief.observed_views.add("front")
    return belief


def add_demo_device_evidence(belief: BeliefState) -> None:
    """模拟“用户提供型号 + 规格库返回参数”，补齐目标设备证据。"""

    values = {
        "target_device_model": "DemoBook 14 Gen 2",
        "target_device_port_type": "USB-C",
        "target_device_minimum_power_w": 45,
        "target_device_required_voltage_v": 20,
        "target_device_protocol": "USB-PD",
    }
    # 型号来自用户，其余参数来自模拟说明书；来源类型因此不同。
    for index, (field_name, value) in enumerate(values.items(), start=1):
        belief.add_evidence(
            Evidence(
                evidence_id=f"ev-device-{index}",
                target_id=belief.target_id,
                field=field_name,
                value=value,
                source_type=(
                    "user_provided" if field_name == "target_device_model" else "manual_spec"
                ),
                source_id=(
                    "user-input" if field_name == "target_device_model" else "demo-device-db:v1"
                ),
                confidence=1.0,
            )
        )


def run_demo() -> None:
    """按七个可观察阶段串起完整证据闭环，供直接运行学习。"""

    # 第 1 阶段：创建任务、现实目标和仅含接口证据的初始认知。
    session = TaskSession(
        session_id="session-ch01",
        intent="compatibility_check",
        target_id="charger-01",
    )
    target = RealityObject(target_id=session.target_id, category="usb_c_charger")
    belief = build_demo_belief()

    print("=== 1. 任务与现实目标 ===")
    print_json({"session": session, "target": target})

    # 第 2～3 阶段：先看还缺什么，再看系统为每类缺口规划什么动作。
    print("\n=== 2. 初始证据缺口 ===")
    print_json({"missing_fields": missing_evidence_fields(belief)})

    print("\n=== 3. 规划的下一步动作 ===")
    print_json(plan_next_actions(belief))

    # 模拟感知端已获得清晰背面照片；真实系统会由摄像头服务返回它。
    observation = Observation(
        observation_id="obs-back-label-01",
        target_id=belief.target_id,
        view_type="back_label",
        quality_score=0.91,
        image_path="artifacts/session-ch01/back_label.jpg",
        local_features=("text_region", "low_glare"),
    )
    # 模拟 OCR 输出；本章没有真正调用 OCR 引擎。
    label_text = "USB Power Delivery Output: 5V/3A, 9V/3A, 15V/3A, 20V/3.25A"
    profiles = add_label_evidence(belief, observation, label_text)
    add_demo_device_evidence(belief)

    print("\n=== 4. 标签证据与派生链 ===")
    print_json(
        {
            "profiles": profiles,
            "maximum_power_w": belief.confirmed_value("maximum_output_power_w"),
            "ledger": belief.ledger,
        }
    )

    print("\n=== 5. 证据补齐后的动作 ===")
    print_json(plan_next_actions(belief))

    # 第 6 阶段：证据齐全后才执行确定性规则。
    result = evaluate_compatibility(belief)
    print("\n=== 6. 确定性 MVP 规则结果 ===")
    print_json(result)

    # RunEvent 是给上层事件流/前端消费的机器记录；回答文本则给最终用户阅读。
    event = RunEvent(
        event_type=RunEventType.FINAL_ANSWER,
        session_id=session.session_id,
        message="USB-C MVP 充电判断完成",
        data={"decision": result.decision, "rule_name": result.rule_name},
        sequence=1,
    )
    print("\n=== 7. 证据化回答 ===")
    print(build_evidence_answer(result))
    print("\n事件：")
    print_json(event)


if __name__ == "__main__":
    # 只有“python ch01_evidence_loop.py”直接运行时才执行 Demo；被导入时不会执行。
    run_demo()
