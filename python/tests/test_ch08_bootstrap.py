"""
第 8 章自动化测试：验证正式包、配置、自检器和兼容迁移没有停留在目录外观。

这个文件的整体逻辑
------------------
工程初始化最容易出现“目录很好看，但从干净环境不能导入”的假完成。因此测试从已安装
的 ``realsight`` 包导入，不修改 ``sys.path``；随后验证 TOML 解析、环境变量覆盖、未知
字段拒绝、领域契约身份、doctor 汇总和 CLI 退出码。测试还读取 CMake 文件，确认 C++20
要求是目标属性。仓库现已演进到第 10 章，因此允许 OpenCV 与 gRPC，但检查 gRPC 只
进入独立 transport target，不污染第 9 章 runtime 核心。

使用的技术栈
------------
- pytest 9：使用 ``tmp_path``，让临时配置彼此隔离。
- unittest.mock：替换工具查找与报告，测试时不依赖本机真的安装 CMake。
- Pydantic v2：断言未知字段和非法预算被拒绝。
- importlib.metadata：确认测试运行的是已安装发行包，而不是项目根偶然导入。

测试调用流程
------------
``uv run --locked pytest python/tests/test_ch08_bootstrap.py -q``
    -> 检查 src 包安装和 Python 3.12
    -> 检查配置文件、覆盖优先级与相对路径
    -> 检查旧 contracts.models 与新包共享类身份
    -> 模拟 doctor 工具成功/缺失
    -> 检查 CLI 文本、JSON 和退出码
    -> 检查 CMake target 及章节边界

所有测试均为确定性测试，不联网、不启动摄像头，也不依赖真实 C++ 编译器。
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import realsight
from pydantic import ValidationError
from realsight import cli
from realsight.config import ConfigurationError, load_settings
from realsight.contracts import ObservationRequest, TaskSession, ViewType
from realsight.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    check_python,
    check_tool,
    run_doctor,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "realsight.example.toml"


def write_config(path: Path, *, extra: str = "") -> None:
    """写入一份小型合法配置；extra 用于构造单项错误。"""

    path.write_text(
        """
[runtime]
data_dir = "data"
log_level = "info"

[governance]
policy_id = "test-policy-v1"
allowed_capabilities = ["perception.observe", "rules.usb_c"]
max_commands = 4
max_observations = 2
max_external_attempts = 6
max_cost_units = 8
"""
        + extra,
        encoding="utf-8",
    )


def test_project_runs_on_the_pinned_python() -> None:
    """本章验收必须真实运行在 3.12，不能只在文件里写版本号。"""

    assert sys.version_info[:2] == (3, 12)


def test_distribution_and_src_package_are_installed() -> None:
    """发行元数据存在，包文件也确实来自 python/src 布局。"""

    assert importlib.metadata.version("realsight") == realsight.__version__
    package_path = Path(realsight.__file__).resolve()
    assert "python" in package_path.parts
    assert "src" in package_path.parts


def test_load_settings_resolves_path_relative_to_config(tmp_path: Path) -> None:
    """切换工作目录不应改变相对 data_dir 的含义。"""

    config_path = tmp_path / "realsight.toml"
    write_config(config_path)
    settings = load_settings(config_path, environ={})

    assert settings.runtime.data_dir == (tmp_path / "data").resolve()
    assert settings.runtime.log_level == "INFO"
    assert settings.governance.max_commands == 4


def test_environment_override_is_explicit_and_typed(tmp_path: Path) -> None:
    """只有白名单变量覆盖配置，整数也必须经过显式转换。"""

    config_path = tmp_path / "realsight.toml"
    write_config(config_path)
    settings = load_settings(
        config_path,
        environ={
            "REALSIGHT_MAX_COMMANDS": "9",
            "REALSIGHT_LOG_LEVEL": "warning",
            "UNRELATED_SECRET": "must-not-enter-settings",
        },
    )

    assert settings.governance.max_commands == 9
    assert settings.runtime.log_level == "WARNING"
    assert "UNRELATED_SECRET" not in settings.model_dump_json()


def test_invalid_environment_integer_has_context(tmp_path: Path) -> None:
    """错误信息指出具体环境变量，而不是只抛出模糊的 int 转换失败。"""

    config_path = tmp_path / "realsight.toml"
    write_config(config_path)
    with pytest.raises(ConfigurationError, match="REALSIGHT_MAX_COMMANDS"):
        load_settings(config_path, environ={"REALSIGHT_MAX_COMMANDS": "many"})


def test_unknown_config_field_is_rejected(tmp_path: Path) -> None:
    """拼错的字段不能被忽略，否则部署时会悄悄使用默认值。"""

    config_path = tmp_path / "realsight.toml"
    write_config(config_path, extra="\n[unexpected]\nvalue = 1\n")
    with pytest.raises(ValidationError, match="unexpected"):
        load_settings(config_path, environ={})


def test_invalid_capability_is_rejected(tmp_path: Path) -> None:
    """capability 必须使用 namespace.name，和第 7 章权限语义保持一致。"""

    config_path = tmp_path / "realsight.toml"
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
        '"perception.observe", "rules.usb_c"', '"invalid capability"'
    )
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError, match="capability"):
        load_settings(config_path, environ={})


def test_formal_contract_builds_chapter_nine_request() -> None:
    """正式包延续稳定类型，可以构造下一章将消费的观察请求。"""

    session = TaskSession(
        session_id="session-test-ch08",
        thread_id="session-test-ch08",
        intent="检查 USB-C 兼容性",
        target_id="charger-test-ch08",
    )
    request = ObservationRequest(
        request_id="request-test-ch08",
        session_id=session.session_id,
        target_id=session.target_id,
        view_type=ViewType.BACK_LABEL,
        required_features=("charger_power",),
        instruction="拍摄背面标签",
        reason="读取额定功率",
    )

    assert request.target_id == session.target_id
    assert request.view_type is ViewType.BACK_LABEL


def test_legacy_and_formal_contracts_share_class_identity() -> None:
    """在旧脚本的根目录语境中，兼容入口必须转发同一个类。"""

    # 正式 src 测试进程故意不能偶然导入项目根目录；所以另启一个以工程根为当前目录的
    # Python 进程，模拟第 4～7 章旧脚本的真实启动方式。
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from contracts.models import TaskSession as old; "
                "from realsight.contracts import TaskSession as new; "
                "raise SystemExit(0 if old is new else 1)"
            ),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_check_python_is_strict_in_target_environment() -> None:
    """当前 uv 环境应让严格 Python 检查真实通过。"""

    result = check_python(strict=True)
    assert result.status is CheckStatus.PASS


def test_check_tool_reports_missing_binary() -> None:
    """缺工具时给出失败和修复提示，不抛出 FileNotFoundError。"""

    with patch("realsight.doctor.shutil.which", return_value=None):
        result = check_tool(
            "cmake",
            ("cmake",),
            ("--version",),
            "install CMake",
        )

    assert result.status is CheckStatus.FAIL
    assert result.remediation == "install CMake"


def test_check_tool_reports_version(tmp_path: Path) -> None:
    """用 mock 隔离平台差异，只验证查找和版本输出逻辑。"""

    executable = tmp_path / "tool.exe"
    with (
        patch("realsight.doctor.shutil.which", return_value=str(executable)),
        patch("realsight.doctor._first_line", return_value="tool 1.2.3"),
    ):
        result = check_tool("tool", ("tool",), ("--version",), "install tool")

    assert result.status is CheckStatus.PASS
    assert "tool 1.2.3" in result.detail


def test_doctor_ready_when_every_check_passes() -> None:
    """汇总逻辑只依赖单项状态，保持确定且容易审计。"""

    passed = CheckResult(name="mock", status=CheckStatus.PASS, detail="ok")
    with (
        patch("realsight.doctor.check_python", return_value=passed),
        patch("realsight.doctor.check_package", return_value=passed),
        patch("realsight.doctor.check_config", return_value=passed),
        patch("realsight.doctor.check_tool", return_value=passed),
        patch("realsight.doctor.check_cxx_compiler", return_value=passed),
        patch("realsight.doctor.check_opencv", return_value=passed),
        patch("realsight.doctor.check_rpc_toolchain", return_value=passed),
    ):
        report = run_doctor(EXAMPLE_CONFIG)

    assert report.ready is True
    assert len(report.checks) == 8


def test_cli_returns_one_and_renders_remediation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """失败报告必须同时影响文本与进程退出码，方便人和 CI 使用。"""

    report = DoctorReport(
        checks=(
            CheckResult(
                name="cmake",
                status=CheckStatus.FAIL,
                detail="missing",
                remediation="install CMake",
            ),
        ),
        ready=False,
    )
    with patch("realsight.cli.run_doctor", return_value=report):
        exit_code = cli.main([])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "install CMake" in output
    assert "READY=false" in output


def test_cli_json_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON 模式可供 CI 或第 14 章 API 复用。"""

    report = DoctorReport(
        checks=(CheckResult(name="all", status=CheckStatus.PASS, detail="ok"),),
        ready=True,
    )
    with patch("realsight.cli.run_doctor", return_value=report):
        exit_code = cli.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True


def test_cmake_uses_target_scoped_cxx20_and_tracks_current_boundary() -> None:
    """CMake 使用 target 标准，且第 10 章 transport 没有反向污染 runtime。"""

    cpp_cmake = (PROJECT_ROOT / "cpp" / "CMakeLists.txt").read_text(encoding="utf-8")
    root_cmake = (PROJECT_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "target_compile_features(realsight_runtime PUBLIC cxx_std_20)" in cpp_cmake
    assert "CMAKE_CXX_STANDARD" not in cpp_cmake + root_cmake
    assert "find_package(OpenCV" in cpp_cmake
    assert "find_package(gRPC CONFIG REQUIRED)" in cpp_cmake
    runtime_section = cpp_cmake.split("realsight_runtime STATIC", maxsplit=1)[1]
    runtime_section = runtime_section.split(
        "add_library(realsight_transport", maxsplit=1
    )[0]
    assert "gRPC::" not in runtime_section
    assert "RealSight::runtime RealSight::proto gRPC::grpc++" in cpp_cmake
