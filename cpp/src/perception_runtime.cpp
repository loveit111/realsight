/*
 * 文件整体逻辑
 * -----------
 * 实现“一个采集生产者 + 一个消费调用线程”的连续帧运行时。生产者只读源和写队列，
 * 消费者只从队列取帧；二者用停止令牌和 close() 协调退出，最后汇总可核验统计。
 *
 * 使用的技术栈
 * -------------
 * C++20 jthread、stop_source/stop_callback、atomic、mutex、chrono 和函数回调。
 *
 * 调用流程
 * --------
 * run() -> 启动 producer -> source.read -> queue.push
 *       -> 当前线程 queue.wait_pop -> consumer
 *       -> 汇合停止原因 -> request_stop -> join -> RuntimeSummary。
 *
 * 重要边界
 * --------
 * 线程间计数使用 atomic；错误文本和生产者终态使用 mutex。consumer 异常会被转成
 * consumer_error，而不会跨线程逃逸导致 std::terminate。运行时不保存图像到磁盘。
 */

#include "realsight/runtime/perception_runtime.hpp"

#include <atomic>
#include <exception>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <utility>

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
      case '\r':
        escaped += "\\r";
        break;
      case '\t':
        escaped += "\\t";
        break;
      default:
        escaped += character;
    }
  }
  return escaped;
}

}  // namespace

PerceptionRuntime::PerceptionRuntime(RuntimeOptions options)
    : options_(std::move(options)) {
  if (options_.queue_capacity == 0) {
    throw std::invalid_argument("runtime queue capacity must be greater than zero");
  }
  if (options_.max_consumed_frames.has_value() &&
      *options_.max_consumed_frames == 0) {
    throw std::invalid_argument("max consumed frames must be greater than zero");
  }
}

RuntimeSummary PerceptionRuntime::run(
    FrameSource& source,
    const FrameConsumer& consumer,
    const std::stop_token external_stop_token) {
  if (!consumer) {
    throw std::invalid_argument("frame consumer must not be empty");
  }

  const auto started_at = MonotonicClock::now();
  BoundedFrameQueue queue(options_.queue_capacity, options_.overflow_policy);
  std::stop_source runtime_stop;

  std::atomic<std::uint64_t> produced_frames{0};
  std::atomic<std::uint64_t> dropped_frames{0};

  std::mutex producer_state_mutex;
  SourceReadStatus producer_terminal_status = SourceReadStatus::stopped;
  std::string producer_error;

  // 外部停止一旦触发，就转发到本次 run 独有的 stop_source。
  std::stop_callback external_callback(external_stop_token, [&runtime_stop] {
    runtime_stop.request_stop();
  });

  std::jthread producer([&](const std::stop_token owner_stop_token) {
    // jthread 析构/显式 request_stop 也必须能终止共享运行时。
    std::stop_callback owner_callback(owner_stop_token, [&runtime_stop] {
      runtime_stop.request_stop();
    });

    while (!runtime_stop.stop_requested()) {
      // FrameSource 的正式约定是“用结果返回错误”，但运行时仍防御第三方适配器抛异常，
      // 防止异常逃出线程函数并触发 std::terminate。
      SourceReadResult result = [&source, &runtime_stop] {
        try {
          return source.read(runtime_stop.get_token());
        } catch (const std::exception& error) {
          return SourceReadResult::error(
              std::string("frame source threw an exception: ") + error.what());
        } catch (...) {
          return SourceReadResult::error(
              "frame source threw a non-standard exception");
        }
      }();
      if (result.status == SourceReadStatus::frame) {
        if (!result.frame.has_value()) {
          std::scoped_lock state_lock(producer_state_mutex);
          producer_terminal_status = SourceReadStatus::error;
          producer_error = "frame source returned frame status without a frame";
          break;
        }

        produced_frames.fetch_add(1, std::memory_order_relaxed);
        QueuePushResult push_result =
            queue.push(std::move(*result.frame), runtime_stop.get_token());
        dropped_frames.fetch_add(
            push_result.dropped_frames, std::memory_order_relaxed);
        if (!push_result.accepted) {
          break;
        }
        continue;
      }

      {
        std::scoped_lock state_lock(producer_state_mutex);
        producer_terminal_status = result.status;
        producer_error = std::move(result.error_message);
      }
      break;
    }

    queue.close();
  });

  std::uint64_t consumed_frames = 0;
  std::optional<std::uint64_t> last_consumed_sequence;
  bool max_frames_reached = false;
  bool consumer_requested = false;
  bool consumer_failed = false;
  std::string consumer_error;

  while (std::optional<Frame> frame =
             queue.wait_pop(runtime_stop.get_token())) {
    last_consumed_sequence = frame->sequence;
    ++consumed_frames;

    try {
      if (consumer(std::move(*frame)) == ConsumerDecision::stop) {
        consumer_requested = true;
      }
    } catch (const std::exception& error) {
      consumer_failed = true;
      consumer_error = error.what();
    } catch (...) {
      consumer_failed = true;
      consumer_error = "frame consumer threw a non-standard exception";
    }

    if (consumer_failed || consumer_requested) {
      runtime_stop.request_stop();
      queue.close();
      break;
    }
    if (options_.max_consumed_frames.has_value() &&
        consumed_frames >= *options_.max_consumed_frames) {
      max_frames_reached = true;
      runtime_stop.request_stop();
      queue.close();
      break;
    }
  }

  // join 前先发出停止，保证生产者若正等待“队列未满”能够退出。
  runtime_stop.request_stop();
  producer.request_stop();
  producer.join();

  SourceReadStatus terminal_status;
  std::string source_error;
  {
    std::scoped_lock state_lock(producer_state_mutex);
    terminal_status = producer_terminal_status;
    source_error = producer_error;
  }

  RuntimeStopReason stop_reason = RuntimeStopReason::external_stop;
  std::string error_message;
  if (terminal_status == SourceReadStatus::error) {
    stop_reason = RuntimeStopReason::source_error;
    error_message = std::move(source_error);
  } else if (consumer_failed) {
    stop_reason = RuntimeStopReason::consumer_error;
    error_message = std::move(consumer_error);
  } else if (consumer_requested) {
    stop_reason = RuntimeStopReason::consumer_requested;
  } else if (max_frames_reached) {
    stop_reason = RuntimeStopReason::max_frames_reached;
  } else if (external_stop_token.stop_requested()) {
    stop_reason = RuntimeStopReason::external_stop;
  } else if (terminal_status == SourceReadStatus::end_of_stream) {
    stop_reason = RuntimeStopReason::source_exhausted;
  }

  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      MonotonicClock::now() - started_at);
  return RuntimeSummary{
      .stop_reason = stop_reason,
      .produced_frames = produced_frames.load(std::memory_order_relaxed),
      .consumed_frames = consumed_frames,
      .dropped_frames = dropped_frames.load(std::memory_order_relaxed),
      .last_consumed_sequence = last_consumed_sequence,
      .elapsed = elapsed,
      .error_message = std::move(error_message),
  };
}

std::string to_string(const RuntimeStopReason reason) {
  switch (reason) {
    case RuntimeStopReason::source_exhausted:
      return "source_exhausted";
    case RuntimeStopReason::max_frames_reached:
      return "max_frames_reached";
    case RuntimeStopReason::consumer_requested:
      return "consumer_requested";
    case RuntimeStopReason::external_stop:
      return "external_stop";
    case RuntimeStopReason::source_error:
      return "source_error";
    case RuntimeStopReason::consumer_error:
      return "consumer_error";
  }
  return "unknown";
}

std::string to_json(const RuntimeSummary& summary) {
  std::ostringstream output;
  output << "{\"stop_reason\":\"" << to_string(summary.stop_reason)
         << "\",\"produced_frames\":" << summary.produced_frames
         << ",\"consumed_frames\":" << summary.consumed_frames
         << ",\"dropped_frames\":" << summary.dropped_frames
         << ",\"last_consumed_sequence\":";
  if (summary.last_consumed_sequence.has_value()) {
    output << *summary.last_consumed_sequence;
  } else {
    output << "null";
  }
  output << ",\"elapsed_ms\":" << summary.elapsed.count()
         << ",\"error_message\":\"" << escape_json(summary.error_message)
         << "\"}";
  return output.str();
}

}  // namespace realsight::runtime
