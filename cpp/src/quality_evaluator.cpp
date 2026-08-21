/*
 * 文件整体逻辑
 * -----------
 * 实现画面质量的可解释启发式：Laplacian 方差衡量边缘清晰度；平均亮度与暗/亮
 * 截断比例共同衡量曝光；HSV 中低饱和、高亮像素比例近似反光；可信 ROI 面积衡量
 * 目标占比。各原始量经过 clamp 归一化后再按固定权重汇总。
 *
 * 使用的技术栈
 * -------------
 * OpenCV cvtColor/Laplacian/meanStdDev/inRange/countNonZero；C++20 数值与异常处理。
 *
 * 调用流程
 * evaluate -> 校验图像/ROI -> 颜色空间转换 -> 原始统计 -> 0～1 分数
 *          -> 逐项阈值问题 -> accepted -> 返回 FrameQualityResult。
 *
 * 重要边界
 * --------
 * 这些分数适合筛除明显模糊、过暗、过亮和强反光帧，不等于 OCR 成功率。阈值需要用
 * 真实摄像头数据校准；第 11 章仍必须根据 OCR/多模态结果判断证据是否可提取。
 */

#include "realsight/perception/quality_evaluator.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <utility>

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>

namespace realsight::perception {
namespace {

double clamp_unit(const double value) {
  return std::clamp(value, 0.0, 1.0);
}

cv::Mat to_gray(const cv::Mat& pixels) {
  cv::Mat gray;
  if (pixels.channels() == 1) {
    pixels.copyTo(gray);
  } else if (pixels.channels() == 3) {
    cv::cvtColor(pixels, gray, cv::COLOR_BGR2GRAY);
  } else if (pixels.channels() == 4) {
    cv::cvtColor(pixels, gray, cv::COLOR_BGRA2GRAY);
  } else {
    throw std::invalid_argument("quality evaluator supports 1, 3 or 4 channels");
  }
  return gray;
}

cv::Mat to_bgr(const cv::Mat& pixels) {
  cv::Mat bgr;
  if (pixels.channels() == 3) {
    pixels.copyTo(bgr);
  } else if (pixels.channels() == 1) {
    cv::cvtColor(pixels, bgr, cv::COLOR_GRAY2BGR);
  } else if (pixels.channels() == 4) {
    cv::cvtColor(pixels, bgr, cv::COLOR_BGRA2BGR);
  } else {
    throw std::invalid_argument("quality evaluator supports 1, 3 or 4 channels");
  }
  return bgr;
}

double mask_ratio(const cv::Mat& mask) {
  return static_cast<double>(cv::countNonZero(mask)) /
         static_cast<double>(mask.total());
}

void validate_policy(const QualityPolicy& policy) {
  if (policy.sharpness_reference <= 0.0 ||
      policy.maximum_glare_pixel_ratio <= 0.0) {
    throw std::invalid_argument("quality normalization references must be positive");
  }
  for (const double threshold : {policy.minimum_sharpness,
                                 policy.minimum_exposure,
                                 policy.minimum_glare_quality,
                                 policy.minimum_target_ratio}) {
    if (threshold < 0.0 || threshold > 1.0) {
      throw std::invalid_argument("quality thresholds must be between zero and one");
    }
  }
}

}  // namespace

FrameQualityEvaluator::FrameQualityEvaluator(QualityPolicy policy)
    : policy_(std::move(policy)) {
  validate_policy(policy_);
}

FrameQualityResult FrameQualityEvaluator::evaluate(
    const cv::Mat& pixels,
    const std::optional<cv::Rect> target_region) const {
  if (pixels.empty() || pixels.rows <= 0 || pixels.cols <= 0) {
    throw std::invalid_argument("quality evaluator requires a non-empty image");
  }
  if (target_region.has_value()) {
    const cv::Rect frame_bounds(0, 0, pixels.cols, pixels.rows);
    if (target_region->width <= 0 || target_region->height <= 0 ||
        ((*target_region) & frame_bounds) != *target_region) {
      throw std::invalid_argument("target region must be fully inside the frame");
    }
  }

  const cv::Mat gray = to_gray(pixels);
  cv::Mat laplacian;
  cv::Laplacian(gray, laplacian, CV_64F);
  cv::Scalar laplacian_mean;
  cv::Scalar laplacian_stddev;
  cv::meanStdDev(laplacian, laplacian_mean, laplacian_stddev);

  cv::Mat dark_mask;
  cv::Mat bright_mask;
  cv::compare(gray, policy_.dark_pixel_threshold, dark_mask, cv::CMP_LE);
  cv::compare(gray, policy_.bright_pixel_threshold, bright_mask, cv::CMP_GE);

  const cv::Mat bgr = to_bgr(pixels);
  cv::Mat hsv;
  cv::cvtColor(bgr, hsv, cv::COLOR_BGR2HSV);
  cv::Mat glare_mask;
  cv::inRange(hsv,
              cv::Scalar(0, 0, policy_.glare_value_minimum),
              cv::Scalar(179, policy_.glare_saturation_maximum, 255),
              glare_mask);

  FrameQualityResult result;
  result.raw.laplacian_variance = laplacian_stddev[0] * laplacian_stddev[0];
  result.raw.mean_luminance = cv::mean(gray)[0];
  result.raw.dark_pixel_ratio = mask_ratio(dark_mask);
  result.raw.bright_pixel_ratio = mask_ratio(bright_mask);
  result.raw.glare_pixel_ratio = mask_ratio(glare_mask);

  result.signals.sharpness =
      clamp_unit(result.raw.laplacian_variance / policy_.sharpness_reference);
  const double centered_brightness =
      1.0 - std::abs(result.raw.mean_luminance - 127.5) / 127.5;
  const double clipping_quality =
      1.0 - std::max(result.raw.dark_pixel_ratio,
                     result.raw.bright_pixel_ratio);
  result.signals.exposure =
      clamp_unit(0.6 * centered_brightness + 0.4 * clipping_quality);
  result.signals.glare = clamp_unit(
      1.0 - result.raw.glare_pixel_ratio / policy_.maximum_glare_pixel_ratio);

  if (target_region.has_value()) {
    const double target_area =
        static_cast<double>(target_region->area()) /
        static_cast<double>(pixels.rows * pixels.cols);
    result.signals.target_ratio = clamp_unit(target_area);
  }

  double weighted_total = 0.45 * result.signals.sharpness +
                          0.35 * result.signals.exposure +
                          0.20 * result.signals.glare;
  double total_weight = 1.0;
  if (result.signals.target_ratio.has_value()) {
    weighted_total += 0.15 * *result.signals.target_ratio;
    total_weight += 0.15;
  }
  result.signals.overall_score = clamp_unit(weighted_total / total_weight);

  if (result.signals.sharpness < policy_.minimum_sharpness) {
    result.issues.push_back(QualityIssue::blurry);
  }
  if (result.signals.exposure < policy_.minimum_exposure) {
    result.issues.push_back(result.raw.mean_luminance < 127.5
                                ? QualityIssue::underexposed
                                : QualityIssue::overexposed);
  }
  if (result.signals.glare < policy_.minimum_glare_quality) {
    result.issues.push_back(QualityIssue::glare);
  }
  if (result.signals.target_ratio.has_value() &&
      *result.signals.target_ratio < policy_.minimum_target_ratio) {
    result.issues.push_back(QualityIssue::target_too_small);
  }
  result.accepted = result.issues.empty();
  return result;
}

std::string to_string(const QualityIssue issue) {
  switch (issue) {
    case QualityIssue::blurry:
      return "blurry";
    case QualityIssue::underexposed:
      return "underexposed";
    case QualityIssue::overexposed:
      return "overexposed";
    case QualityIssue::glare:
      return "glare";
    case QualityIssue::target_too_small:
      return "target_too_small";
  }
  return "unknown";
}

}  // namespace realsight::perception
