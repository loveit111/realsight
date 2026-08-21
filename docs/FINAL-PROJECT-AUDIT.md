# RealSight 最终项目审计

## 审计结论

**教学型工程 MVP 通过验收。** 项目已经从单图问答演进为可规划、可暂停、可恢复、可
追踪的主动感知与证据决策系统；它尚不是可直接面向公网或真实硬件结论的生产系统。

## 已验证项目

| 项目 | 结果 |
|---|---|
| 第 1-14 章 Python 自动化测试 | 168 项通过 |
| 第 13 章离线主循环 | 两次 interrupt 后 `conditions_met`，含证据与未知边界 |
| 第 14 章 API 回放 | HTTP 创建/恢复、WebSocket 事件重放、规则终态通过 |
| 代码质量 | Ruff 通过，mypy 对 33 个正式源码/测试文件无问题 |
| C++ 边界 | 已保持原有 C++20/OpenCV/gRPC 运行时；未容器化摄像头 |
| 双模式 | deterministic 可离线验收；OpenAI 仅在显式 Key 下启用受限规划 |

## 技术栈审计

| 层 | 采用技术 | 选择理由 |
|---|---|---|
| C++ 运行时 | C++20、CMake、OpenCV、线程队列 | 实时帧处理和本机硬件适配 |
| 跨语言契约 | Protobuf、gRPC | 强类型、流式事件、deadline、取消；不传逐帧视频 |
| Python 领域层 | Python 3.12、Pydantic v2 | 严格 JSON/状态验证和可序列化契约 |
| 工作流 | LangGraph、checkpoint、interrupt | 可暂停恢复的长任务控制流 |
| Agent 规划 | Deterministic Planner、OpenAI Responses API tools | 离线可测，真实模型仅选择受限动作 |
| 视觉证据 | TextRecognizer Protocol、VisionEvidenceAgent | 可替换 OCR/VLM，保留来源和 gap |
| 决策 | LocalSpecificationCatalog、纯 Python 规则 | 可审计、可重复、不让模型代替逻辑 |
| 应用接口 | FastAPI、WebSocket、Uvicorn | 浏览器/CLI 任务接口与进度事件 |
| 持久化 | SQLite、LangGraph SqliteSaver | 单进程教学恢复；非分布式队列 |
| 工程工具 | uv、pytest、Ruff、mypy、Docker Compose | 锁定环境、测试、静态质量与 Python 服务交付 |

## 发现并修正的问题

1. `REQUEST_VIEW Action` 与 `pending_request` 曾没有在同一次状态更新中保存；第 4 章
   契约测试使该错误立即暴露，现已原子写入。
2. 错误 Observation 的 request ID 一度可能在图内失败后消费 interrupt；现已在发送
   `Command(resume=...)` 前验证，客户端可安全重试。
3. 扩展 capability 配置时曾破坏第 8 章的文本替换兼容测试；现保留原 capability 前缀
   并追加新权限，168 项回归测试通过。

## 明确未完成项

- 真实 OCR/VLM、真实摄像头和真实 C++ gRPC 端到端设备验收；
- 人工核验、版本化、授权合规的真实厂商规格资料；
- USB-C 线缆 e-marker、端口角色、PD 实际协商、多口分配和温度等硬件验证；
- 用户认证、权限、TLS、CORS、速率限制、密钥托管与审计保留策略；
- 多实例任务调度、共享事件总线、分布式锁、队列和观测平台；
- 通用商品识别、通用真伪鉴定、多摄像头、多目标跟踪。

这些不是“稍后再说”的小细节，而是从教学 MVP 走向企业生产系统前必须单独立项和验收
的工作。当前正确的下一步是先用一个真实、受控、可复核的设备样本替换一份教学夹具，
而不是扩大问题范围。
