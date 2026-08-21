# 初学者的 14 章阅读路线

> 本文件用于快速阅读原有 14 章。需要每天可验收的 24 天课程、答辩题、真实设备改造和
> 简历 Gate，请以 [PORTFOLIO-ROADMAP.md](PORTFOLIO-ROADMAP.md) 为准。

## 先建立正确目标

一个月的目标不是“背完 C++、Python、Agent、Docker”，而是你能自己讲清楚一条真实工程
链路：相机为什么在 C++，模型为什么不能直接判断，证据如何追踪，暂停后为什么能恢复，
HTTP 为什么不取代 gRPC。每章都应该按“读概念 -> 运行 Demo -> 改一个小值 -> 看测试”学习。

## 四周安排

### 第一周：从问题到状态

1. 读第 1、2 章，运行 `examples/ch01_evidence_loop.py` 与第 2 章 Demo。
2. 读第 3 章，画出 Agent、服务、工具三者的职责边界。
3. 读第 4 章的 `TaskSession`、`Observation`、`Evidence`、`Action` 和 `RunEvent`。
4. 读第 5 章，运行 interrupt Demo；故意改错 request ID，观察为何恢复被拒绝。

本周检查：你能用自己的话解释 Observation 和 Evidence 的区别，能说出为何 checkpoint
不能保存 gRPC channel 或摄像头对象。

### 第二周：持久化与工程骨架

1. 读第 6 章，区分 checkpoint、Evidence Ledger 和图片 artifact。
2. 读第 7 章，理解 capability、预算、超时、幂等和审计的目的。
3. 读第 8 章，运行 `realsight-doctor`，认识 `pyproject.toml`、`uv.lock`、配置文件。
4. 修改 `config/realsight.example.toml` 的一个安全值，运行测试看 Pydantic 如何拦截错误。

本周检查：你能解释为什么“有 SQLite”不等于“可分布式恢复”，也能解释密钥不该写入
TOML 或 checkpoint。

### 第三周：C++ 感知与证据

1. 读第 9 章的 Frame、队列、质量和关键帧代码；运行视频回放。
2. 读第 10 章的 `.proto` 和 Python gRPC 适配器；确认只输出关键帧路径。
3. 读第 11 章，运行视觉 Evidence Demo；把脚本 OCR 中的 `65W` 改为矛盾数值，观察 gap。
4. 读第 12 章，比较 30W、45W、65W 和非 PD 协议的规则结果。

本周检查：你能沿着 `ObservationRequest -> Protobuf -> Observation -> Evidence` 说清每个
边界，也知道 ExampleBook 只是夹具。

### 第四周：主循环与 API

1. 先读 `workflow/models.py`，再读 `planner.py`，最后读 `main_agent.py`。
2. 运行 `examples/ch13_main_agent_loop.py`，记录两个 interrupt 的不同作用。
3. 运行 `examples/ch14_api_replay.py`，打开 `http://127.0.0.1:8000/docs` 浏览 API。
4. 阅读 `application/task_service.py` 和 `application/api.py`，最后再尝试 OpenAI 模式。

本周检查：你能回答为什么最终回答由模板引用规则结果，而不是让模型自由写；能解释
WebSocket 的 `after_sequence` 如何用于断线重连。

## 建议的每日循环

```powershell
uv run --locked pytest python/tests/test_ch13_main_agent_loop.py -q
uv run --locked python examples/ch13_main_agent_loop.py
```

每次只做一个可观察改动，例如把示例标签的功率改为 45W，先预测规则应为
`limited_power`，再运行测试或 Demo 验证。不要一开始替换成真实 OCR、摄像头、云检索和
多用户 API；先保证每层的输入输出仍然可解释。完成离线边界学习后，再按 24 天路线的
Day 19～23 启用正式 PaddleOCR、C++ gRPC 和真实摄像头。

## 排错顺序

1. `uv run --locked realsight-doctor`：先检查 Python、包、配置和工具链。
2. 单元测试：定位到具体章节，而不是只跑一个长 Demo。
3. 查看 `RunEvent`：确认是规划、外部观察、视觉、检索还是规则节点失败。
4. 查看 `Evidence.source_id`、`derived_from`、`source_metadata`：找事实来源。
5. 最后才看 OpenAI、OCR、摄像头或网络；它们不是所有问题的起点。

## 何时开启真实 OpenAI

先确保离线的 168 项测试和两个 Demo 都能通过。随后设置环境变量切换 `openai`，只观察
模型是否在允许工具中选择合理动作；不要用一次模型输出证明硬件兼容。真实 API 调用会
产生费用，先把 `reasoning_effort`、预算与第 7 章治理策略理解清楚。
