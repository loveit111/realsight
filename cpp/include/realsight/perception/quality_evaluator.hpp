#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件定义第 10 章的确定性画面质量模型。评估器读取一帧 BGR/灰度图，分别计算
 * 清晰度、曝光、抗反光质量和可选目标区域占比，再根据显式阈值给出 accepted 与问题列表。
 * 它不识别 USB-C 文本、不判断物体类别，也不把启发式分数包装成模型置信度。
 *
 * 使用的技术栈
 * -------------
 * - C++20：强类型结构、optional、vector。
 * - OpenCV core/imgproc：灰度与 HSV 转换、Laplacian、阈值掩码和像素统计。
 *
 * 调用流程
 * --------
 * Frame.pixels + 可选 target_region
 * -> FrameQualityEvaluator::evaluate()
 * -> RawQualityMeasurements（便于调试）
 * -> QualitySignals（统一到 0～1，1 表示更好）
 * -> issues + accepted
 * -> KeyframeSelector。
 *
 * 重要边界
 * --------
 * target_ratio 只有外部已经提供可信目标框时才计算；没有检测器或跟踪器时保持 nullopt。
 * glare 的值表示“抗反光质量”，1 是几乎没有高亮反光，避免与全局 0 差、1 好的约定冲突。
 */

#include <optional>
#include <string>
#include <vector>

#include <opencv2/core/mat.hpp>
#include <opencv2/core/types.hpp>

namespace realsight::perception {

struct QualityPolicy {
  double sharpness_reference{500.0};
  double minimum_sharpness{0.12};
  double minimum_exposure{0.45};
  double minimum_glare_quality{0.40};
  double minimum_target_ratio{0.15};
  double maximum_glare_pixel_ratio{0.08};
  int dark_pixel_threshold{15};
  int bright_pixel_threshold{240};
  int glare_saturation_maximum{40};
  int glare_value_minimum{245};
};

struct RawQualityMeasurements {
  double laplacian_variance{0.0};
  double mean_luminance{0.0};
  double dark_pixel_ratio{0.0};
  double bright_pixel_ratio{0.0};
  double glare_pixel_ratio{0.0};
};

struct QualitySignals {
  double overall_score{0.0};
  double sharpness{0.0};
  double exposure{0.0};
  double glare{0.0};
  std::optional<double> target_ratio;
};

enum class QualityIssue {
  blurry,
  underexposed,
  overexposed,
  glare,
  target_too_small,
};

struct FrameQualityResult {
  QualitySignals signals;
  RawQualityMeasurements raw;
  std::vector<QualityIssue> issues;
  bool accepted{false};
};

class FrameQualityEvaluator {
 public:
  explicit FrameQualityEvaluator(QualityPolicy policy = {});

  [[nodiscard]] FrameQualityResult evaluate(
      const cv::Mat& pixels,
      std::optional<cv::Rect> target_region = std::nullopt) const;

 private:
  QualityPolicy policy_;
};

[[nodiscard]] std::string to_string(QualityIssue issue);

}  // namespace realsight::perception
