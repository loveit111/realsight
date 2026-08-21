#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 定义运行时自描述对象，区分“本章已经实现”和“后续计划实现”的能力，避免把路线图
 * 当成当前能力声明。
 *
 * 使用的技术栈：C++20 struct、vector、string；手写小型 JSON 输出用于 CLI 自检。
 * 调用流程：describe_runtime -> RuntimeInfo -> to_json -> realsight-perception --describe。
 * 边界：这不是跨语言协议；第 10 章的服务能力会通过 Protobuf 正式表达。
 */

#include <string>
#include <vector>

namespace realsight::runtime {

struct RuntimeInfo {
  std::string service_name;
  std::string version;
  int cxx_standard;
  std::vector<std::string> implemented_capabilities;
  std::vector<std::string> planned_capabilities;
};

[[nodiscard]] RuntimeInfo describe_runtime();
[[nodiscard]] std::string to_json(const RuntimeInfo& info);

}  // namespace realsight::runtime
