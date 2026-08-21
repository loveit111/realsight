/*
 * 文件整体逻辑
 * -----------
 * 在临时目录写入一个 8 帧 MJPG AVI，再通过 OpenCvFrameSource 完整读回，验证真实
 * videoio 编码/解码路径、帧尺寸、序号、时间戳和 EOF 语义。
 *
 * 使用的技术栈：OpenCV VideoWriter/VideoCapture、C++20 filesystem、CTest。
 * 调用流程：write_fixture -> open_video(no pacing) -> read x 8 -> EOF -> 删除临时文件。
 * 边界：测试不打开物理摄像头；摄像头由用户在 Demo 中按设备条件验证。
 */

#include "realsight/runtime/opencv_frame_source.hpp"
#include "test_support.hpp"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <stdexcept>
#include <string>

#include <opencv2/core.hpp>
#include <opencv2/videoio.hpp>

namespace {

std::string path_to_utf8(const std::filesystem::path& path) {
  const std::u8string value = path.u8string();
  return std::string(value.begin(), value.end());
}

std::filesystem::path write_fixture() {
  const auto unique = std::chrono::steady_clock::now().time_since_epoch().count();
  const std::filesystem::path path =
      std::filesystem::temp_directory_path() /
      ("realsight-ch09-" + std::to_string(unique) + ".avi");
  cv::VideoWriter writer(path_to_utf8(path),
                         cv::VideoWriter::fourcc('M', 'J', 'P', 'G'), 25.0,
                         cv::Size(64, 48), true);
  if (!writer.isOpened()) {
    throw std::runtime_error("test could not create MJPG fixture");
  }
  for (int index = 0; index < 8; ++index) {
    cv::Mat image(48, 64, CV_8UC3,
                  cv::Scalar(index * 10, index * 20, index * 30));
    writer.write(image);
  }
  writer.release();
  return path;
}

}  // namespace

int main() {
  realsight::testing::TestContext test;
  std::filesystem::path fixture_path;
  try {
    fixture_path = write_fixture();
    auto source = realsight::runtime::OpenCvFrameSource::open_video(
        {.path = fixture_path, .pace_as_recorded = false});
    test.check(source->descriptor().kind ==
                   realsight::runtime::SourceKind::video_file,
               "fixture must open as video file");
    test.check(source->descriptor().width == 64 &&
                   source->descriptor().height == 48,
               "source must report decoded dimensions");

    std::optional<realsight::runtime::MonotonicClock::time_point> previous;
    for (std::uint64_t expected = 0; expected < 8; ++expected) {
      auto result = source->read({});
      test.check(result.status == realsight::runtime::SourceReadStatus::frame,
                 "first eight reads must return frames");
      test.check(result.frame.has_value(), "frame status must carry a frame");
      if (!result.frame.has_value()) {
        continue;
      }
      test.check(result.frame->sequence == expected,
                 "video sequence must increase from zero");
      test.check(result.frame->valid(), "decoded frame must be valid");
      test.check(result.frame->pixels.cols == 64 &&
                     result.frame->pixels.rows == 48,
                 "decoded frame dimensions must match fixture");
      if (previous.has_value()) {
        test.check(result.frame->captured_at > *previous,
                   "capture time must be strictly monotonic");
      }
      previous = result.frame->captured_at;
    }
    const auto end = source->read({});
    test.check(end.status == realsight::runtime::SourceReadStatus::end_of_stream,
               "ninth read must report normal EOF");

    bool bad_path_rejected = false;
    try {
      auto missing = realsight::runtime::OpenCvFrameSource::open_video(
          {.path = fixture_path.string() + ".missing",
           .pace_as_recorded = false});
    } catch (const std::runtime_error&) {
      bad_path_rejected = true;
    }
    test.check(bad_path_rejected, "missing video path must be rejected");
  } catch (const std::exception& error) {
    test.check(false, error.what());
  }

  if (!fixture_path.empty()) {
    std::error_code ignored;
    std::filesystem::remove(fixture_path, ignored);
  }
  return test.failures() == 0 ? 0 : 1;
}
