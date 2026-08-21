/*
 * 文件整体逻辑
 * -----------
 * 实现 Frame 的构造、有效性检查和像素字节数计算。构造函数接收 cv::Mat 的值并移动
 * 到成员中，采集源之后不再持有这帧的逻辑所有权。
 *
 * 使用的技术栈：C++20 移动语义、OpenCV cv::Mat。
 * 调用流程：FrameSource 构造 Frame -> 队列移动 Frame -> 消费者读取元数据和像素。
 * 边界：这里只验证矩阵存在和二维尺寸，不做模糊、曝光等第 10 章质量判断。
 */

#include "realsight/runtime/frame.hpp"

#include <utility>

namespace realsight::runtime {

Frame::Frame(
    const std::uint64_t sequence_value,
    const MonotonicClock::time_point captured_at_value,
    const std::optional<std::chrono::milliseconds> source_position_value,
    cv::Mat pixels_value)
    : sequence(sequence_value),
      captured_at(captured_at_value),
      captured_at_utc(WallClock::now()),
      source_position(source_position_value),
      pixels(std::move(pixels_value)) {}

bool Frame::valid() const noexcept {
  return !pixels.empty() && pixels.rows > 0 && pixels.cols > 0;
}

std::size_t Frame::byte_size() const noexcept {
  return pixels.total() * pixels.elemSize();
}

}  // namespace realsight::runtime
