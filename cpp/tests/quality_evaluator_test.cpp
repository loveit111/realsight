/*
 * 文件整体逻辑
 * -----------
 * 用程序生成的确定性图像验证质量评分的方向和边界：随机纹理应清晰，纯黑图应曝光
 * 不足，大片白色应产生反光问题，可信 ROI 应得到精确面积比例，越界 ROI 必须拒绝。
 *
 * 使用的技术栈：OpenCV Mat/RNG、C++20、项目 TestContext 与 FrameQualityEvaluator。
 * 调用流程：构造像素 -> evaluate -> 检查分数/issue/accepted -> CTest 读取退出码。
 * 边界：这是算法单元测试，不替代真实充电器、镜头和光照条件下的阈值校准。
 */

#include "realsight/perception/quality_evaluator.hpp"
#include "test_support.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include <opencv2/core.hpp>

namespace {

bool contains(const std::vector<realsight::perception::QualityIssue>& issues,
              const realsight::perception::QualityIssue expected) {
  return std::find(issues.begin(), issues.end(), expected) != issues.end();
}

cv::Mat textured_image() {
  cv::Mat image(120, 160, CV_8UC3);
  cv::RNG random(20260817);
  random.fill(image, cv::RNG::UNIFORM, cv::Scalar(40, 40, 40),
              cv::Scalar(210, 210, 210));
  return image;
}

}  // namespace

int main() {
  realsight::testing::TestContext test;
  realsight::perception::FrameQualityEvaluator evaluator;

  const auto good = evaluator.evaluate(textured_image());
  test.check(good.signals.sharpness > 0.5,
             "textured image should have a strong sharpness score");
  test.check(good.accepted, "balanced textured image should be accepted");
  test.check(!good.signals.target_ratio.has_value(),
             "target ratio must stay unknown without a trusted region");

  const cv::Mat dark(120, 160, CV_8UC3, cv::Scalar(0, 0, 0));
  const auto dark_result = evaluator.evaluate(dark);
  test.check(contains(dark_result.issues,
                      realsight::perception::QualityIssue::underexposed),
             "black image should be underexposed");
  test.check(!dark_result.accepted, "black image must not be accepted");

  const cv::Mat white(120, 160, CV_8UC3, cv::Scalar(255, 255, 255));
  const auto white_result = evaluator.evaluate(white);
  test.check(contains(white_result.issues,
                      realsight::perception::QualityIssue::glare),
             "white low-saturation image should be detected as glare");
  test.check(contains(white_result.issues,
                      realsight::perception::QualityIssue::overexposed),
             "white image should be overexposed");

  const auto with_region =
      evaluator.evaluate(textured_image(), cv::Rect(0, 0, 80, 60));
  test.check(with_region.signals.target_ratio.has_value(),
             "trusted target region should produce target ratio");
  if (with_region.signals.target_ratio.has_value()) {
    test.check(std::abs(*with_region.signals.target_ratio - 0.25) < 1e-9,
               "target ratio should equal ROI area divided by frame area");
  }

  bool bad_region_rejected = false;
  try {
    static_cast<void>(
        evaluator.evaluate(textured_image(), cv::Rect(150, 100, 20, 30)));
  } catch (const std::invalid_argument&) {
    bad_region_rejected = true;
  }
  test.check(bad_region_rejected, "out-of-bounds target region must be rejected");
  return test.failures() == 0 ? 0 : 1;
}
