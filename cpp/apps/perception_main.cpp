/*
 * 文件整体逻辑
 * -----------
 * 这是第 9 章 C++ 感知运行时的命令行入口。用户选择预录视频或摄像头后，本程序创建
 * OpenCvFrameSource，配置有界队列并运行 PerceptionRuntime，最后输出结构化 JSON。
 *
 * 使用的技术栈
 * -------------
 * - C++20：from_chars 参数解析、filesystem、jthread、stop_source、signal。
 * - OpenCV：由 OpenCvFrameSource 间接完成视频解码或摄像头采集。
 * - RealSight runtime：Frame、FrameSource、有界队列、运行统计。
 *
 * 调用流程
 * --------
 * main -> parse_arguments
 * -> OpenCvFrameSource::open_video/open_camera
 * -> PerceptionRuntime::run
 * -> consumer 检查时间戳并模拟处理延迟
 * -> 输出 source JSON 与 summary JSON
 * -> 根据运行结果返回退出码。
 *
 * 重要边界
 * --------
 * 程序不显示 GUI、不逐帧打印日志、不调用 Python/LLM。Ctrl+C 通过轻量轮询线程转换为
 * stop_token；真正的驱动 read 若阻塞，仍需等该次读取返回后才能完成退出。
 */

#include "realsight/runtime/opencv_frame_source.hpp"
#include "realsight/runtime/perception_runtime.hpp"
#include "realsight/runtime/runtime_info.hpp"

#include <atomic>
#include <charconv>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <stop_token>
#include <string>
#include <string_view>
#include <thread>

namespace {

volatile std::sig_atomic_t signal_requested = 0;

void handle_signal(int) {
  signal_requested = 1;
}

struct CliOptions {
  bool describe_only{false};
  std::optional<std::filesystem::path> video_path;
  std::optional<int> camera_index;
  bool pace_video{true};
  std::size_t queue_capacity{4};
  realsight::runtime::OverflowPolicy overflow_policy{
      realsight::runtime::OverflowPolicy::drop_oldest};
  std::optional<std::uint64_t> max_frames;
  std::chrono::milliseconds consumer_delay{0};
};

[[noreturn]] void fail_argument(const std::string& message) {
  throw std::invalid_argument(message);
}

template <typename Integer>
Integer parse_integer(const std::string_view text, const std::string& option) {
  Integer value{};
  const auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size()) {
    fail_argument(option + " requires an integer, received: " +
                  std::string(text));
  }
  return value;
}

std::string_view require_value(
    const int argc,
    char* argv[],
    int& index,
    const std::string& option) {
  ++index;
  if (index >= argc) {
    fail_argument(option + " requires a value");
  }
  return argv[index];
}

CliOptions parse_arguments(const int argc, char* argv[]) {
  CliOptions options;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    if (argument == "--describe") {
      options.describe_only = true;
    } else if (argument == "--video") {
      options.video_path = std::filesystem::path(
          require_value(argc, argv, index, argument));
    } else if (argument == "--camera") {
      options.camera_index = parse_integer<int>(
          require_value(argc, argv, index, argument), argument);
    } else if (argument == "--no-realtime") {
      options.pace_video = false;
    } else if (argument == "--queue-capacity") {
      options.queue_capacity = parse_integer<std::size_t>(
          require_value(argc, argv, index, argument), argument);
    } else if (argument == "--max-frames") {
      options.max_frames = parse_integer<std::uint64_t>(
          require_value(argc, argv, index, argument), argument);
    } else if (argument == "--consumer-delay-ms") {
      options.consumer_delay = std::chrono::milliseconds(parse_integer<int>(
          require_value(argc, argv, index, argument), argument));
    } else if (argument == "--overflow") {
      const std::string_view value = require_value(argc, argv, index, argument);
      if (value == "drop-oldest") {
        options.overflow_policy =
            realsight::runtime::OverflowPolicy::drop_oldest;
      } else if (value == "block") {
        options.overflow_policy =
            realsight::runtime::OverflowPolicy::block_producer;
      } else {
        fail_argument("--overflow must be drop-oldest or block");
      }
    } else if (argument == "--help" || argument == "-h") {
      options.describe_only = true;
    } else {
      fail_argument("unknown argument: " + argument);
    }
  }

  if (options.describe_only) {
    return options;
  }
  if (options.video_path.has_value() == options.camera_index.has_value()) {
    fail_argument("choose exactly one source: --video PATH or --camera INDEX");
  }
  if (options.queue_capacity == 0) {
    fail_argument("--queue-capacity must be greater than zero");
  }
  if (options.max_frames.has_value() && *options.max_frames == 0) {
    fail_argument("--max-frames must be greater than zero");
  }
  if (options.consumer_delay.count() < 0) {
    fail_argument("--consumer-delay-ms must not be negative");
  }
  return options;
}

void print_usage() {
  std::cout
      << "RealSight perception runtime\n"
      << "  --video PATH | --camera INDEX\n"
      << "  [--queue-capacity N] [--overflow drop-oldest|block]\n"
      << "  [--max-frames N] [--consumer-delay-ms N] [--no-realtime]\n"
      << "  --describe  print runtime capabilities\n";
}

std::string escape_json(const std::string& value) {
  std::string escaped;
  escaped.reserve(value.size());
  for (const char character : value) {
    if (character == '\\') {
      escaped += "\\\\";
    } else if (character == '"') {
      escaped += "\\\"";
    } else {
      escaped += character;
    }
  }
  return escaped;
}

void print_source(const realsight::runtime::SourceDescriptor& source) {
  std::cout << "{\"event\":\"source_opened\",\"kind\":\""
            << realsight::runtime::to_string(source.kind) << "\",\"label\":\""
            << escape_json(source.label) << "\",\"backend\":\""
            << escape_json(source.backend) << "\",\"width\":" << source.width
            << ",\"height\":" << source.height << ",\"fps\":"
            << source.frames_per_second << "}\n";
}

}  // namespace

int main(const int argc, char* argv[]) {
  try {
    const CliOptions cli = parse_arguments(argc, argv);
    if (cli.describe_only || argc == 1) {
      print_usage();
      std::cout << realsight::runtime::to_json(
                       realsight::runtime::describe_runtime())
                << '\n';
      return 0;
    }

    std::unique_ptr<realsight::runtime::OpenCvFrameSource> source;
    if (cli.video_path.has_value()) {
      source = realsight::runtime::OpenCvFrameSource::open_video(
          {.path = *cli.video_path, .pace_as_recorded = cli.pace_video});
    } else {
      source = realsight::runtime::OpenCvFrameSource::open_camera(
          {.device_index = *cli.camera_index,
           .api_preference = cv::CAP_ANY,
           .requested_width = std::nullopt,
           .requested_height = std::nullopt,
           .requested_frames_per_second = std::nullopt});
    }
    print_source(source->descriptor());

    realsight::runtime::PerceptionRuntime runtime({
        .queue_capacity = cli.queue_capacity,
        .overflow_policy = cli.overflow_policy,
        .max_consumed_frames = cli.max_frames,
    });

    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    std::stop_source external_stop;
    std::jthread signal_watcher(
        [&external_stop](const std::stop_token watcher_stop) {
          while (!watcher_stop.stop_requested() && signal_requested == 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
          }
          if (signal_requested != 0) {
            external_stop.request_stop();
          }
        });

    bool timestamps_monotonic = true;
    std::optional<realsight::runtime::MonotonicClock::time_point>
        previous_timestamp;
    std::uint64_t sampled_byte_sum = 0;
    const auto consumer = [&](realsight::runtime::Frame&& frame) {
      if (previous_timestamp.has_value() &&
          frame.captured_at <= *previous_timestamp) {
        timestamps_monotonic = false;
      }
      previous_timestamp = frame.captured_at;
      if (frame.valid() && frame.byte_size() > 0) {
        sampled_byte_sum += frame.pixels.data[0];
      }
      if (cli.consumer_delay.count() > 0) {
        std::this_thread::sleep_for(cli.consumer_delay);
      }
      return realsight::runtime::ConsumerDecision::continue_running;
    };

    const realsight::runtime::RuntimeSummary summary =
        runtime.run(*source, consumer, external_stop.get_token());
    signal_watcher.request_stop();
    signal_watcher.join();

    std::cout << "{\"event\":\"runtime_summary\",\"runtime\":"
              << realsight::runtime::to_json(summary)
              << ",\"timestamps_monotonic\":"
              << (timestamps_monotonic ? "true" : "false")
              << ",\"sampled_byte_sum\":" << sampled_byte_sum << "}\n";

    if (summary.stop_reason == realsight::runtime::RuntimeStopReason::source_error ||
        summary.stop_reason ==
            realsight::runtime::RuntimeStopReason::consumer_error) {
      return 3;
    }
    return timestamps_monotonic ? 0 : 4;
  } catch (const std::invalid_argument& error) {
    std::cerr << "argument error: " << error.what() << '\n';
    print_usage();
    return 2;
  } catch (const std::exception& error) {
    std::cerr << "startup error: " << error.what() << '\n';
    return 3;
  }
}
