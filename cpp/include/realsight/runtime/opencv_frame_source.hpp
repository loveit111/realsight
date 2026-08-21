#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件声明 OpenCV 对 FrameSource 的适配器。它把“视频文件路径”和“摄像头索引”
 * 转换成完全相同的 read() 接口，使上层运行时不需要知道设备驱动和解码器差异。
 *
 * 使用的技术栈
 * -------------
 * - OpenCV videoio：cv::VideoCapture 打开文件或摄像头并解码帧。
 * - C++20：filesystem、stop_token、chrono、RAII。
 *
 * 调用流程
 * --------
 * open_video()/open_camera()
 * -> cv::VideoCapture::open
 * -> 读取实际宽高/FPS/backend
 * -> read() 得到 cv::Mat
 * -> 构造移动型 Frame
 * -> 交给 PerceptionRuntime。
 *
 * 重要边界
 * --------
 * 文件回放可选择按原 FPS 节奏播放；摄像头由驱动自然定时。设置分辨率只是请求，设备
 * 可能拒绝或调整，因此 descriptor() 返回打开后的实际值，而不是盲信请求值。
 */

#include <chrono>
#include <filesystem>
#include <memory>
#include <optional>

#include <opencv2/videoio.hpp>

#include "realsight/runtime/frame_source.hpp"

namespace realsight::runtime {

struct VideoFileOptions {
  std::filesystem::path path;
  bool pace_as_recorded{true};
};

struct CameraOptions {
  int device_index{0};
  int api_preference{cv::CAP_ANY};
  std::optional<int> requested_width;
  std::optional<int> requested_height;
  std::optional<double> requested_frames_per_second;
};

class OpenCvFrameSource final : public FrameSource {
 public:
  [[nodiscard]] static std::unique_ptr<OpenCvFrameSource> open_video(
      const VideoFileOptions& options);
  [[nodiscard]] static std::unique_ptr<OpenCvFrameSource> open_camera(
      const CameraOptions& options);

  ~OpenCvFrameSource() override;

  [[nodiscard]] const SourceDescriptor& descriptor() const noexcept override;
  [[nodiscard]] SourceReadResult read(std::stop_token stop_token) override;

 private:
  OpenCvFrameSource(cv::VideoCapture capture,
                    SourceDescriptor descriptor,
                    bool pace_as_recorded);

  void wait_for_replay_deadline(std::stop_token stop_token);

  cv::VideoCapture capture_;
  SourceDescriptor descriptor_;
  bool pace_as_recorded_;
  std::uint64_t next_sequence_{0};
  MonotonicClock::time_point replay_started_at_;
  MonotonicClock::time_point last_captured_at_{};
};

}  // namespace realsight::runtime
