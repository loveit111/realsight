"""
RealSight 工程自检器：用结构化检查回答“这台机器能否开发当前章节的项目”。

这个文件的整体逻辑
------------------
README 中写出安装命令还不够。初学者遇到失败时，常常分不清是 Python 版本、包导入、
配置文件还是 C++ 工具链出了问题。本模块把每一项检查表示成 ``CheckResult``，逐项探测
当前环境，最后生成机器可读的 ``DoctorReport``。失败项带修复提示，但自检器不会擅自
安装软件或修改用户的 PATH。

使用的技术栈
------------
- Python 标准库：``sys`` 检查解释器；``shutil.which`` 查找工具；``subprocess`` 安全
  执行版本命令；``importlib.metadata`` 读取已安装包版本。
- Pydantic v2：确保报告字段稳定，并可输出 JSON 给 CI 或后续 API。
- RealSight 配置包：真实加载 TOML，而不是只检查文件是否存在。

调用流程
--------
``run_doctor(config_path)``
    -> 检查 Python 是否为目标 3.12
    -> 检查正式 realsight 包及版本
    -> 调用 load_settings 验证 TOML
    -> 查找 uv、cmake、C++ 编译器、OpenCV 和第 10 章 Protobuf/gRPC 工具链
    -> 汇总 passed/failed/skipped 和 ready

``realsight-doctor`` CLI 再根据 ``--strict-python`` 决定版本不匹配是失败还是警告。

边界
----
工具“存在”不等于摄像头驱动已联调，也不证明端口、DLL 与协议一定能通信。当前 ready
表示第 10 章构建前置条件满足；真实跨语言链路仍由 run_ch10_demo.py 验证。
"""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from realsight.config import load_settings


class CheckStatus(StrEnum):
    """单项检查的三种结果。"""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class CheckResult(BaseModel):
    """一条稳定自检结果，既可打印也可序列化。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    status: CheckStatus
    detail: str = Field(min_length=1)
    remediation: str | None = None


class DoctorReport(BaseModel):
    """所有检查的汇总；ready 只有在没有 FAIL 时才为真。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[CheckResult, ...]
    ready: bool


def check_python(*, strict: bool) -> CheckResult:
    """验证当前解释器是否严格命中课程目标 Python 3.12。"""

    version = sys.version_info
    matches = version.major == 3 and version.minor == 12
    if matches:
        return CheckResult(
            name="python",
            status=CheckStatus.PASS,
            detail=f"Python {version.major}.{version.minor}.{version.micro}",
        )

    status = CheckStatus.FAIL if strict else CheckStatus.SKIP
    return CheckResult(
        name="python",
        status=status,
        detail=(
            f"running Python {version.major}.{version.minor}.{version.micro}; "
            "project target is 3.12"
        ),
        remediation="run through 'uv run --python 3.12' after uv sync",
    )


def check_package() -> CheckResult:
    """确认当前进程导入的是已安装的正式包，并读取其发行版本。"""

    try:
        version = importlib.metadata.version("realsight")
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            name="python-package",
            status=CheckStatus.FAIL,
            detail="the realsight distribution is not installed",
            remediation="run 'uv sync --locked --all-groups'",
        )
    return CheckResult(
        name="python-package",
        status=CheckStatus.PASS,
        detail=f"realsight {version}",
    )


def check_config(config_path: Path) -> CheckResult:
    """通过正式加载器解析配置，确保内容而不只是文件名有效。"""

    try:
        settings = load_settings(config_path, environ={})
    except (ValueError, OSError) as exc:
        return CheckResult(
            name="configuration",
            status=CheckStatus.FAIL,
            detail=f"invalid config: {exc}",
            remediation="compare the file with config/realsight.example.toml",
        )
    return CheckResult(
        name="configuration",
        status=CheckStatus.PASS,
        detail=(
            f"policy={settings.governance.policy_id}, "
            f"data_dir={settings.runtime.data_dir}"
        ),
    )


def _first_line(command: list[str]) -> str:
    """执行无副作用的版本命令，并返回第一条非空输出。"""

    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    output = completed.stdout or completed.stderr
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if completed.returncode != 0 or not first:
        raise RuntimeError(f"version command failed with code {completed.returncode}")
    return first


def check_tool(
    name: str,
    candidates: tuple[str, ...],
    version_args: tuple[str, ...],
    remediation: str,
) -> CheckResult:
    """按候选顺序查找一个工具，并验证它至少能执行版本命令。"""

    executable = next(
        (
            shutil.which(candidate)
            for candidate in candidates
            if shutil.which(candidate)
        ),
        None,
    )
    if executable is None:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            detail=f"none of {', '.join(candidates)} is available on PATH",
            remediation=remediation,
        )
    try:
        version = _first_line([executable, *version_args])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            detail=f"found {executable}, but could not run it: {exc}",
            remediation=remediation,
        )
    return CheckResult(
        name=name,
        status=CheckStatus.PASS,
        detail=f"{version} ({executable})",
    )


def check_cxx_compiler() -> CheckResult:
    """检查本章真实支持的 GCC/Clang；MSVC 会在增加对应 preset 后再纳入。"""

    return check_tool(
        "cxx-compiler",
        ("g++", "clang++"),
        ("--version",),
        "install MSYS2 UCRT64 GCC or Clang and add its bin directory to PATH",
    )


def check_opencv() -> CheckResult:
    """通过 pkg-config 验证 C++ OpenCV 开发包，而不是 Python 的 cv2 wheel。"""

    executable = shutil.which("pkg-config")
    if executable is None:
        return CheckResult(
            name="opencv-cxx",
            status=CheckStatus.FAIL,
            detail="pkg-config is not available; cannot resolve the OpenCV C++ package",
            remediation=(
                "install the platform OpenCV development package and pkg-config; "
                "on MSYS2 UCRT64 use mingw-w64-ucrt-x86_64-opencv"
            ),
        )

    # OpenCV 4/5 的 MSYS2 和多数 Linux 开发包沿用 opencv4.pc；同时保留 opencv
    # 作为其他发行版的兼容候选。这里只找 C++ 构建元数据，不导入 Python cv2。
    failures: list[str] = []
    for module_name in ("opencv5", "opencv4", "opencv"):
        try:
            version = _first_line([executable, "--modversion", module_name])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{module_name}: {exc}")
            continue
        return CheckResult(
            name="opencv-cxx",
            status=CheckStatus.PASS,
            detail=f"OpenCV {version} via pkg-config module {module_name}",
        )

    # OpenCV 5 的 MSYS2 包可能只发布 OpenCVConfig.cmake，不发布 .pc。编译器所在前缀
    # 是比硬编码 E:\msys64 更可靠的定位方式：<prefix>/bin/g++ 对应 <prefix>/lib/cmake。
    compiler = next(
        (
            shutil.which(candidate)
            for candidate in ("g++", "clang++")
            if shutil.which(candidate)
        ),
        None,
    )
    if compiler is not None:
        prefix = Path(compiler).resolve().parent.parent
        configs = sorted((prefix / "lib" / "cmake").glob("opencv*/OpenCVConfig.cmake"))
        if configs:
            return CheckResult(
                name="opencv-cxx",
                status=CheckStatus.PASS,
                detail=f"OpenCV CMake package {configs[0]}",
            )

    return CheckResult(
        name="opencv-cxx",
        status=CheckStatus.FAIL,
        detail=(
            "OpenCV pkg-config/CMake package was not found ("
            + "; ".join(failures)
            + ")"
        ),
        remediation=(
            "install the C++ OpenCV development package; a Python cv2 wheel is not enough"
        ),
    )


def check_rpc_toolchain() -> CheckResult:
    """检查 C++/Python 共享协议所需的 protoc、gRPC 插件和 Python runtime。"""

    protoc = shutil.which("protoc")
    grpc_plugin = shutil.which("grpc_cpp_plugin")
    missing = [
        name
        for name, path in (("protoc", protoc), ("grpc_cpp_plugin", grpc_plugin))
        if path is None
    ]
    if missing:
        return CheckResult(
            name="protobuf-grpc-cxx",
            status=CheckStatus.FAIL,
            detail="missing tools: " + ", ".join(missing),
            remediation=(
                "install the platform Protobuf and gRPC C++ development packages; "
                "on MSYS2 UCRT64 use mingw-w64-ucrt-x86_64-grpc"
            ),
        )
    try:
        # grpc_cpp_plugin 没有跨版本统一的 --version 选项；确认它可执行由 which 完成，
        # 版本兼容性则在 CMake 生成和真实跨语言 Demo 中验证。
        protoc_version = _first_line([str(protoc), "--version"])
        grpcio_version = importlib.metadata.version("grpcio")
    except (
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        return CheckResult(
            name="protobuf-grpc-cxx",
            status=CheckStatus.FAIL,
            detail=f"RPC toolchain is incomplete: {exc}",
            remediation="run uv sync and install the C++ gRPC development package",
        )
    return CheckResult(
        name="protobuf-grpc-cxx",
        status=CheckStatus.PASS,
        detail=(
            f"{protoc_version}; grpc_cpp_plugin={grpc_plugin}; "
            f"Python grpcio {grpcio_version}"
        ),
    )


def run_doctor(config_path: str | Path, *, strict_python: bool = True) -> DoctorReport:
    """运行全部初始化检查，并生成确定顺序的报告。"""

    checks = (
        check_python(strict=strict_python),
        check_package(),
        check_config(Path(config_path)),
        check_tool(
            "uv",
            ("uv",),
            ("--version",),
            "install uv and ensure it is available on PATH",
        ),
        check_tool(
            "cmake",
            ("cmake",),
            ("--version",),
            "install CMake 3.25+ and ensure it is available on PATH",
        ),
        check_cxx_compiler(),
        check_opencv(),
        check_rpc_toolchain(),
    )
    ready = all(check.status is not CheckStatus.FAIL for check in checks)
    return DoctorReport(checks=checks, ready=ready)


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "check_config",
    "check_cxx_compiler",
    "check_opencv",
    "check_rpc_toolchain",
    "check_package",
    "check_python",
    "check_tool",
    "run_doctor",
]
