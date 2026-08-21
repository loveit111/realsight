# RealSight

## 第 13、14 章：完整闭环

```powershell
uv sync --locked --all-groups
uv run --locked python examples/ch13_main_agent_loop.py
uv run --locked python examples/ch14_api_replay.py
uv run --locked pytest
```

第 13 章会在“背面标签观察”和“笔记本型号”两处暂停并恢复；第 14 章以 FastAPI
和 WebSocket 暴露同一工作流。离线配置默认使用确定性规划器。启用真实 OpenAI
规划时，在环境中设置 `OPENAI_API_KEY`，并设置：

```powershell
$env:REALSIGHT_AGENT_PROVIDER = "openai"
$env:REALSIGHT_OPENAI_MODEL = "gpt-5.6-terra"
uv run --locked realsight-api
```

API 默认只绑定 `127.0.0.1:8000`，文档位于
`http://127.0.0.1:8000/docs`。本机容器化 Python API：

```powershell
docker compose up --build
```

容器不包含 C++ 摄像头运行时。真实 C++ 服务继续在 Windows 本机运行，通过第 10
章 gRPC 适配器与 Python 连接；不要跨进程传输逐帧视频。

RealSight 是一个教学型主动感知项目：C++ 负责实时感知运行时，Python 负责证据工作流、
治理与 Agent 编排。当前仓库聚焦单摄像头、单目标和 USB-C 充电兼容性场景。

## 快速验证

```powershell
uv sync --locked --all-groups
uv run --locked realsight-doctor
uv run --locked pytest
uv run --locked cmake --preset windows-gcc-debug
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
```

第 10 章要求 C++ OpenCV、Protobuf 与 gRPC 开发包。MSYS2 UCRT64 环境可安装：

```powershell
E:\msys64\usr\bin\pacman.exe -S --needed mingw-w64-ucrt-x86_64-opencv mingw-w64-ucrt-x86_64-grpc
```

Python 的 `opencv-python/cv2` wheel 不能替代 C++ 头文件和链接库。第 9 章的原始回放仍可
单独运行：

```powershell
build\windows-gcc-debug\cpp\realsight-generate-video.exe runtime-data\ch09-demo.avi 24
build\windows-gcc-debug\cpp\realsight-perception.exe --video runtime-data\ch09-demo.avi --overflow block --queue-capacity 4 --no-realtime
```

第 10 章从同一个 `.proto` 生成 C++ 与 Python 类型，计算清晰度、曝光和抗反光质量，
只保存并发送最终关键帧 Observation。代码生成和一键跨语言联调：

```powershell
uv run --locked python scripts/generate_python_proto.py
uv run --locked python scripts/run_ch10_demo.py
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
```

成功场景末尾会输出 `CH10_DEMO_OK`，取消场景会输出 `CH10_CANCEL_OK`。工件写到
`runtime-data/ch10-artifacts/<request_id>/keyframe.jpg`；该目录是运行产物，不进入 Git。

`examples/` 与根目录 `tests/` 保存第 1～7 章的教学代码；正式 Python 包位于
`python/src/realsight/`，正式 C++ 工程位于 `cpp/`。
