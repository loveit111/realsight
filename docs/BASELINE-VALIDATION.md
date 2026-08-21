# RealSight 本机验证记录

## 可复现命令

```powershell
uv sync --locked --all-groups
uv run --locked realsight-doctor
uv run --locked pytest -m "not integration"
uv run --locked ruff format --check python/src python/tests scripts
uv run --locked ruff check python/src/realsight python/tests scripts
uv run --locked mypy python/src/realsight python/tests
uv run --locked cmake --preset windows-gcc-debug
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
uv run --locked python scripts/run_ch10_demo.py
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
uv run --locked python examples/ch13_main_agent_loop.py
uv run --locked python examples/ch14_api_replay.py
```

## 2026-08-21 本机记录

| 门禁 | 结果 |
|---|---|
| Python 非设备测试 | 192 项通过：120 项章节教学测试 + 72 项正式包测试 |
| Ruff | format check 与 lint 通过 |
| mypy | strict，对 40 个正式源码/测试文件无问题 |
| C++ | Debug 构建成功，6/6 CTest 通过 |
| C++→Python | accepted Observation stream 与工件验证通过 |
| 取消 | Cancel RPC、C++ stop 与 Python CANCELLED 传播通过 |
| LangGraph | 两次 interrupt/resume 后规则终态通过 |
| API | HTTP 创建/恢复、WebSocket 补发与终态关闭通过 |
| OCR 依赖 | 隔离环境导入 PaddleOCR 3.7.0、ONNX Runtime 1.28.0 |
| OCR 冒烟 | 官方 PP-OCRv6 ONNX 模型初始化并对合成关键帧完成推理 |
| OCR integration marker | 1 项真实模型测试通过；普通测试选择中明确排除 |
| Proto | 重新生成后无 Git diff |

OCR 冒烟图不是充电器标签，只证明安装、构造参数、真实推理和结果适配可运行，不构成
功率/协议准确率。真实摄像头 30 样本和公开 GitHub Actions 仍需在对应设备/远端执行；
不得把 fake recognizer、合成图或 schema 示例记为真实成绩。

## Python 解释执行与 C++ 编译构建

Python 源文件由当前虚拟环境的解释器加载；`uv.lock` 固定第三方版本，pytest/Ruff/mypy
分别验证运行行为、风格与静态类型。C++ 源文件先由 CMake 配置生成构建系统，再经编译器
生成并链接本机可执行文件；OpenCV/gRPC 需要头文件和原生库，Python wheel 不能替代。

因此，“Python 包能 import”不说明 C++ 已构建，“C++ CTest 通过”也不说明 FastAPI、
checkpoint 或真实 OCR 已验收。交付时必须分别报告这些门禁。
