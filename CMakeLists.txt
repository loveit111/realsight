# 文件整体逻辑
# ------------
# 这是 RealSight C++ 单仓工程的顶层构建入口。它声明项目版本、启用 CTest，再把实际
# C++ 目标交给 cpp/CMakeLists.txt。顶层文件只管理全局工程关系，不罗列源文件。
#
# 使用的技术栈：CMake 3.25+、C++20、CTest、OpenCV、Protobuf 与 gRPC targets。
# 调用流程：cmake -S/-B -> project() -> include(CTest) -> add_subdirectory(cpp)。
# 边界：Python stub 继续由 uv/pyproject.toml 和专用生成脚本管理；顶层不罗列子模块源文件。

cmake_minimum_required(VERSION 3.25)

project(
  RealSight
  VERSION 0.1.0
  DESCRIPTION "Evidence-driven active perception teaching project"
  LANGUAGES CXX
)

include(CTest)
add_subdirectory(cpp)
