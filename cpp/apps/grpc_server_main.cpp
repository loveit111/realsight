/*
 * 文件整体逻辑
 * -----------
 * 这是第 10 章 C++ gRPC 感知服务的命令行入口。它把视频/摄像头、监听地址、工件目录
 * 和帧预算解析成 PerceptionServiceOptions，启动本机 server-streaming 服务；--once
 * 让自动化 Demo 在完成一个 Observe 后正常退出。
 *
 * 使用的技术栈
 * -------------
 * C++20 from_chars/filesystem；gRPC ServerBuilder；RealSight transport 服务实现。
 *
 * 调用流程
 * --------
 * 命令行参数 -> parse_options -> PerceptionRuntimeService
 * -> ServerBuilder.AddListeningPort/RegisterService/BuildAndStart
 * -> 输出 SERVER_READY
 * -> Wait，或 --once 等待一个请求后 Shutdown。
 *
 * 重要边界
 * --------
 * 默认只监听 127.0.0.1 并使用 insecure credentials，适合本机 C++/Python 进程通信。
 * 若跨主机部署，必须增加 TLS、身份认证、网络访问控制和工件对象存储，不能直接把
 * --listen 改成 0.0.0.0 就视为生产安全。
 */

#include <charconv>
#include <filesystem>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>

#include <grpcpp/grpcpp.h>

#include "realsight/transport/perception_service.hpp"

namespace {

struct CommandLineOptions {
  std::string listen_address{"127.0.0.1:50051"};
  realsight::transport::PerceptionServiceOptions service;
  bool source_selected{false};
  bool exit_after_one{false};
};

template <typename Integer>
Integer parse_integer(const std::string_view text, const std::string& name) {
  Integer value{};
  const auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size()) {
    throw std::invalid_argument(name + " must be an integer");
  }
  return value;
}

std::string require_value(int& index,
                          const int argc,
                          char* argv[],
                          const std::string& flag) {
  if (++index >= argc) {
    throw std::invalid_argument(flag + " requires a value");
  }
  return argv[index];
}

CommandLineOptions parse_options(const int argc, char* argv[]) {
  CommandLineOptions options;
  for (int index = 1; index < argc; ++index) {
    const std::string flag = argv[index];
    if (flag == "--video") {
      if (options.source_selected) {
        throw std::invalid_argument("choose exactly one video or camera source");
      }
      options.service.source.kind =
          realsight::transport::ObservationSourceKind::video_file;
      options.service.source.video_path =
          require_value(index, argc, argv, flag);
      options.source_selected = true;
    } else if (flag == "--camera") {
      if (options.source_selected) {
        throw std::invalid_argument("choose exactly one video or camera source");
      }
      options.service.source.kind =
          realsight::transport::ObservationSourceKind::camera;
      options.service.source.camera_index = parse_integer<int>(
          require_value(index, argc, argv, flag), "camera index");
      options.source_selected = true;
    } else if (flag == "--listen") {
      options.listen_address = require_value(index, argc, argv, flag);
    } else if (flag == "--artifacts") {
      options.service.artifact_root = require_value(index, argc, argv, flag);
    } else if (flag == "--max-frames") {
      options.service.max_frames = parse_integer<std::uint64_t>(
          require_value(index, argc, argv, flag), "max frames");
    } else if (flag == "--progress-interval") {
      options.service.progress_interval_frames = parse_integer<std::uint64_t>(
          require_value(index, argc, argv, flag), "progress interval");
    } else if (flag == "--no-realtime") {
      options.service.source.pace_as_recorded = false;
    } else if (flag == "--once") {
      options.exit_after_one = true;
    } else {
      throw std::invalid_argument("unknown argument: " + flag);
    }
  }
  if (!options.source_selected) {
    throw std::invalid_argument("one of --video PATH or --camera INDEX is required");
  }
  return options;
}

void print_usage() {
  std::cerr
      << "usage: realsight-perception-grpc (--video PATH | --camera INDEX) "
         "[--listen HOST:PORT] [--artifacts DIR] [--max-frames N] "
         "[--progress-interval N] [--no-realtime] [--once]\n";
}

}  // namespace

int main(const int argc, char* argv[]) {
  try {
    const CommandLineOptions options = parse_options(argc, argv);
    realsight::transport::PerceptionRuntimeService service(options.service);

    grpc::ServerBuilder builder;
    int selected_port = 0;
    builder.AddListeningPort(options.listen_address,
                             grpc::InsecureServerCredentials(), &selected_port);
    builder.RegisterService(&service);
    std::unique_ptr<grpc::Server> server = builder.BuildAndStart();
    if (!server || selected_port == 0) {
      throw std::runtime_error("gRPC server could not bind listening address");
    }

    std::cout << "SERVER_READY address=" << options.listen_address
              << " selected_port=" << selected_port << std::endl;
    if (options.exit_after_one) {
      service.wait_for_completed_observation();
      server->Shutdown();
    }
    server->Wait();
    return 0;
  } catch (const std::exception& error) {
    print_usage();
    std::cerr << "gRPC perception server failed: " << error.what() << '\n';
    return 1;
  }
}
