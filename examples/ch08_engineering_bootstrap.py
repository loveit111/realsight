"""
第 8 章 Demo：从“散落脚本”进入可安装 Python 包、配置快照和工程自检。

这个文件做什么
------------
前七章的 Demo 直接修改 ``sys.path`` 来找到课程目录中的模块，这适合逐章学习，却不适合
团队工程。本 Demo 必须通过 ``uv run`` 启动，并且只从已安装的 ``realsight`` 正式包
导入代码。它读取 TOML 配置，构造延续第 4 章契约的 TaskSession 和 ObservationRequest，
再运行第 8 章 doctor。输出同时证明“配置可读、稳定类型可导入、项目元数据可发现”。

使用的技术栈
------------
- uv + pyproject.toml：创建 Python 3.12 隔离环境并以 editable 形式安装本项目。
- Python src 布局：代码位于 python/src/realsight，防止从项目根误导入未安装源码。
- Pydantic v2：校验 TaskSession、ObservationRequest 与自检报告。
- tomllib + pathlib：解析 TOML，并稳定处理 Windows/Linux 路径。
- importlib.metadata：doctor 读取已安装发行包的版本。

调用流程
--------
``uv run --locked python examples/ch08_engineering_bootstrap.py``
    -> 从正式包导入 contracts/config/doctor
    -> load_settings(config/realsight.example.toml)
    -> 构造 TaskSession 和 ObservationRequest
    -> run_doctor(..., strict_python=True)
    -> 输出一个 JSON 摘要

如何理解输出
------------
``session`` 和 ``observation_request`` 说明前面章节的领域语言已进入正式包；``policy``
说明配置不再写死在 Demo；``doctor`` 逐项报告环境。doctor 可以因 CMake 未安装而显示
ready=false，这不是伪造成功，而是给出明确的下一步修复。完整验收要求所有项通过。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from realsight.config import load_settings
from realsight.contracts import ObservationRequest, TaskSession, ViewType
from realsight.doctor import run_doctor

# 文件位于 <project>/examples，因此父目录的父目录就是工程根。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "realsight.example.toml"


def build_demo_summary(config_path: Path, *, strict_python: bool) -> dict[str, object]:
    """执行本章最小闭环，返回易于测试和阅读的结构化摘要。"""

    settings = load_settings(config_path, environ={})

    # TaskSession 仍保持“session_id 与 thread_id 一一对应”的课程 MVP 约束。
    session = TaskSession(
        session_id="session-ch08-001",
        thread_id="session-ch08-001",
        intent="判断 USB-C 充电兼容性",
        target_id="charger-ch08-001",
    )

    # 本章不连接摄像头，但创建一条第 9～10 章将交给 C++ 的真实契约。
    request = ObservationRequest(
        request_id="request-ch08-001",
        session_id=session.session_id,
        target_id=session.target_id,
        view_type=ViewType.BACK_LABEL,
        required_features=("charger_power", "charger_protocol"),
        instruction="请拍摄充电器背面标签",
        reason="需要额定功率与协议证据",
        timeout_ms=5_000,
        correlation_id="command-ch08-001",
    )

    report = run_doctor(config_path, strict_python=strict_python)
    return {
        "session": session.model_dump(mode="json"),
        "observation_request": request.model_dump(mode="json"),
        "policy": {
            "policy_id": settings.governance.policy_id,
            "allowed_capabilities": sorted(settings.governance.allowed_capabilities),
        },
        "doctor": report.model_dump(mode="json"),
    }


def main() -> int:
    """解析参数、打印 JSON，并让退出码反映自检是否通过。"""

    parser = argparse.ArgumentParser(description="Run the Chapter 8 bootstrap demo")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--no-strict-python",
        action="store_true",
        help="only for explaining diagnostics outside the target Python 3.12 environment",
    )
    arguments = parser.parse_args()
    summary = build_demo_summary(
        arguments.config,
        strict_python=not arguments.no_strict_python,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if bool(summary["doctor"]["ready"]) else 1  # type: ignore[index]


if __name__ == "__main__":
    raise SystemExit(main())
