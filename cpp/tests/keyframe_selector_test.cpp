/*
 * 文件整体逻辑
 * -----------
 * 验证关键帧选择器在“坏帧后出现好帧”时选择合格帧，并在全部失败时仍保留最佳诊断
 * 帧。测试同时确认帧计数、候选更新和源序号没有被图像 clone 丢失。
 *
 * 使用的技术栈：C++20 chrono/移动语义、OpenCV RNG、Frame、KeyframeSelector。
 * 调用流程：make_frame -> selector.consider -> selected/statistics -> CTest。
 * 边界：本测试只覆盖单张最佳帧策略，不测试 OCR 语义多样性或摄像头设备行为。
 */

#include "realsight/perception/keyframe_selector.hpp"
#include "test_support.hpp"

#include <chrono>
#include <cstdint>

#include <opencv2/core.hpp>

namespace {

realsight::runtime::Frame make_frame(const std::uint64_t sequence,
                                     cv::Mat pixels) {
  return realsight::runtime::Frame(
      sequence,
      realsight::runtime::MonotonicClock::now() +
          std::chrono::milliseconds(sequence),
      std::chrono::milliseconds(sequence * 40), std::move(pixels));
}

cv::Mat textured_image(const std::uint64_t seed) {
  cv::Mat image(120, 160, CV_8UC3);
  cv::RNG random(seed);
  random.fill(image, cv::RNG::UNIFORM, cv::Scalar(40, 40, 40),
              cv::Scalar(210, 210, 210));
  return image;
}

}  // namespace

int main() {
  realsight::testing::TestContext test;
  realsight::perception::KeyframeSelector selector(
      realsight::perception::FrameQualityEvaluator{});

  cv::Mat dark(120, 160, CV_8UC3, cv::Scalar(0, 0, 0));
  static_cast<void>(selector.consider(make_frame(0, dark)));
  static_cast<void>(selector.consider(make_frame(1, textured_image(11))));

  test.check(selector.has_accepted_frame(),
             "selector should remember an accepted frame");
  const auto* selected = selector.selected();
  test.check(selected != nullptr, "selector should return a candidate");
  if (selected != nullptr) {
    test.check(selected->source_frame_sequence == 1,
               "good second frame should replace rejected first frame");
    test.check(selected->quality.accepted,
               "selected candidate should be marked accepted");
    test.check(selected->source_position == std::chrono::milliseconds(40),
               "source position should survive selection");
  }
  test.check(selector.statistics().evaluated_frames == 2,
             "selector should count every evaluated frame");
  test.check(selector.statistics().accepted_frames == 1 &&
                 selector.statistics().rejected_frames == 1,
             "accepted and rejected counters should be separate");

  realsight::perception::KeyframeSelector rejected_only(
      realsight::perception::FrameQualityEvaluator{});
  static_cast<void>(rejected_only.consider(make_frame(7, dark.clone())));
  test.check(!rejected_only.has_accepted_frame(),
             "all-bad stream must not claim an accepted frame");
  test.check(rejected_only.selected() != nullptr,
             "all-bad stream should retain a diagnostic candidate");
  return test.failures() == 0 ? 0 : 1;
}
