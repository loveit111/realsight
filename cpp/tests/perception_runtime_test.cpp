/*
 * 文件整体逻辑
 * -----------
 * 使用内存合成源验证 PerceptionRuntime 的生产/消费、丢帧、上限、错误和外部停止。
 * 合成源让测试不依赖摄像头、编解码器或网络，并能精确控制返回的序号和终态。
 *
 * 使用的技术栈：C++20 jthread/stop_token/chrono；OpenCV Mat；CTest 显式检查。
 * 调用流程：SyntheticFrameSource -> PerceptionRuntime -> consumer -> RuntimeSummary。
 * 边界：OpenCV 视频文件适配器由 opencv_source_test.cpp 单独验证。
 */

#include "realsight/runtime/perception_runtime.hpp"
#include "test_support.hpp"

#include <chrono>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <stop_token>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <opencv2/core.hpp>

namespace {

class SyntheticFrameSource final : public realsight::runtime::FrameSource {
 public:
  explicit SyntheticFrameSource(
      const std::uint64_t frame_count,
      std::optional<std::uint64_t> fail_before_sequence = std::nullopt)
      : frame_count_(frame_count),
        fail_before_sequence_(fail_before_sequence),
        descriptor_{.kind = realsight::runtime::SourceKind::synthetic,
                    .label = "test-synthetic",
                    .backend = "memory",
                    .width = 4,
                    .height = 3,
                    .frames_per_second = 1000.0} {}

  [[nodiscard]] const realsight::runtime::SourceDescriptor& descriptor()
      const noexcept override {
    return descriptor_;
  }

  [[nodiscard]] realsight::runtime::SourceReadResult read(
      const std::stop_token stop_token) override {
    if (stop_token.stop_requested()) {
      return realsight::runtime::SourceReadResult::stopped();
    }
    if (fail_before_sequence_.has_value() &&
        sequence_ == *fail_before_sequence_) {
      return realsight::runtime::SourceReadResult::error("synthetic source failed");
    }
    if (sequence_ >= frame_count_) {
      return realsight::runtime::SourceReadResult::end();
    }

    cv::Mat pixels(3, 4, CV_8UC3,
                   cv::Scalar(sequence_ % 255, 10, 20));
    auto timestamp = realsight::runtime::MonotonicClock::now();
    if (timestamp <= last_timestamp_) {
      timestamp = last_timestamp_ + std::chrono::nanoseconds(1);
    }
    last_timestamp_ = timestamp;
    realsight::runtime::Frame frame(
        sequence_, timestamp, std::chrono::milliseconds(sequence_),
        std::move(pixels));
    ++sequence_;
    return realsight::runtime::SourceReadResult::with_frame(std::move(frame));
  }

 private:
  std::uint64_t frame_count_;
  std::optional<std::uint64_t> fail_before_sequence_;
  realsight::runtime::SourceDescriptor descriptor_;
  std::uint64_t sequence_{0};
  realsight::runtime::MonotonicClock::time_point last_timestamp_{};
};

class ThrowingFrameSource final : public realsight::runtime::FrameSource {
 public:
  [[nodiscard]] const realsight::runtime::SourceDescriptor& descriptor()
      const noexcept override {
    return descriptor_;
  }

  [[nodiscard]] realsight::runtime::SourceReadResult read(
      std::stop_token) override {
    throw std::runtime_error("unexpected adapter exception");
  }

 private:
  realsight::runtime::SourceDescriptor descriptor_{
      .kind = realsight::runtime::SourceKind::synthetic,
      .label = "throwing-source",
      .backend = "memory",
      .width = 1,
      .height = 1,
      .frames_per_second = 1.0,
  };
};

}  // namespace

int main() {
  using realsight::runtime::ConsumerDecision;
  using realsight::runtime::OverflowPolicy;
  using realsight::runtime::PerceptionRuntime;
  using realsight::runtime::RuntimeStopReason;
  realsight::testing::TestContext test;

  {
    SyntheticFrameSource source(6);
    PerceptionRuntime runtime({.queue_capacity = 2,
                               .overflow_policy =
                                   OverflowPolicy::block_producer,
                               .max_consumed_frames = std::nullopt});
    std::vector<std::uint64_t> sequences;
    const auto summary = runtime.run(
        source, [&](realsight::runtime::Frame&& frame) {
          sequences.push_back(frame.sequence);
          return ConsumerDecision::continue_running;
        });
    test.check(summary.stop_reason == RuntimeStopReason::source_exhausted,
               "finite source must finish as source_exhausted");
    test.check(summary.produced_frames == 6 && summary.consumed_frames == 6,
               "block policy must consume all six frames");
    test.check(summary.dropped_frames == 0,
               "block policy must not drop frames");
    test.check(sequences == std::vector<std::uint64_t>({0, 1, 2, 3, 4, 5}),
               "block policy must preserve sequence order");
  }

  {
    SyntheticFrameSource source(100);
    PerceptionRuntime runtime(
        {.queue_capacity = 2,
         .overflow_policy = OverflowPolicy::drop_oldest,
         .max_consumed_frames = std::nullopt});
    const auto summary = runtime.run(
        source, [](realsight::runtime::Frame&&) {
          std::this_thread::sleep_for(std::chrono::milliseconds(2));
          return ConsumerDecision::continue_running;
        });
    test.check(summary.stop_reason == RuntimeStopReason::source_exhausted,
               "drop test source must still end normally");
    test.check(summary.dropped_frames > 0,
               "slow consumer must cause observable drop-oldest events");
    test.check(summary.consumed_frames + summary.dropped_frames ==
                   summary.produced_frames,
               "every produced frame must be consumed or dropped");
    test.check(summary.last_consumed_sequence.has_value() &&
                   *summary.last_consumed_sequence == 99,
               "freshness policy must retain final frame");
  }

  {
    SyntheticFrameSource source(20);
    PerceptionRuntime runtime({
        .queue_capacity = 1,
        .overflow_policy = OverflowPolicy::block_producer,
        .max_consumed_frames = 3,
    });
    const auto summary = runtime.run(
        source, [](realsight::runtime::Frame&&) {
          return ConsumerDecision::continue_running;
        });
    test.check(summary.stop_reason == RuntimeStopReason::max_frames_reached,
               "runtime must report max frame stop");
    test.check(summary.consumed_frames == 3,
               "max frame stop must happen after three consumed frames");
  }

  {
    SyntheticFrameSource source(10, 0);
    PerceptionRuntime runtime({});
    const auto summary = runtime.run(
        source, [](realsight::runtime::Frame&&) {
          return ConsumerDecision::continue_running;
        });
    test.check(summary.stop_reason == RuntimeStopReason::source_error,
               "source failure must be preserved");
    test.check(summary.error_message == "synthetic source failed",
               "source error message must remain actionable");
  }

  {
    SyntheticFrameSource source(2);
    PerceptionRuntime runtime({.queue_capacity = 1,
                               .overflow_policy =
                                   OverflowPolicy::block_producer,
                               .max_consumed_frames = std::nullopt});
    const auto summary = runtime.run(
        source, [](realsight::runtime::Frame&&) -> ConsumerDecision {
          throw std::runtime_error("consumer failed");
        });
    test.check(summary.stop_reason == RuntimeStopReason::consumer_error,
               "consumer exception must become consumer_error");
    test.check(summary.error_message == "consumer failed",
               "consumer exception text must be preserved");
  }

  {
    ThrowingFrameSource source;
    PerceptionRuntime runtime({});
    const auto summary = runtime.run(
        source, [](realsight::runtime::Frame&&) {
          return ConsumerDecision::continue_running;
        });
    test.check(summary.stop_reason == RuntimeStopReason::source_error,
               "thrown source exception must become source_error");
    test.check(summary.error_message.find("unexpected adapter exception") !=
                   std::string::npos,
               "thrown source exception text must remain visible");
  }

  {
    SyntheticFrameSource source(50);
    PerceptionRuntime runtime({});
    std::stop_source external;
    external.request_stop();
    const auto summary = runtime.run(
        source,
        [](realsight::runtime::Frame&&) {
          return ConsumerDecision::continue_running;
        },
        external.get_token());
    test.check(summary.stop_reason == RuntimeStopReason::external_stop,
               "pre-requested stop token must stop the runtime");
    test.check(summary.consumed_frames == 0,
               "pre-requested stop must consume no frames");
  }

  return test.failures() == 0 ? 0 : 1;
}
