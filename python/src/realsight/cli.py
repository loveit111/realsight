"""
RealSight 第 8 章命令行入口：以人类文本或 JSON 形式展示工程自检结果。

这个文件的整体逻辑
------------------
``pyproject.toml`` 把 ``realsight-doctor`` 命令映射到本文件的 ``main``。CLI 只负责解析
参数、调用 ``doctor.run_doctor`` 和选择输出格式；环境检查的业务规则都留在 doctor
模块，因此测试可以直接调用函数，不必启动子进程。

使用的技术栈与调用流程
----------------------
- ``argparse``：解析配置路径、JSON 输出和 Python 严格模式。
- Pydantic ``model_dump_json``：生成稳定机器可读输出。

终端 -> realsight-doctor -> cli.main -> doctor.run_doctor -> DoctorReport -> 退出码

退出码 0 表示本章初始化条件满足，1 表示至少一项失败。``--no-strict-python`` 只用于
解释当前非目标解释器下的诊断结果，正式开发和 CI 始终要求 Python 3.12。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from realsight.doctor import CheckStatus, DoctorReport, run_doctor


def build_parser() -> argparse.ArgumentParser:
    """创建参数解析器；独立函数便于测试帮助信息和默认值。"""

    parser = argparse.ArgumentParser(description="Check the RealSight toolchain")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/realsight.example.toml"),
        help="path to the TOML configuration file",
    )
    parser.add_argument(
        "--json", action="store_true", help="print JSON instead of text"
    )
    parser.add_argument(
        "--no-strict-python",
        action="store_true",
        help="report a non-3.12 interpreter as skipped instead of failed",
    )
    return parser


def render_text(report: DoctorReport) -> str:
    """把结构化报告变成适合初学者阅读的逐行结果。"""

    lines: list[str] = []
    for check in report.checks:
        lines.append(f"[{check.status.value.upper():4}] {check.name}: {check.detail}")
        if check.remediation is not None and check.status is not CheckStatus.PASS:
            lines.append(f"       fix: {check.remediation}")
    lines.append(f"READY={str(report.ready).lower()}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """运行 CLI；返回值直接作为进程退出码。"""

    arguments = build_parser().parse_args(argv)
    report = run_doctor(
        arguments.config,
        strict_python=not arguments.no_strict_python,
    )
    output = report.model_dump_json(indent=2) if arguments.json else render_text(report)
    print(output)
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
