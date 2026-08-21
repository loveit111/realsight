#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件把逐帧质量结果收敛成一个最终关键帧。选择器保留“最佳合格帧”和“最佳总体帧”：
 * 有合格帧时返回前者；全部不合格时仍返回后者，让上层能报告真实失败原因和质量分数。
 *
 * 使用的技术栈
 * -------------
 * C++20 optional/chrono/移动语义；OpenCV cv::Mat::clone；第 9 章 Frame 与本章质量评估器。
 *
 * 调用流程
 * --------
 * PerceptionRuntime consumer(Frame&&)
 * -> KeyframeSelector::consider()
 * -> FrameQualityEvaluator::evaluate()
 * -> 更新统计和候选帧
 * -> selected() 返回合格最佳帧，或失败诊断用的最佳总体帧。
 *
 * 重要边界
 * --------
 * 这里按 overall_score 选择一张静态关键帧，不做目标跟踪、镜头语义多样性或 OCR。
 * 只对候选帧 clone 像素，避免把每帧都复制或跨进程发送。
 */

#include <cstdint>
#include <optional>

#include <opencv2/core/mat.hpp>
#include <opencv2/core/types.hpp>

#include "realsight/perception/quality_evaluator.hpp"
#include "realsight/runtime/frame.hpp"

namespace realsight::perception {

struct SelectedKeyframe {
  std::uint64_t source_frame_sequence{0};
  runtime::WallClock::time_point captured_at_utc;
  std::optional<std::chrono::milliseconds> source_position;
  cv::Mat pixels;
  FrameQualityResult quality;
};

struct KeyframeStatistics {
  std::uint64_t evaluated_frames{0};
  std::uint64_t accepted_frames{0};
  std::uint64_t rejected_frames{0};
  std::uint64_t candidate_updates{0};
};

class KeyframeSelector {
 public:
  explicit KeyframeSelector(FrameQualityEvaluator evaluator);

  [[nodiscard]] const FrameQualityResult& consider(
      runtime::Frame&& frame,
      std::optional<cv::Rect> target_region = std::nullopt);

  [[nodiscard]] const SelectedKeyframe* selected() const noexcept;
  [[nodiscard]] bool has_accepted_frame() const noexcept;
  [[nodiscard]] const KeyframeStatistics& statistics() const noexcept;

 private:
  [[nodiscard]] static SelectedKeyframe make_candidate(
      const runtime::Frame& frame,
      FrameQualityResult quality);

  FrameQualityEvaluator evaluator_;
  KeyframeStatistics statistics_;
  std::optional<SelectedKeyframe> best_accepted_;
  std::optional<SelectedKeyframe> best_overall_;
  FrameQualityResult last_result_;
};

}  // namespace realsight::perception
