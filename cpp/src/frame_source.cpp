/*
 * 文件整体逻辑
 * -----------
 * 集中实现 FrameSource 结果对象的四个工厂函数和枚举到文本的转换。集中构造可以保证
 * “有帧时必须携带 Frame、错误时必须携带消息”这一基本约定在所有适配器中一致。
 *
 * 使用的技术栈：C++20 std::optional、移动语义和强类型枚举。
 * 调用流程：具体源创建 SourceReadResult -> PerceptionRuntime switch(status) -> 统计/停止。
 * 边界：不打开设备、不处理线程，也不把内部状态直接映射为 gRPC 状态码。
 */

#include "realsight/runtime/frame_source.hpp"

#include <stdexcept>
#include <utility>

namespace realsight::runtime {

SourceReadResult SourceReadResult::with_frame(Frame value) {
  if (!value.valid()) {
    throw std::invalid_argument("SourceReadResult cannot contain an invalid frame");
  }
  return SourceReadResult{
      .status = SourceReadStatus::frame,
      .frame = std::move(value),
      .error_message = {},
  };
}

SourceReadResult SourceReadResult::end() {
  return SourceReadResult{
      .status = SourceReadStatus::end_of_stream,
      .frame = std::nullopt,
      .error_message = {},
  };
}

SourceReadResult SourceReadResult::stopped() {
  return SourceReadResult{
      .status = SourceReadStatus::stopped,
      .frame = std::nullopt,
      .error_message = {},
  };
}

SourceReadResult SourceReadResult::error(std::string message) {
  if (message.empty()) {
    message = "frame source failed without an error message";
  }
  return SourceReadResult{
      .status = SourceReadStatus::error,
      .frame = std::nullopt,
      .error_message = std::move(message),
  };
}

std::string to_string(const SourceKind kind) {
  switch (kind) {
    case SourceKind::synthetic:
      return "synthetic";
    case SourceKind::video_file:
      return "video_file";
    case SourceKind::camera:
      return "camera";
  }
  return "unknown";
}

std::string to_string(const SourceReadStatus status) {
  switch (status) {
    case SourceReadStatus::frame:
      return "frame";
    case SourceReadStatus::end_of_stream:
      return "end_of_stream";
    case SourceReadStatus::stopped:
      return "stopped";
    case SourceReadStatus::error:
      return "error";
  }
  return "unknown";
}

}  // namespace realsight::runtime
