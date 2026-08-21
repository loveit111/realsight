/*
 * 文件整体逻辑
 * -----------
 * 实现 OpenCV 视频文件/摄像头适配器。打开阶段读取真实源信息；读取阶段按需等待视频
 * 的播放时刻，再把解码得到的 cv::Mat 移动进 Frame。
 *
 * 使用的技术栈：OpenCV 5/4 兼容的 videoio API、C++20 chrono/filesystem/RAII。
 * 调用流程：open_* -> VideoCapture -> read -> cv::Mat -> Frame -> SourceReadResult。
 * 边界：不弹出 GUI 窗口；不做 OCR/质量评分；摄像头驱动中的阻塞 read 无法硬取消。
 */

#include "realsight/runtime/opencv_frame_source.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace realsight::runtime {
namespace {

std::string path_to_utf8(const std::filesystem::path& path) {
  const std::u8string value = path.u8string();
  return std::string(value.begin(), value.end());
}

int capture_dimension(const cv::VideoCapture& capture, const int property) {
  return std::max(0, static_cast<int>(std::lround(capture.get(property))));
}

double capture_fps(const cv::VideoCapture& capture) {
  const double fps = capture.get(cv::CAP_PROP_FPS);
  return std::isfinite(fps) && fps > 0.0 ? fps : 0.0;
}

std::string capture_backend(const cv::VideoCapture& capture) {
  try {
    return capture.getBackendName();
  } catch (const cv::Exception&) {
    return "unknown";
  }
}

}  // namespace

std::unique_ptr<OpenCvFrameSource> OpenCvFrameSource::open_video(
    const VideoFileOptions& options) {
  if (options.path.empty()) {
    throw std::invalid_argument("video path must not be empty");
  }

  cv::VideoCapture capture;
  const std::string path_text = path_to_utf8(options.path);
  // 常见桌面构建优先使用 FFmpeg，避免 CAP_ANY 为普通文件尝试摄像头/管线型 backend
  // 并输出无关警告；若当前 OpenCV 没有 FFmpeg，再回退到自动选择。
  bool opened = capture.open(path_text, cv::CAP_FFMPEG);
  if (!opened) {
    capture.release();
    opened = capture.open(path_text, cv::CAP_ANY);
  }
  if (!opened || !capture.isOpened()) {
    throw std::runtime_error("OpenCV could not open video file: " + path_text);
  }

  SourceDescriptor descriptor{
      .kind = SourceKind::video_file,
      .label = path_text,
      .backend = capture_backend(capture),
      .width = capture_dimension(capture, cv::CAP_PROP_FRAME_WIDTH),
      .height = capture_dimension(capture, cv::CAP_PROP_FRAME_HEIGHT),
      .frames_per_second = capture_fps(capture),
  };
  return std::unique_ptr<OpenCvFrameSource>(new OpenCvFrameSource(
      std::move(capture), std::move(descriptor), options.pace_as_recorded));
}

std::unique_ptr<OpenCvFrameSource> OpenCvFrameSource::open_camera(
    const CameraOptions& options) {
  if (options.device_index < 0) {
    throw std::invalid_argument("camera index must be zero or greater");
  }

  cv::VideoCapture capture;
  if (!capture.open(options.device_index, options.api_preference) ||
      !capture.isOpened()) {
    throw std::runtime_error(
        "OpenCV could not open camera index " +
        std::to_string(options.device_index));
  }

  // set() 的返回值和最终值都可能受驱动影响，因此下面仍读取实际参数。
  if (options.requested_width.has_value()) {
    capture.set(cv::CAP_PROP_FRAME_WIDTH, *options.requested_width);
  }
  if (options.requested_height.has_value()) {
    capture.set(cv::CAP_PROP_FRAME_HEIGHT, *options.requested_height);
  }
  if (options.requested_frames_per_second.has_value()) {
    capture.set(cv::CAP_PROP_FPS, *options.requested_frames_per_second);
  }

  SourceDescriptor descriptor{
      .kind = SourceKind::camera,
      .label = "camera:" + std::to_string(options.device_index),
      .backend = capture_backend(capture),
      .width = capture_dimension(capture, cv::CAP_PROP_FRAME_WIDTH),
      .height = capture_dimension(capture, cv::CAP_PROP_FRAME_HEIGHT),
      .frames_per_second = capture_fps(capture),
  };
  return std::unique_ptr<OpenCvFrameSource>(new OpenCvFrameSource(
      std::move(capture), std::move(descriptor), false));
}

OpenCvFrameSource::OpenCvFrameSource(
    cv::VideoCapture capture,
    SourceDescriptor descriptor,
    const bool pace_as_recorded)
    : capture_(std::move(capture)),
      descriptor_(std::move(descriptor)),
      pace_as_recorded_(pace_as_recorded),
      replay_started_at_(MonotonicClock::now()) {}

OpenCvFrameSource::~OpenCvFrameSource() {
  capture_.release();
}

const SourceDescriptor& OpenCvFrameSource::descriptor() const noexcept {
  return descriptor_;
}

SourceReadResult OpenCvFrameSource::read(const std::stop_token stop_token) {
  if (stop_token.stop_requested()) {
    return SourceReadResult::stopped();
  }

  if (descriptor_.kind == SourceKind::video_file && pace_as_recorded_) {
    wait_for_replay_deadline(stop_token);
    if (stop_token.stop_requested()) {
      return SourceReadResult::stopped();
    }
  }

  cv::Mat pixels;
  try {
    if (!capture_.read(pixels) || pixels.empty()) {
      if (descriptor_.kind == SourceKind::video_file) {
        return SourceReadResult::end();
      }
      return SourceReadResult::error(
          "camera read returned no frame; the device may be disconnected");
    }
  } catch (const cv::Exception& error) {
    return SourceReadResult::error(
        std::string("OpenCV frame read failed: ") + error.what());
  }

  std::optional<std::chrono::milliseconds> source_position;
  if (descriptor_.kind == SourceKind::video_file) {
    const double position_ms = capture_.get(cv::CAP_PROP_POS_MSEC);
    if (std::isfinite(position_ms) && position_ms >= 0.0) {
      source_position = std::chrono::milliseconds(
          static_cast<std::int64_t>(std::llround(position_ms)));
    }
  }

  auto captured_at = MonotonicClock::now();
  if (next_sequence_ > 0 && captured_at <= last_captured_at_) {
    captured_at = last_captured_at_ + MonotonicClock::duration(1);
  }
  last_captured_at_ = captured_at;

  Frame frame(next_sequence_, captured_at, source_position, std::move(pixels));
  ++next_sequence_;
  return SourceReadResult::with_frame(std::move(frame));
}

void OpenCvFrameSource::wait_for_replay_deadline(
    const std::stop_token stop_token) {
  if (descriptor_.frames_per_second <= 0.0 || next_sequence_ == 0) {
    return;
  }

  const auto target_offset = std::chrono::duration<double>(
      static_cast<double>(next_sequence_) / descriptor_.frames_per_second);
  const auto target_time = replay_started_at_ +
                           std::chrono::duration_cast<MonotonicClock::duration>(
                               target_offset);

  // 小段睡眠让停止请求最多等待约 5ms，而不是一次 sleep 到完整帧期限。
  while (!stop_token.stop_requested()) {
    const auto remaining = target_time - MonotonicClock::now();
    if (remaining <= MonotonicClock::duration::zero()) {
      return;
    }
    std::this_thread::sleep_for(
        std::min(remaining,
                 std::chrono::duration_cast<MonotonicClock::duration>(
                     std::chrono::milliseconds(5))));
  }
}

}  // namespace realsight::runtime
