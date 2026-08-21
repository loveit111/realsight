/*
 * 文件整体逻辑
 * -----------
 * 验证运行时能力清单和 JSON，不再使用会在 Release/NDEBUG 下消失的 assert。
 *
 * 使用的技术栈：C++20 ranges、CTest、项目内显式 TestContext。
 * 调用流程：describe_runtime -> 检查 implemented/planned -> to_json -> 返回失败数。
 * 边界：这里只验证能力声明；帧行为由其他三组测试负责。
 */

#include "realsight/runtime/runtime_info.hpp"
#include "test_support.hpp"

#include <algorithm>
#include <string>

int main() {
  realsight::testing::TestContext test;
  const auto info = realsight::runtime::describe_runtime();

  test.check(info.service_name == "realsight-perception-runtime",
             "service name must remain stable");
  test.check(info.cxx_standard == 20, "runtime must declare C++20");
  test.check(
      std::ranges::find(info.implemented_capabilities, "camera_capture") !=
          info.implemented_capabilities.end(),
      "camera capture must be implemented in Chapter 9");
  test.check(
      std::ranges::find(info.implemented_capabilities, "video_replay") !=
          info.implemented_capabilities.end(),
      "video replay must be implemented in Chapter 9");
  test.check(
      std::ranges::find(info.planned_capabilities, "quality_scoring") !=
          info.planned_capabilities.end(),
      "quality scoring must remain planned for Chapter 10");

  const std::string json = realsight::runtime::to_json(info);
  test.check(json.find("\"cxx_standard\":20") != std::string::npos,
             "JSON must include C++ standard");
  test.check(json.find("\"implemented_capabilities\"") != std::string::npos,
             "JSON must distinguish implemented capabilities");
  return test.failures() == 0 ? 0 : 1;
}
