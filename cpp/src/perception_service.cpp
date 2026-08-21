/*
 * 文件整体逻辑
 * -----------
 * 实现 C++ 感知运行时到 Protobuf 事件流的适配。请求先经过独立校验，再注册取消令牌；
 * 运行时只在 C++ 内部消费连续帧，周期性发送低频进度，最终把一张关键帧写入受控目录，
 * 然后发送一个 Observation。所有帧都不经过 gRPC，网络上传的是小型元数据事件。
 *
 * 使用的技术栈
 * -------------
 * gRPC C++ synchronous server-streaming、Protobuf generated API、OpenCV imgcodecs、
 * C++20 filesystem/chrono/stop_source，以及第 9 章 PerceptionRuntime。
 *
 * 调用流程
 * Observe -> validate_request -> register_request -> progress(request_accepted)
 *         -> open source -> PerceptionRuntime::run -> KeyframeSelector::consider
 *         -> save JPEG -> observation event -> complete_request -> gRPC Status。
 * Cancel  -> active_requests_ 查找 -> request_stop -> Observe 在下一次帧边界退出。
 *
 * 重要边界
 * --------
 * image_path 是同一台机器上的本地工件引用，不是可供远程节点直接访问的 URL。请求 ID
 * 必须通过白名单校验后才能参与路径构造。摄像头驱动若永久阻塞 read()，stop_token 不能
 * 强制打断驱动调用；这是第 9 章已声明的设备后端边界。
 */

#include "realsight/transport/perception_service.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <utility>

#include <opencv2/imgcodecs.hpp>

#include "realsight/perception/keyframe_selector.hpp"
#include "realsight/runtime/opencv_frame_source.hpp"
#include "realsight/runtime/perception_runtime.hpp"

namespace realsight::transport {
namespace {

using SystemClock = std::chrono::system_clock;

std::int64_t unix_milliseconds(const SystemClock::time_point value) {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             value.time_since_epoch())
      .count();
}

bool is_safe_identifier(const std::string& value) {
  if (value.empty() || value.size() > 128 ||
      !std::isalnum(static_cast<unsigned char>(value.front()))) {
    return false;
  }
  return std::all_of(value.begin(), value.end(), [](const char character) {
    const auto byte = static_cast<unsigned char>(character);
    return std::isalnum(byte) || character == '.' || character == '_' ||
           character == '-';
  });
}

std::optional<std::string> validate_request(
    const v1::ObservationRequest& request) {
  if (request.schema_version() != 1) {
    return "schema_version must equal 1";
  }
  for (const auto* value : {&request.request_id(), &request.session_id(),
                            &request.target_id()}) {
    if (!is_safe_identifier(*value)) {
      return "request, session and target IDs must use safe identifier syntax";
    }
  }
  if (request.view_type() == v1::VIEW_TYPE_UNSPECIFIED) {
    return "view_type must be specified";
  }
  if (request.required_features().empty()) {
    return "required_features must not be empty";
  }
  if (request.instruction().empty() || request.reason().empty()) {
    return "instruction and reason must not be empty";
  }
  if (request.timeout_ms() < 100 || request.timeout_ms() > 30'000) {
    return "timeout_ms must be between 100 and 30000";
  }
  return std::nullopt;
}

std::string path_to_utf8(const std::filesystem::path& path) {
  const std::u8string value = path.u8string();
  return std::string(value.begin(), value.end());
}

std::unique_ptr<runtime::OpenCvFrameSource> open_source(
    const ObservationSourceConfig& config) {
  if (config.kind == ObservationSourceKind::video_file) {
    return runtime::OpenCvFrameSource::open_video(
        {.path = config.video_path,
         .pace_as_recorded = config.pace_as_recorded});
  }
  return runtime::OpenCvFrameSource::open_camera(
      {.device_index = config.camera_index,
       .api_preference = cv::CAP_ANY,
       .requested_width = config.requested_width > 0
                              ? std::optional<int>(config.requested_width)
                              : std::nullopt,
       .requested_height = config.requested_height > 0
                               ? std::optional<int>(config.requested_height)
                               : std::nullopt,
       .requested_frames_per_second = std::nullopt});
}

std::string issues_to_text(
    const std::vector<perception::QualityIssue>& issues) {
  std::ostringstream stream;
  for (std::size_t index = 0; index < issues.size(); ++index) {
    if (index > 0) {
      stream << ',';
    }
    stream << perception::to_string(issues[index]);
  }
  return stream.str();
}

class EventWriter {
 public:
  EventWriter(const v1::ObservationRequest& request,
              grpc::ServerWriter<v1::ObservationEvent>& writer)
      : request_(request), writer_(writer) {}

  bool progress(const std::string& stage,
                const std::uint32_t percent,
                const std::string& message) {
    v1::ObservationEvent event = base_event();
    auto* payload = event.mutable_progress();
    payload->set_request_id(request_.request_id());
    payload->set_target_id(request_.target_id());
    payload->set_stage(stage);
    payload->set_progress_percent(std::min(percent, std::uint32_t{100}));
    payload->set_message(message);
    return writer_.Write(event);
  }

  bool failure(const std::string& code,
               const std::string& message,
               const bool retryable) {
    v1::ObservationEvent event = base_event();
    auto* payload = event.mutable_failure();
    payload->set_request_id(request_.request_id());
    payload->set_target_id(request_.target_id());
    payload->set_code(code);
    payload->set_message(message);
    payload->set_retryable(retryable);
    return writer_.Write(event);
  }

  bool observation(const perception::SelectedKeyframe& selected,
                   const bool accepted,
                   const std::filesystem::path& image_path) {
    v1::ObservationEvent event = base_event();
    auto* payload = event.mutable_observation();
    payload->set_schema_version(1);
    payload->set_observation_id(request_.request_id() + "-observation-1");
    payload->set_request_id(request_.request_id());
    payload->set_target_id(request_.target_id());
    payload->set_view_type(request_.view_type());
    payload->set_captured_at_unix_ms(
        unix_milliseconds(selected.captured_at_utc));
    payload->set_image_path(path_to_utf8(image_path));
    payload->set_source_frame_sequence(selected.source_frame_sequence);
    if (selected.source_position.has_value()) {
      payload->set_source_position_ms(selected.source_position->count());
    }

    auto* quality = payload->mutable_quality();
    quality->set_overall_score(selected.quality.signals.overall_score);
    quality->set_sharpness(selected.quality.signals.sharpness);
    quality->set_exposure(selected.quality.signals.exposure);
    quality->set_glare(selected.quality.signals.glare);
    if (selected.quality.signals.target_ratio.has_value()) {
      quality->set_target_ratio(*selected.quality.signals.target_ratio);
    }

    if (selected.quality.signals.sharpness >= 0.5) {
      payload->add_local_features("quality:sharp");
    }
    if (selected.quality.signals.exposure >= 0.6) {
      payload->add_local_features("quality:well_exposed");
    }
    if (selected.quality.signals.glare >= 0.6) {
      payload->add_local_features("quality:low_glare");
    }

    if (accepted) {
      payload->set_status(v1::OBSERVATION_STATUS_ACCEPTED);
    } else {
      payload->set_status(v1::OBSERVATION_STATUS_REJECTED);
      payload->set_failure_reason("no acceptable keyframe: " +
                                  issues_to_text(selected.quality.issues));
    }
    return writer_.Write(event);
  }

 private:
  v1::ObservationEvent base_event() {
    ++sequence_;
    v1::ObservationEvent event;
    event.set_schema_version(1);
    event.set_event_id(request_.request_id() + "-event-" +
                       std::to_string(sequence_));
    event.set_request_id(request_.request_id());
    event.set_sequence(sequence_);
    event.set_occurred_at_unix_ms(unix_milliseconds(SystemClock::now()));
    return event;
  }

  const v1::ObservationRequest& request_;
  grpc::ServerWriter<v1::ObservationEvent>& writer_;
  std::uint64_t sequence_{0};
};

}  // namespace

PerceptionRuntimeService::PerceptionRuntimeService(
    PerceptionServiceOptions options)
    : options_(std::move(options)) {
  if (options_.queue_capacity == 0 || options_.max_frames == 0 ||
      options_.progress_interval_frames == 0) {
    throw std::invalid_argument(
        "queue capacity, max frames and progress interval must be positive");
  }
  if (options_.source.kind == ObservationSourceKind::video_file &&
      options_.source.video_path.empty()) {
    throw std::invalid_argument("video source requires a path");
  }
}

grpc::Status PerceptionRuntimeService::Observe(
    grpc::ServerContext* context,
    const v1::ObservationRequest* request,
    grpc::ServerWriter<v1::ObservationEvent>* writer) {
  if (request == nullptr || writer == nullptr || context == nullptr) {
    return {grpc::StatusCode::INTERNAL, "gRPC provided a null handler argument"};
  }
  if (const auto invalid = validate_request(*request); invalid.has_value()) {
    // 该请求从未注册，不能按它提供的 ID 擅自删除另一个正在运行的请求。
    mark_completed_observation();
    return {grpc::StatusCode::INVALID_ARGUMENT, *invalid};
  }

  const auto stop_source = register_request(request->request_id());
  if (!stop_source) {
    // 资源竞争请求也没有进入 active_requests_，这里只记录 RPC 已结束。
    mark_completed_observation();
    return {grpc::StatusCode::RESOURCE_EXHAUSTED,
            "another observation is already using this perception runtime"};
  }

  EventWriter events(*request, *writer);
  const auto finish = [this, request](grpc::Status status) {
    complete_request(request->request_id());
    return status;
  };

  try {
    if (!events.progress("request_accepted", 0,
                         "observation request accepted")) {
      stop_source->request_stop();
      return finish({grpc::StatusCode::CANCELLED, "client closed event stream"});
    }

    auto source = open_source(options_.source);
    if (!events.progress("source_opened", 5,
                         "source opened: " + source->descriptor().label)) {
      stop_source->request_stop();
      return finish({grpc::StatusCode::CANCELLED, "client closed event stream"});
    }

    perception::KeyframeSelector selector(
        perception::FrameQualityEvaluator(options_.quality_policy));
    runtime::PerceptionRuntime engine(
        {.queue_capacity = options_.queue_capacity,
         .overflow_policy = options_.overflow_policy,
         .max_consumed_frames = options_.max_frames});
    const auto started_at = runtime::MonotonicClock::now();
    bool stream_open = true;
    bool timed_out = false;

    const runtime::RuntimeSummary summary = engine.run(
        *source,
        [&](runtime::Frame&& frame) {
          const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
              runtime::MonotonicClock::now() - started_at);
          if (context->IsCancelled() || stop_source->stop_requested()) {
            return runtime::ConsumerDecision::stop;
          }
          if (elapsed.count() >= request->timeout_ms()) {
            timed_out = true;
            stop_source->request_stop();
            return runtime::ConsumerDecision::stop;
          }

          static_cast<void>(selector.consider(std::move(frame)));
          const auto evaluated = selector.statistics().evaluated_frames;
          if (evaluated % options_.progress_interval_frames == 0) {
            const auto percent = static_cast<std::uint32_t>(std::min<std::uint64_t>(
                90, 5 + (evaluated * 85 / options_.max_frames)));
            stream_open = events.progress(
                "scanning", percent,
                "evaluated=" + std::to_string(evaluated) +
                    " accepted=" +
                    std::to_string(selector.statistics().accepted_frames));
            if (!stream_open) {
              stop_source->request_stop();
              return runtime::ConsumerDecision::stop;
            }
          }
          return runtime::ConsumerDecision::continue_running;
        },
        stop_source->get_token());

    if (!stream_open || context->IsCancelled()) {
      return finish({grpc::StatusCode::CANCELLED, "observation stream cancelled"});
    }
    if (timed_out) {
      static_cast<void>(events.failure("deadline_exceeded",
                                       "observation did not finish before timeout",
                                       true));
      return finish({grpc::StatusCode::DEADLINE_EXCEEDED,
                     "observation request timeout elapsed"});
    }
    if (stop_source->stop_requested()) {
      static_cast<void>(events.failure("cancelled",
                                       "observation was explicitly cancelled",
                                       true));
      return finish({grpc::StatusCode::CANCELLED,
                     "observation was explicitly cancelled"});
    }
    if (summary.stop_reason == runtime::RuntimeStopReason::source_error ||
        summary.stop_reason == runtime::RuntimeStopReason::consumer_error) {
      static_cast<void>(events.failure("runtime_error", summary.error_message,
                                       true));
      return finish({grpc::StatusCode::INTERNAL, summary.error_message});
    }

    const perception::SelectedKeyframe* selected = selector.selected();
    if (selected == nullptr) {
      static_cast<void>(events.failure("no_frames",
                                       "source produced no frame to evaluate",
                                       true));
      return finish(grpc::Status::OK);
    }

    const std::filesystem::path request_directory =
        std::filesystem::absolute(options_.artifact_root / request->request_id());
    std::filesystem::create_directories(request_directory);
    const std::filesystem::path image_path =
        request_directory /
        (selector.has_accepted_frame() ? "keyframe.jpg" : "rejected-best.jpg");
    if (!cv::imwrite(path_to_utf8(image_path), selected->pixels)) {
      throw std::runtime_error("OpenCV could not write selected keyframe");
    }

    if (!events.progress("keyframe_selected", 95,
                         "selected source frame " +
                             std::to_string(selected->source_frame_sequence))) {
      return finish({grpc::StatusCode::CANCELLED, "client closed event stream"});
    }
    if (!events.observation(*selected, selector.has_accepted_frame(), image_path)) {
      return finish({grpc::StatusCode::CANCELLED, "client closed event stream"});
    }
    return finish(grpc::Status::OK);
  } catch (const std::invalid_argument& error) {
    static_cast<void>(events.failure("invalid_configuration", error.what(), false));
    return finish({grpc::StatusCode::FAILED_PRECONDITION, error.what()});
  } catch (const std::exception& error) {
    static_cast<void>(events.failure("source_or_artifact_error", error.what(), true));
    return finish({grpc::StatusCode::UNAVAILABLE, error.what()});
  }
}

grpc::Status PerceptionRuntimeService::Cancel(
    grpc::ServerContext*,
    const v1::CancelObservationRequest* request,
    v1::CancelObservationResponse* response) {
  if (request == nullptr || response == nullptr ||
      !is_safe_identifier(request->request_id()) || request->reason().empty()) {
    return {grpc::StatusCode::INVALID_ARGUMENT,
            "cancellation requires a safe request_id and non-empty reason"};
  }

  response->set_request_id(request->request_id());
  std::lock_guard lock(active_mutex_);
  const auto found = active_requests_.find(request->request_id());
  if (found == active_requests_.end()) {
    response->set_accepted(false);
    return grpc::Status::OK;
  }
  found->second->request_stop();
  response->set_accepted(true);
  return grpc::Status::OK;
}

void PerceptionRuntimeService::wait_for_completed_observation() {
  std::unique_lock lock(completion_mutex_);
  completion_changed_.wait(lock,
                           [this] { return completed_observations_ > 0; });
}

std::shared_ptr<std::stop_source> PerceptionRuntimeService::register_request(
    const std::string& request_id) {
  std::lock_guard lock(active_mutex_);
  if (!active_requests_.empty() || active_requests_.contains(request_id)) {
    return {};
  }
  auto source = std::make_shared<std::stop_source>();
  active_requests_.emplace(request_id, source);
  return source;
}

void PerceptionRuntimeService::complete_request(const std::string& request_id) {
  {
    std::lock_guard lock(active_mutex_);
    active_requests_.erase(request_id);
  }
  mark_completed_observation();
}

void PerceptionRuntimeService::mark_completed_observation() {
  std::lock_guard lock(completion_mutex_);
  ++completed_observations_;
  completion_changed_.notify_all();
}

}  // namespace realsight::transport
