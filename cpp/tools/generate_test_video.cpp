/*
 * 文件整体逻辑
 * -----------
 * 生成一个很小、内容确定的 AVI 测试视频，供第 9 章 Demo 和 OpenCV 回放测试使用。
 * 每帧有变化的背景、移动矩形和序号，因此可确认解码到的不是空白或重复假数据。
 *
 * 使用的技术栈
 * -------------
 * OpenCV core/imgproc/videoio；C++20 filesystem、from_chars。
 *
 * 调用流程
 * --------
 * 命令行读取输出路径和可选帧数
 * -> VideoWriter(MJPG, 20 FPS, 160x90)
 * -> 循环生成 cv::Mat
 * -> writer.write
 * -> 关闭文件并输出 JSON。
 *
 * 重要边界
 * --------
 * 这是测试数据生成器，不属于生产采集服务。MJPG 是为了在 Windows/Linux 教学环境中
 * 获得较高可用性；编码器仍由当前 OpenCV videoio backend 决定。
 */

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/videoio.hpp>

#include <charconv>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {

int parse_frame_count(const std::string_view text) {
  int value = 0;
  const auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size() || value < 1) {
    throw std::invalid_argument("frame count must be a positive integer");
  }
  return value;
}

std::string path_to_utf8(const std::filesystem::path& path) {
  const std::u8string value = path.u8string();
  return std::string(value.begin(), value.end());
}

std::string escape_json(const std::string_view value) {
  std::string escaped;
  escaped.reserve(value.size());
  for (const char character : value) {
    if (character == '\\' || character == '"') {
      escaped.push_back('\\');
    }
    escaped.push_back(character);
  }
  return escaped;
}

}  // namespace

int main(const int argc, char* argv[]) {
  try {
    if (argc < 2 || argc > 3) {
      std::cerr << "usage: realsight-generate-video OUTPUT.avi [FRAME_COUNT]\n";
      return 2;
    }

    const std::filesystem::path output_path = argv[1];
    const int frame_count = argc == 3 ? parse_frame_count(argv[2]) : 24;
    constexpr double frames_per_second = 20.0;
    const cv::Size frame_size(160, 90);
    const int codec = cv::VideoWriter::fourcc('M', 'J', 'P', 'G');

    cv::VideoWriter writer(path_to_utf8(output_path), codec, frames_per_second,
                           frame_size, true);
    if (!writer.isOpened()) {
      throw std::runtime_error("OpenCV could not create MJPG AVI test video");
    }

    for (int index = 0; index < frame_count; ++index) {
      cv::Mat image(frame_size, CV_8UC3,
                    cv::Scalar((index * 17) % 255, (index * 31) % 255,
                               (index * 47) % 255));
      const int left = (index * 7) % (frame_size.width - 30);
      cv::rectangle(image, cv::Rect(left, 24, 30, 24),
                    cv::Scalar(20, 230, 80), cv::FILLED);
      cv::putText(image, std::to_string(index), cv::Point(6, 18),
                  cv::FONT_HERSHEY_SIMPLEX, 0.5, cv::Scalar(255, 255, 255), 1,
                  cv::LINE_AA);
      writer.write(image);
    }
    writer.release();

    const auto bytes = std::filesystem::file_size(output_path);
    std::cout << "{\"event\":\"test_video_created\",\"path\":\""
              << escape_json(path_to_utf8(output_path)) << "\",\"frames\":"
              << frame_count
              << ",\"fps\":" << frames_per_second << ",\"width\":"
              << frame_size.width << ",\"height\":" << frame_size.height
              << ",\"bytes\":" << bytes << "}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "test video generation failed: " << error.what() << '\n';
    return 1;
  }
}
