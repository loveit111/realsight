#pragma once

/*
 * 文件整体逻辑
 * -----------
 * 本文件声明第 10 章的 C++ gRPC 服务适配层。Python 发送一个 ObservationRequest，
 * 服务在本机打开预配置的视频或摄像头，复用第 9 章运行时筛选关键帧，并以 server
 * streaming 依次返回进度、最终 Observation 或结构化失败。Cancel RPC 通过 stop_source
 * 停止同 request_id 的活动采集。
 *
 * 使用的技术栈
 * -------------
 * - gRPC C++ 同步 API：ServerWriter、ServerContext、状态码和本地不安全凭据。
 * - Protobuf 生成类型：realsight.v1.PerceptionRuntime 服务契约。
 * - C++20：stop_source、mutex、condition_variable、filesystem。
 *
 * 调用流程
 * --------
 * Python stub.Observe(request, deadline)
 * -> PerceptionRuntimeService::Observe()
 * -> OpenCvFrameSource + runtime::PerceptionRuntime
 * -> KeyframeSelector
 * -> 本地 JPEG 工件
 * -> ObservationEvent 流回 Python。
 * Cancel(request_id) -> 找到活动 stop_source -> request_stop()。
 *
 * 重要边界
 * --------
 * 同步 API 是本课程“单摄像头、低并发”MVP 的刻意选择；gRPC 会为 RPC 调度服务线程，
 * 但本服务额外限制同一进程只执行一个 Observe，避免多个请求争抢同一摄像头。需要大量
 * 并发会话时应迁移 callback API 与设备租约管理，而不是直接扩大线程数。
 */

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <mutex>
#include <stop_token>
#include <string>
#include <unordered_map>

#include <grpcpp/grpcpp.h>

#include "realsight.grpc.pb.h"
#include "realsight/perception/quality_evaluator.hpp"
#include "realsight/runtime/frame_queue.hpp"

namespace realsight::transport {

enum class ObservationSourceKind { video_file, camera };

struct ObservationSourceConfig {
  ObservationSourceKind kind{ObservationSourceKind::video_file};
  std::filesystem::path video_path;
  int camera_index{0};
  bool pace_as_recorded{true};
  int requested_width{0};
  int requested_height{0};
};

struct PerceptionServiceOptions {
  ObservationSourceConfig source;
  std::filesystem::path artifact_root{"runtime-data/observations"};
  perception::QualityPolicy quality_policy;
  std::size_t queue_capacity{4};
  runtime::OverflowPolicy overflow_policy{runtime::OverflowPolicy::drop_oldest};
  std::uint64_t max_frames{120};
  std::uint64_t progress_interval_frames{10};
};

class PerceptionRuntimeService final
    : public v1::PerceptionRuntime::Service {
 public:
  explicit PerceptionRuntimeService(PerceptionServiceOptions options);

  grpc::Status Observe(grpc::ServerContext* context,
                       const v1::ObservationRequest* request,
                       grpc::ServerWriter<v1::ObservationEvent>* writer) override;

  grpc::Status Cancel(grpc::ServerContext* context,
                      const v1::CancelObservationRequest* request,
                      v1::CancelObservationResponse* response) override;

  void wait_for_completed_observation();

 private:
  [[nodiscard]] std::shared_ptr<std::stop_source> register_request(
      const std::string& request_id);
  void complete_request(const std::string& request_id);
  void mark_completed_observation();

  PerceptionServiceOptions options_;
  std::mutex active_mutex_;
  std::unordered_map<std::string, std::shared_ptr<std::stop_source>>
      active_requests_;
  std::mutex completion_mutex_;
  std::condition_variable completion_changed_;
  std::uint64_t completed_observations_{0};
};

}  // namespace realsight::transport
