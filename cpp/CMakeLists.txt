# 文件整体逻辑
# ------------
# 本文件把第 9 章 runtime、第 10 章画面质量/关键帧库、Protobuf 生成代码和 gRPC
# 服务组织成分层 target。生成文件只进入 build/generated，不混进学习源码目录。
#
# 使用的技术栈
# - CMake target model：PUBLIC/PRIVATE 使用范围、别名目标、CTest、自定义代码生成。
# - OpenCV core/imgproc/imgcodecs/videoio：质量计算、关键帧 JPEG、采集与视频编解码。
# - Protobuf/gRPC：从同一 .proto 生成 C++ message/service stub 并链接同步服务。
#
# 调用流程
# find_package(OpenCV/Protobuf/gRPC)
# -> protoc + grpc_cpp_plugin -> build/generated/*.pb.*
# -> realsight_proto + realsight_runtime + realsight_perception
# -> realsight-perception / realsight-perception-grpc / 测试视频生成器
# -> BUILD_TESTING 下的运行时、质量与关键帧测试
# -> ctest 统一执行。
#
# 重要边界
# gRPC transport 单独链接 realsight_proto，不让 runtime 核心反向依赖网络。Python stub
# 由 scripts/generate_python_proto.py 生成，CMake 只负责 C++ 构建产物。

find_package(OpenCV REQUIRED COMPONENTS core imgproc imgcodecs videoio)
find_package(Protobuf CONFIG REQUIRED)

# MSYS2 的 gRPC Config 会引用 ZLIB::ZLIB 和 OpenSSL targets，但 Windows 上的 CMake
# Find 模块无法总是从编译器路径自动推断 UCRT64 前缀。这里从实际 g++ 路径推导前缀，
# 不硬编码盘符；Linux/macOS 继续走各自标准搜索路径。
if(MINGW)
  get_filename_component(REALSIGHT_TOOLCHAIN_BIN "${CMAKE_CXX_COMPILER}" DIRECTORY)
  get_filename_component(REALSIGHT_TOOLCHAIN_PREFIX "${REALSIGHT_TOOLCHAIN_BIN}" DIRECTORY)
  if(NOT DEFINED ZLIB_ROOT)
    set(ZLIB_ROOT "${REALSIGHT_TOOLCHAIN_PREFIX}")
  endif()
  if(NOT DEFINED OPENSSL_ROOT_DIR)
    set(OPENSSL_ROOT_DIR "${REALSIGHT_TOOLCHAIN_PREFIX}")
  endif()
endif()
find_package(ZLIB REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(gRPC CONFIG REQUIRED)
message(STATUS "RealSight Chapter 10 uses OpenCV ${OpenCV_VERSION}")
message(STATUS "RealSight Chapter 10 uses Protobuf ${Protobuf_VERSION}")

set(REALSIGHT_PROTO_DIR "${PROJECT_SOURCE_DIR}/contracts")
set(REALSIGHT_PROTO_FILE "${REALSIGHT_PROTO_DIR}/realsight.proto")
set(REALSIGHT_GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
set(REALSIGHT_PROTO_SOURCES
    "${REALSIGHT_GENERATED_DIR}/realsight.pb.cc"
    "${REALSIGHT_GENERATED_DIR}/realsight.grpc.pb.cc")
set(REALSIGHT_PROTO_HEADERS
    "${REALSIGHT_GENERATED_DIR}/realsight.pb.h"
    "${REALSIGHT_GENERATED_DIR}/realsight.grpc.pb.h")

add_custom_command(
  OUTPUT ${REALSIGHT_PROTO_SOURCES} ${REALSIGHT_PROTO_HEADERS}
  COMMAND ${CMAKE_COMMAND} -E make_directory "${REALSIGHT_GENERATED_DIR}"
  COMMAND protobuf::protoc
  ARGS
    "--proto_path=${REALSIGHT_PROTO_DIR}"
    "--cpp_out=${REALSIGHT_GENERATED_DIR}"
    "--grpc_out=${REALSIGHT_GENERATED_DIR}"
    "--plugin=protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>"
    "${REALSIGHT_PROTO_FILE}"
  DEPENDS "${REALSIGHT_PROTO_FILE}" protobuf::protoc gRPC::grpc_cpp_plugin
  COMMENT "Generating RealSight C++ Protobuf and gRPC sources"
  VERBATIM
)

add_library(realsight_proto STATIC ${REALSIGHT_PROTO_SOURCES})
add_library(RealSight::proto ALIAS realsight_proto)
target_include_directories(realsight_proto PUBLIC "${REALSIGHT_GENERATED_DIR}")
target_compile_features(realsight_proto PUBLIC cxx_std_20)
target_link_libraries(realsight_proto PUBLIC protobuf::libprotobuf gRPC::grpc++)

function(realsight_enable_warnings target_name)
  if(MSVC)
    target_compile_options(${target_name} PRIVATE /W4 /permissive-)
  else()
    target_compile_options(${target_name} PRIVATE -Wall -Wextra -Wpedantic)
  endif()
endfunction()

add_library(
  realsight_runtime STATIC
  src/frame.cpp
  src/frame_queue.cpp
  src/frame_source.cpp
  src/opencv_frame_source.cpp
  src/perception_runtime.cpp
  src/quality_evaluator.cpp
  src/keyframe_selector.cpp
  src/runtime_info.cpp
)
add_library(RealSight::runtime ALIAS realsight_runtime)

target_include_directories(
  realsight_runtime
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
    ${OpenCV_INCLUDE_DIRS}
)
target_compile_features(realsight_runtime PUBLIC cxx_std_20)
target_link_libraries(realsight_runtime PUBLIC ${OpenCV_LIBS})
realsight_enable_warnings(realsight_runtime)

add_library(realsight_transport STATIC src/perception_service.cpp)
add_library(RealSight::transport ALIAS realsight_transport)
target_include_directories(
  realsight_transport
  PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_compile_features(realsight_transport PUBLIC cxx_std_20)
target_link_libraries(
  realsight_transport
  PUBLIC RealSight::runtime RealSight::proto gRPC::grpc++
)
realsight_enable_warnings(realsight_transport)

add_executable(realsight-perception apps/perception_main.cpp)
target_link_libraries(realsight-perception PRIVATE RealSight::runtime)
realsight_enable_warnings(realsight-perception)

add_executable(realsight-perception-grpc apps/grpc_server_main.cpp)
target_link_libraries(realsight-perception-grpc PRIVATE RealSight::transport)
realsight_enable_warnings(realsight-perception-grpc)

add_executable(realsight-generate-video tools/generate_test_video.cpp)
target_compile_features(realsight-generate-video PRIVATE cxx_std_20)
target_include_directories(realsight-generate-video PRIVATE ${OpenCV_INCLUDE_DIRS})
target_link_libraries(realsight-generate-video PRIVATE ${OpenCV_LIBS})
realsight_enable_warnings(realsight-generate-video)

if(BUILD_TESTING)
  add_executable(realsight-runtime-info-test tests/runtime_info_test.cpp)
  target_link_libraries(realsight-runtime-info-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-runtime-info-test)
  add_test(NAME realsight-runtime-info COMMAND realsight-runtime-info-test)

  add_executable(realsight-frame-queue-test tests/frame_queue_test.cpp)
  target_link_libraries(realsight-frame-queue-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-frame-queue-test)
  add_test(NAME realsight-frame-queue COMMAND realsight-frame-queue-test)

  add_executable(realsight-perception-runtime-test tests/perception_runtime_test.cpp)
  target_link_libraries(realsight-perception-runtime-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-perception-runtime-test)
  add_test(NAME realsight-perception-runtime COMMAND realsight-perception-runtime-test)

  add_executable(realsight-opencv-source-test tests/opencv_source_test.cpp)
  target_link_libraries(realsight-opencv-source-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-opencv-source-test)
  add_test(NAME realsight-opencv-source COMMAND realsight-opencv-source-test)

  add_executable(realsight-quality-evaluator-test tests/quality_evaluator_test.cpp)
  target_link_libraries(realsight-quality-evaluator-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-quality-evaluator-test)
  add_test(NAME realsight-quality-evaluator COMMAND realsight-quality-evaluator-test)

  add_executable(realsight-keyframe-selector-test tests/keyframe_selector_test.cpp)
  target_link_libraries(realsight-keyframe-selector-test PRIVATE RealSight::runtime)
  realsight_enable_warnings(realsight-keyframe-selector-test)
  add_test(NAME realsight-keyframe-selector COMMAND realsight-keyframe-selector-test)
endif()
