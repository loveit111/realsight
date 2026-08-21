#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件声明一个线程安全、有容量上限的帧队列。采集线程是生产者，质量分析线程是
 * 消费者。容量上限保证内存不会随着消费者变慢而无限增长。
 *
 * 使用的技术栈
 * -------------
 * - C++20：std::mutex、std::condition_variable_any、std::stop_token、移动语义。
 * - std::deque：保存固定数量的移动型 Frame。
 *
 * 调用流程
 * --------
 * 生产者 push(Frame)
 * -> 队列按 OverflowPolicy 阻塞，或丢弃最旧帧
 * -> 消费者 wait_pop()
 * -> close() 唤醒所有等待者
 * -> 队列析构时 cv::Mat 通过 RAII 自动释放。
 *
 * 重要边界
 * --------
 * drop_oldest 追求“新鲜度”，适合实时摄像头；block_producer 追求“不丢帧”，适合
 * 可控的视频回放和测试。这里不把每帧跨进程发送，也不负责第 10 章的质量评分。
 */

#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <stop_token>
#include <string>

#include "realsight/runtime/frame.hpp"

namespace realsight::runtime {

enum class OverflowPolicy { drop_oldest, block_producer };

struct QueuePushResult {
  bool accepted;
  bool stopped;
  bool closed;
  std::size_t dropped_frames;
};

class BoundedFrameQueue {
 public:
  BoundedFrameQueue(std::size_t capacity, OverflowPolicy overflow_policy);

  BoundedFrameQueue(const BoundedFrameQueue&) = delete;
  BoundedFrameQueue& operator=(const BoundedFrameQueue&) = delete;

  [[nodiscard]] QueuePushResult push(Frame frame, std::stop_token stop_token);
  [[nodiscard]] std::optional<Frame> wait_pop(std::stop_token stop_token);
  [[nodiscard]] std::optional<Frame> try_pop();

  void close();

  [[nodiscard]] std::size_t size() const;
  [[nodiscard]] std::size_t capacity() const noexcept;
  [[nodiscard]] bool closed() const;

 private:
  const std::size_t capacity_;
  const OverflowPolicy overflow_policy_;

  mutable std::mutex mutex_;
  std::condition_variable_any not_empty_;
  std::condition_variable_any not_full_;
  std::deque<Frame> frames_;
  bool closed_{false};
};

[[nodiscard]] std::string to_string(OverflowPolicy policy);

}  // namespace realsight::runtime
