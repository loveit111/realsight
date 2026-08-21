/*
 * 文件整体逻辑
 * -----------
 * 确定性验证有界队列的容量、drop_oldest、block_producer 取消和 close 排空语义。
 *
 * 使用的技术栈：C++20 stop_source、type_traits；OpenCV cv::Mat 生成微型测试帧。
 * 调用流程：make_frame -> push/close/pop -> TestContext -> CTest。
 * 边界：本测试不启动摄像头，也不依赖线程调度速度。
 */

#include "realsight/runtime/frame_queue.hpp"
#include "test_support.hpp"

#include <chrono>
#include <cstdint>
#include <stop_token>
#include <type_traits>

#include <opencv2/core.hpp>

namespace {

realsight::runtime::Frame make_frame(const std::uint64_t sequence) {
  cv::Mat pixels(2, 3, CV_8UC3,
                 cv::Scalar(sequence % 255, sequence % 255, sequence % 255));
  return realsight::runtime::Frame(
      sequence,
      realsight::runtime::MonotonicClock::time_point(
          std::chrono::milliseconds(sequence)),
      std::chrono::milliseconds(sequence * 10), std::move(pixels));
}

}  // namespace

int main() {
  using realsight::runtime::BoundedFrameQueue;
  using realsight::runtime::OverflowPolicy;
  realsight::testing::TestContext test;

  static_assert(!std::is_copy_constructible_v<realsight::runtime::Frame>);
  static_assert(std::is_move_constructible_v<realsight::runtime::Frame>);

  bool rejected_zero_capacity = false;
  try {
    BoundedFrameQueue invalid(0, OverflowPolicy::drop_oldest);
  } catch (const std::invalid_argument&) {
    rejected_zero_capacity = true;
  }
  test.check(rejected_zero_capacity, "zero-capacity queue must be rejected");

  BoundedFrameQueue latest_queue(2, OverflowPolicy::drop_oldest);
  const auto first = latest_queue.push(make_frame(0), {});
  const auto second = latest_queue.push(make_frame(1), {});
  const auto third = latest_queue.push(make_frame(2), {});
  test.check(first.accepted && second.accepted && third.accepted,
             "drop-oldest pushes must be accepted");
  test.check(third.dropped_frames == 1,
             "third push must evict exactly one oldest frame");
  auto remaining_one = latest_queue.try_pop();
  auto remaining_two = latest_queue.try_pop();
  test.check(remaining_one.has_value() && remaining_one->sequence == 1,
             "queue must retain sequence 1 after evicting sequence 0");
  test.check(remaining_two.has_value() && remaining_two->sequence == 2,
             "newest sequence must be retained");

  BoundedFrameQueue blocking_queue(1, OverflowPolicy::block_producer);
  test.check(blocking_queue.push(make_frame(10), {}).accepted,
             "first blocking push must fit");
  std::stop_source cancelled;
  cancelled.request_stop();
  const auto cancelled_push =
      blocking_queue.push(make_frame(11), cancelled.get_token());
  test.check(!cancelled_push.accepted && cancelled_push.stopped,
             "full blocking push must honor cancellation");

  blocking_queue.close();
  auto drained = blocking_queue.wait_pop({});
  auto after_drain = blocking_queue.wait_pop({});
  test.check(drained.has_value() && drained->sequence == 10,
             "close must still allow draining queued frame");
  test.check(!after_drain.has_value(),
             "closed and empty queue must return no frame");
  const auto after_close = blocking_queue.push(make_frame(12), {});
  test.check(!after_close.accepted && after_close.closed,
             "closed queue must reject new frame");

  BoundedFrameQueue cancelled_latest(1, OverflowPolicy::drop_oldest);
  test.check(cancelled_latest.push(make_frame(20), {}).accepted,
             "drop queue must accept its first frame");
  const auto cancelled_latest_push =
      cancelled_latest.push(make_frame(21), cancelled.get_token());
  auto preserved = cancelled_latest.try_pop();
  test.check(!cancelled_latest_push.accepted && cancelled_latest_push.stopped,
             "pre-cancelled drop push must be rejected");
  test.check(preserved.has_value() && preserved->sequence == 20,
             "cancelled push must not evict an existing frame");

  return test.failures() == 0 ? 0 : 1;
}
