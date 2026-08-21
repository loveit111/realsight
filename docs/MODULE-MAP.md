# RealSight 模块地图

## C++：现实接触层

| 模块 | 输入 | 输出 | 责任边界 |
|---|---|---|---|
| `cpp/src/frame_source.cpp` / `opencv_frame_source.cpp` | 摄像头或视频路径 | 连续 Frame | 不做 OCR 或 Agent 推理 |
| `frame_queue.cpp` | Frame | 有界线程队列 | 负责背压和帧生命周期 |
| `quality_evaluator.cpp` | Frame | 清晰度、曝光、反光、占比评分 | 不决定业务字段 |
| `keyframe_selector.cpp` | 质量化 Frame | 最终关键帧 | 不传每一帧给 Python |
| `perception_runtime.cpp` | 请求、Frame 流 | Observation 候选 | 不读取资料、不判断 USB-C |
| `perception_service.cpp` | Protobuf request | gRPC progress/failure/Observation | 只做跨进程传输 |

## Python：证据与编排层

| 模块 | 主要输入 | 主要输出 | 责任边界 |
|---|---|---|---|
| `contracts/models.py` | 外部 JSON/Protobuf 数据 | Pydantic 契约 | 不做 I/O 或判断 |
| `perception/grpc_client.py` | ObservationRequest | Observation/进度/失败 | 不做 OCR、规则或存储 |
| `vision/evidence_agent.py` | accepted Observation + artifact | Evidence/EvidenceGap | 不控制摄像头、不判兼容 |
| `vision/paddle_ocr.py` | 关键帧路径 | RecognitionDocument | 不解析功率、不形成兼容 verdict |
| `compatibility/catalog.py` | confirmed laptop_model Evidence | 三条规格 Evidence | 只精确匹配本地资料 |
| `compatibility/rules.py` | 两个 target 的 Evidence | UsbCCompatibilityResult | 不调用模型、不代表硬件实测 |
| `governance/policy.py` | capability、上限、已用量 | 新预算快照或拒绝 | 不是认证、计费或分布式限流 |
| `workflow/planner.py` | MainAgentState | 受限 Action | 不执行工具、不产出 verdict |
| `workflow/main_agent.py` | checkpoint、Action、恢复载荷 | 新 checkpoint、RunEvent | 不直接处理连续视频 |
| `workflow/replay.py` | ObservationRequest | 回放或 gRPC Observation | 回放器不是 OCR/真实摄像头 |
| `application/task_service.py` | 用例请求 | 主工作流状态 | 单进程，不是消息队列 |
| `application/api.py` | HTTP/WebSocket | JSON 状态与事件 | 无认证的本地教学 API |
| `application/serve.py` | TOML 配置 | 完整运行时装配 | 不隐式启动 C++ 摄像头服务 |
| `evaluation/device_dataset.py` | 人工标注 JSONL | 可复算指标 | 不采集数据、不生成真值 |

## 数据对象如何流动

| 类型 | 来自哪里 | 下一站 | 初学者要记住什么 |
|---|---|---|---|
| `RealityObject` | 创建任务 | GraphState | 一个持续存在的现实目标 |
| `ObservationRequest` | Planner Action | C++ 或回放器 | 描述“想看什么”，不是图像 |
| `Observation` | C++ gRPC | 视觉 Agent | 描述“一次看到了什么质量的帧” |
| `Evidence` | 视觉、用户、资料 | Belief/规则 | 带来源和状态的事实 |
| `BeliefState` | 充电器 Evidence | Planner | 只属于一个 target |
| `MainAgentState` | 双目标上下文 | LangGraph checkpoint | 同时保存充电器与笔记本边界 |
| `Action` | Planner | workflow 节点或外部暂停 | 下一步意图，不是执行事实 |
| `RunEvent` | workflow | WebSocket | 已发生的过程事实 |
| `UsbCCompatibilityResult` | 规则引擎 | 最终回答 | 仅表示已知静态条件 |
