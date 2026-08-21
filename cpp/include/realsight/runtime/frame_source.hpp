#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件定义“帧从哪里来”的抽象接口。摄像头和视频文件虽然打开方式不同，运行时真正
 * 关心的却只是：源的描述是什么，以及下一次读取返回帧、结束、停止还是错误。
 *
 * 使用的技术栈
 * -------------
 * - C++20：纯虚接口、std::stop_token、std::optional 和移动语义。
 * - 不直接依赖具体摄像头 API：OpenCV 细节留在 opencv_frame_source.hpp/.cpp。
 *
 * 调用流程
 * --------
 * CLI 选择视频或摄像头
 * -> 创建某个 FrameSource 实现
 * -> PerceptionRuntime 循环调用 read(stop_token)
 * -> SourceReadResult 明确表达四种结果
 * -> 运行时据此关闭队列或传播错误。
 *
 * 重要边界
 * --------
 * “视频读完”不是错误；“用户请求停止”也不是错误。使用枚举而不是一个 bool，避免把
 * 正常结束、取消和设备故障混成同一种 false。
 */

#include <optional>
#include <stop_token>
#include <string>

#include "realsight/runtime/frame.hpp"

namespace realsight::runtime {

enum class SourceKind { synthetic, video_file, camera };

struct SourceDescriptor {
  SourceKind kind;
  std::string label;
  std::string backend;
  int width;
  int height;
  double frames_per_second;
};

enum class SourceReadStatus { frame, end_of_stream, stopped, error };

struct SourceReadResult {
  SourceReadStatus status;
  std::optional<Frame> frame;
  std::string error_message;

  [[nodiscard]] static SourceReadResult with_frame(Frame value);
  [[nodiscard]] static SourceReadResult end();
  [[nodiscard]] static SourceReadResult stopped();
  [[nodiscard]] static SourceReadResult error(std::string message);
};

class FrameSource {
 public:
  virtual ~FrameSource() = default;

  [[nodiscard]] virtual const SourceDescriptor& descriptor() const noexcept = 0;
  [[nodiscard]] virtual SourceReadResult read(std::stop_token stop_token) = 0;
};

[[nodiscard]] std::string to_string(SourceKind kind);
[[nodiscard]] std::string to_string(SourceReadStatus status);

}  // namespace realsight::runtime
