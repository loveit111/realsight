#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件定义 RealSight C++ 感知运行时中最小、最高频的数据对象 Frame（帧）。
 * 一帧不仅是图片，还必须带有递增序号、单调时钟采集时间和可选的视频源位置。
 * 这样后续模块才能判断先后顺序、计算延迟，并把画面追溯到预录视频中的位置。
 *
 * 使用的技术栈
 * -------------
 * - C++20：移动语义、std::chrono、std::optional。
 * - OpenCV：cv::Mat 保存像素内存；本章只用 core 数据结构，不在这里做视觉算法。
 *
 * 调用流程
 * --------
 * OpenCvFrameSource 读取 cv::Mat
 * -> 构造 Frame
 * -> 以移动方式进入 BoundedFrameQueue
 * -> PerceptionRuntime 将 Frame 移交给消费者
 * -> 第 10 章的质量评分器消费像素并产生 Observation。
 *
 * 重要边界
 * --------
 * Frame 禁止复制，只允许移动。这样可以减少大图像被无意复制，也能让初学者看清
 * 一帧在“采集线程 -> 队列 -> 消费线程”之间只有一个逻辑所有者。
 */

#include <chrono>
#include <cstdint>
#include <optional>

#include <opencv2/core/mat.hpp>

namespace realsight::runtime {

using MonotonicClock = std::chrono::steady_clock;
using WallClock = std::chrono::system_clock;

struct Frame {
  std::uint64_t sequence;
  MonotonicClock::time_point captured_at;
  WallClock::time_point captured_at_utc;
  std::optional<std::chrono::milliseconds> source_position;
  cv::Mat pixels;

  Frame(std::uint64_t sequence_value,
        MonotonicClock::time_point captured_at_value,
        std::optional<std::chrono::milliseconds> source_position_value,
        cv::Mat pixels_value);

  Frame(const Frame&) = delete;
  Frame& operator=(const Frame&) = delete;
  Frame(Frame&&) noexcept = default;
  Frame& operator=(Frame&&) noexcept = default;
  ~Frame() = default;

  [[nodiscard]] bool valid() const noexcept;
  [[nodiscard]] std::size_t byte_size() const noexcept;
};

}  // namespace realsight::runtime
