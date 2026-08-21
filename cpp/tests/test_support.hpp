#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 提供第 9 章 C++ 测试共用的极小检查器。每次 check 失败都会打印位置和原因，main
 * 最终返回非零，让 CTest 在 Debug/Release 中都能发现失败。
 *
 * 使用的技术栈：C++20 source_location、iostream、string_view。
 * 调用流程：测试 main -> TestContext::check -> failures() -> 进程退出码 -> CTest。
 * 边界：它不是要替代 GoogleTest/Catch2；课程此处只需少量明确断言，避免网络下载。
 */

#include <iostream>
#include <source_location>
#include <string_view>

namespace realsight::testing {

class TestContext {
 public:
  void check(
      const bool condition,
      const std::string_view message,
      const std::source_location location = std::source_location::current()) {
    if (condition) {
      return;
    }
    ++failures_;
    std::cerr << location.file_name() << ':' << location.line()
              << " check failed: " << message << '\n';
  }

  [[nodiscard]] int failures() const noexcept {
    return failures_;
  }

 private:
  int failures_{0};
};

}  // namespace realsight::testing
