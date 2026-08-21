#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件定义第 9 章的连续帧编排器 PerceptionRuntime。它启动一个采集线程，把 Frame
 * 放入有界队列；调用 run() 的线程负责消费帧，并最终返回可审计的统计摘要。
 *
 * 使用的技术栈
 * -------------
 * - C++20：std::jthread、std::stop_source/stop_token、std::function、chrono。
 * - 本项目 FrameSource 与 BoundedFrameQueue：隔离设备读取和并发策略。
 *
 * 调用流程
 * --------
 * PerceptionRuntime::run(source, consumer, external_stop)
 * -> jthread 持续 source.read()
 * -> BoundedFrameQueue
 * -> consumer(Frame&&)
 * -> 到达 EOF、上限、消费方停止、外部取消或错误
 * -> join 采集线程
 * -> 返回 RuntimeSummary。
 *
 * 重要边界
 * --------
 * 本章 consumer 只是 C++ 回调；第 10 章会在这里接质量评分和 Observation 生成，但
 * gRPC/Python Agent 不进入本类。某些摄像头后端的阻塞 read 不能被 stop_token 强制
 * 打断，运行时只能在该次驱动调用返回后完成停止，这是本章明确保留的能力边界。
 */

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <stop_token>
#include <string>

#include "realsight/runtime/frame_queue.hpp"
#include "realsight/runtime/frame_source.hpp"

namespace realsight::runtime {

enum class ConsumerDecision { continue_running, stop };

enum class RuntimeStopReason {
  source_exhausted,
  max_frames_reached,
  consumer_requested,
  external_stop,
  source_error,
  consumer_error,
};

struct RuntimeOptions {
  std::size_t queue_capacity{4};
  OverflowPolicy overflow_policy{OverflowPolicy::drop_oldest};
  std::optional<std::uint64_t> max_consumed_frames;
};

struct RuntimeSummary {
  RuntimeStopReason stop_reason;
  std::uint64_t produced_frames;
  std::uint64_t consumed_frames;
  std::uint64_t dropped_frames;
  std::optional<std::uint64_t> last_consumed_sequence;
  std::chrono::milliseconds elapsed;
  std::string error_message;
};

using FrameConsumer = std::function<ConsumerDecision(Frame&&)>;

class PerceptionRuntime {
 public:
  explicit PerceptionRuntime(RuntimeOptions options);

  [[nodiscard]] RuntimeSummary run(
      FrameSource& source,
      const FrameConsumer& consumer,
      std::stop_token external_stop_token = {});

 private:
  RuntimeOptions options_;
};

[[nodiscard]] std::string to_string(RuntimeStopReason reason);
[[nodiscard]] std::string to_json(const RuntimeSummary& summary);

}  // namespace realsight::runtime
