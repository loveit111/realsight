/*
 * 文件整体逻辑
 * -----------
 * 实现有界帧队列的两个溢出策略、可取消等待和关闭语义。所有共享状态只在 mutex 保护
 * 下访问；条件变量只负责等待“非空”或“未满”，不承载业务状态。
 *
 * 使用的技术栈：C++20 mutex、condition_variable_any、stop_token、deque。
 * 调用流程：采集线程 push -> 通知 not_empty -> 消费线程 wait_pop -> 通知 not_full。
 * 边界：队列关闭后拒绝新帧，但允许消费者排空已存在帧；停止令牌则让等待尽快返回。
 */

#include "realsight/runtime/frame_queue.hpp"

#include <stdexcept>
#include <utility>

namespace realsight::runtime {

BoundedFrameQueue::BoundedFrameQueue(
    const std::size_t capacity,
    const OverflowPolicy overflow_policy)
    : capacity_(capacity), overflow_policy_(overflow_policy) {
  if (capacity == 0) {
    throw std::invalid_argument("frame queue capacity must be greater than zero");
  }
}

QueuePushResult BoundedFrameQueue::push(
    Frame frame,
    const std::stop_token stop_token) {
  std::unique_lock lock(mutex_);
  std::size_t dropped_frames = 0;

  // 取消或关闭必须先于溢出处理，否则一次已取消的 push 仍可能误删队列旧帧。
  if (closed_) {
    return {.accepted = false,
            .stopped = false,
            .closed = true,
            .dropped_frames = 0};
  }
  if (stop_token.stop_requested()) {
    return {.accepted = false,
            .stopped = true,
            .closed = false,
            .dropped_frames = 0};
  }

  if (overflow_policy_ == OverflowPolicy::block_producer) {
    // stop_token 版本的 wait 会在取消时返回 false，不需要轮询或短周期 sleep。
    const bool can_push = not_full_.wait(
        lock, stop_token, [this] { return closed_ || frames_.size() < capacity_; });
    if (!can_push) {
      return {.accepted = false,
              .stopped = true,
              .closed = false,
              .dropped_frames = 0};
    }
    if (closed_) {
      return {.accepted = false,
              .stopped = false,
              .closed = true,
              .dropped_frames = 0};
    }
    if (stop_token.stop_requested()) {
      return {.accepted = false,
              .stopped = true,
              .closed = false,
              .dropped_frames = 0};
    }
  } else if (frames_.size() == capacity_) {
    // 实时策略保留最新画面：先释放最旧 Frame，再移动放入新 Frame。
    frames_.pop_front();
    dropped_frames = 1;
  }

  if (closed_) {
    return {.accepted = false,
            .stopped = false,
            .closed = true,
            .dropped_frames = dropped_frames};
  }
  frames_.push_back(std::move(frame));
  lock.unlock();
  not_empty_.notify_one();
  return {.accepted = true,
          .stopped = false,
          .closed = false,
          .dropped_frames = dropped_frames};
}

std::optional<Frame> BoundedFrameQueue::wait_pop(
    const std::stop_token stop_token) {
  std::unique_lock lock(mutex_);
  const bool can_pop = not_empty_.wait(
      lock, stop_token, [this] { return closed_ || !frames_.empty(); });
  if (!can_pop || frames_.empty()) {
    return std::nullopt;
  }

  Frame frame = std::move(frames_.front());
  frames_.pop_front();
  lock.unlock();
  not_full_.notify_one();
  return frame;
}

std::optional<Frame> BoundedFrameQueue::try_pop() {
  std::scoped_lock lock(mutex_);
  if (frames_.empty()) {
    return std::nullopt;
  }
  Frame frame = std::move(frames_.front());
  frames_.pop_front();
  not_full_.notify_one();
  return frame;
}

void BoundedFrameQueue::close() {
  {
    std::scoped_lock lock(mutex_);
    closed_ = true;
  }
  not_empty_.notify_all();
  not_full_.notify_all();
}

std::size_t BoundedFrameQueue::size() const {
  std::scoped_lock lock(mutex_);
  return frames_.size();
}

std::size_t BoundedFrameQueue::capacity() const noexcept {
  return capacity_;
}

bool BoundedFrameQueue::closed() const {
  std::scoped_lock lock(mutex_);
  return closed_;
}

std::string to_string(const OverflowPolicy policy) {
  switch (policy) {
    case OverflowPolicy::drop_oldest:
      return "drop_oldest";
    case OverflowPolicy::block_producer:
      return "block_producer";
  }
  return "unknown";
}

}  // namespace realsight::runtime
