/*
 * 文件整体逻辑
 * -----------
 * 实现“每帧计算、只保留最佳候选”的有界内存算法。最佳总体帧用于所有帧不合格时
 * 解释失败，最佳合格帧用于生成最终 Observation。只有候选分数严格变好时才 clone。
 *
 * 使用的技术栈：C++20 optional/移动语义，OpenCV 引用计数矩阵的显式 clone。
 * 调用流程：consider(Frame) -> evaluate -> 计数 -> 必要时替换候选 -> selected()。
 * 边界：相同分数保留较早帧，保证确定性；不把原始 Frame 放进跨线程长期缓存。
 */

#include "realsight/perception/keyframe_selector.hpp"

#include <stdexcept>
#include <utility>

namespace realsight::perception {

KeyframeSelector::KeyframeSelector(FrameQualityEvaluator evaluator)
    : evaluator_(std::move(evaluator)) {}

const FrameQualityResult& KeyframeSelector::consider(
    runtime::Frame&& frame,
    const std::optional<cv::Rect> target_region) {
  if (!frame.valid()) {
    throw std::invalid_argument("keyframe selector requires a valid frame");
  }

  last_result_ = evaluator_.evaluate(frame.pixels, target_region);
  ++statistics_.evaluated_frames;
  if (last_result_.accepted) {
    ++statistics_.accepted_frames;
  } else {
    ++statistics_.rejected_frames;
  }

  if (!best_overall_.has_value() ||
      last_result_.signals.overall_score >
          best_overall_->quality.signals.overall_score) {
    best_overall_ = make_candidate(frame, last_result_);
    ++statistics_.candidate_updates;
  }
  if (last_result_.accepted &&
      (!best_accepted_.has_value() ||
       last_result_.signals.overall_score >
           best_accepted_->quality.signals.overall_score)) {
    best_accepted_ = make_candidate(frame, last_result_);
  }
  return last_result_;
}

const SelectedKeyframe* KeyframeSelector::selected() const noexcept {
  if (best_accepted_.has_value()) {
    return &*best_accepted_;
  }
  return best_overall_.has_value() ? &*best_overall_ : nullptr;
}

bool KeyframeSelector::has_accepted_frame() const noexcept {
  return best_accepted_.has_value();
}

const KeyframeStatistics& KeyframeSelector::statistics() const noexcept {
  return statistics_;
}

SelectedKeyframe KeyframeSelector::make_candidate(
    const runtime::Frame& frame,
    FrameQualityResult quality) {
  return SelectedKeyframe{
      .source_frame_sequence = frame.sequence,
      .captured_at_utc = frame.captured_at_utc,
      .source_position = frame.source_position,
      .pixels = frame.pixels.clone(),
      .quality = std::move(quality),
  };
}

}  // namespace realsight::perception
