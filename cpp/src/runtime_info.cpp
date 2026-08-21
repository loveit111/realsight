/*
 * 文件整体逻辑
 * -----------
 * 构造 RealSight C++ 运行时的版本/能力快照，并把它序列化为合法 JSON。第 9 章把
 * camera_capture 和 video_replay 从 planned 移到 implemented，quality_scoring 保留。
 *
 * 使用的技术栈：C++20 字符串、vector、ostringstream。
 * 调用流程：CLI --describe -> describe_runtime -> to_json -> stdout。
 * 边界：只处理自身固定文本；任务状态和 Observation 使用第 4 章正式契约。
 */

#include "realsight/runtime/runtime_info.hpp"

#include <sstream>

namespace realsight::runtime {
namespace {

std::string escape_json(const std::string& value) {
  std::string escaped;
  escaped.reserve(value.size());
  for (const char character : value) {
    switch (character) {
      case '\\':
        escaped += "\\\\";
        break;
      case '"':
        escaped += "\\\"";
        break;
      case '\n':
        escaped += "\\n";
        break;
      default:
        escaped += character;
    }
  }
  return escaped;
}

void write_string_array(
    std::ostringstream& output,
    const std::vector<std::string>& values) {
  output << '[';
  for (std::size_t index = 0; index < values.size(); ++index) {
    if (index != 0) {
      output << ',';
    }
    output << '"' << escape_json(values[index]) << '"';
  }
  output << ']';
}

}  // namespace

RuntimeInfo describe_runtime() {
  return RuntimeInfo{
      .service_name = "realsight-perception-runtime",
      .version = "0.1.0",
      .cxx_standard = 20,
      .implemented_capabilities = {"video_replay", "camera_capture",
                                   "bounded_frame_queue",
                                   "cooperative_stop"},
      .planned_capabilities = {"quality_scoring", "observation_stream"},
  };
}

std::string to_json(const RuntimeInfo& info) {
  std::ostringstream output;
  output << "{\"service_name\":\"" << escape_json(info.service_name)
         << "\",\"version\":\"" << escape_json(info.version)
         << "\",\"cxx_standard\":" << info.cxx_standard
         << ",\"implemented_capabilities\":";
  write_string_array(output, info.implemented_capabilities);
  output << ",\"planned_capabilities\":";
  write_string_array(output, info.planned_capabilities);
  output << '}';
  return output.str();
}

}  // namespace realsight::runtime
