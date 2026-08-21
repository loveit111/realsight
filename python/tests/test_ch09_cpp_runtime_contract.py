"""
第 9 章 C++ 实时运行时的轻量工程契约测试。

这个文件的整体逻辑
------------------
C++ 行为由 CTest 真正编译运行，但 Python 很适合检查“仓库结构是否忘了某个教学约束”。
本文件读取 C++/CMake 源码，确认每个第 9 章文件都有顶部学习说明、OpenCV 已进入正确
target、后续 gRPC 没有污染 runtime、Release 测试不依赖 assert，并单测 doctor 分支。

使用的技术栈
------------
- Python 3.12 标准库 pathlib：以 UTF-8 读取源码。
- pytest 9 和 unittest.mock：隔离本机 pkg-config，确定性测试 doctor 分支。
- RealSight doctor：复用正式环境报告类型。

调用流程
--------
pytest
    -> 扫描第 9 章 C++/CMake 文件的顶部注释
    -> 检查构建目标和章节边界
    -> 模拟 pkg-config 成功/缺失
    -> 将失败位置交给 pytest 报告。

边界
----
源码文本测试不能替代 C++ 编译、线程测试或视频解码测试，所以它只检查结构约束；真实
行为仍由四个 CTest 程序和命令行 Demo 证明。
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from realsight.doctor import CheckStatus, check_opencv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_chapter_nine_cpp_files_have_learning_headers() -> None:
    """新增 C++ 文件顶部必须说明逻辑、技术栈和调用流程。"""

    paths = [
        *sorted((PROJECT_ROOT / "cpp" / "include").rglob("*.hpp")),
        *sorted((PROJECT_ROOT / "cpp" / "src").glob("*.cpp")),
        *sorted((PROJECT_ROOT / "cpp" / "apps").glob("*.cpp")),
        *sorted((PROJECT_ROOT / "cpp" / "tools").glob("*.cpp")),
        *sorted((PROJECT_ROOT / "cpp" / "tests").rglob("*.cpp")),
    ]
    assert paths
    for path in paths:
        header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:35])
        assert "文件整体逻辑" in header, path
        assert "使用的技术栈" in header, path
        assert "调用流程" in header, path


def test_cmake_files_explain_flow_and_keep_runtime_transport_separate() -> None:
    """CMake 解释构建流程；第 10 章 gRPC 只链接独立 transport target。"""

    root_cmake = (PROJECT_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    cpp_cmake = (PROJECT_ROOT / "cpp" / "CMakeLists.txt").read_text(encoding="utf-8")
    for content in (root_cmake, cpp_cmake):
        assert "文件整体逻辑" in content[:1200]
        assert "使用的技术栈" in content[:1200]
        assert "调用流程" in content[:1200]
    assert "find_package(OpenCV REQUIRED" in cpp_cmake
    assert "RealSight::runtime" in cpp_cmake
    assert "find_package(gRPC CONFIG REQUIRED)" in cpp_cmake
    runtime_section = cpp_cmake.split("realsight_runtime STATIC", maxsplit=1)[1]
    runtime_section = runtime_section.split(
        "add_library(realsight_transport", maxsplit=1
    )[0]
    assert "gRPC::" not in runtime_section


def test_cpp_tests_do_not_depend_on_release_disabled_assert() -> None:
    """行为检查必须在 Release/NDEBUG 下继续执行。"""

    test_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((PROJECT_ROOT / "cpp" / "tests").glob("*.cpp"))
    )
    assert "#include <cassert>" not in test_sources
    assert re.search(r"(?<!_)\bassert\(", test_sources) is None
    assert "TestContext" in test_sources


def test_doctor_reports_opencv_version_from_pkg_config() -> None:
    """正式自检器应识别 C++ OpenCV，而不是误把 Python cv2 当开发包。"""

    with (
        patch("realsight.doctor.shutil.which", return_value="pkg-config"),
        patch("realsight.doctor._first_line", return_value="5.0.0"),
    ):
        result = check_opencv()

    assert result.status is CheckStatus.PASS
    assert "5.0.0" in result.detail


def test_doctor_explains_missing_pkg_config() -> None:
    """缺少开发元数据时，给出可执行修复方向。"""

    with patch("realsight.doctor.shutil.which", return_value=None):
        result = check_opencv()

    assert result.status is CheckStatus.FAIL
    assert result.remediation is not None
    assert "OpenCV" in result.remediation
