# RealSight 项目审计

## 结论

RealSight 已从纯教学骨架扩展为可配置的作品集 Alpha：回放闭环、C++/Python gRPC、真实
OCR 适配端口、正式 API 装配、治理预算和评测工具均有代码与测试，官方 PP-OCRv6 ONNX
模型也已完成一次合成图推理冒烟。它仍未完成真实摄像头 30 样本、真实标签 OCR 指标和公开远端 CI，因此暂时适合作为“正在完成真实验收
的复现并扩展项目”，还不应在简历里填写准确率、实时性或生产级表述。

## 已实现与证据

| 维度 | 当前状态 | 可以如何表述 |
|---|---|---|
| 架构与边界 | C++ 感知、Python 证据/工作流、规则 verdict 分离 | 可重点讲设计取舍 |
| Python 工作流 | interrupt/resume、双 target、规则绑定答案 | 能独立调试后可写 |
| C++ 感知 | 并发队列、背压、质量、关键帧、停止 | 真实摄像头数字待采集 |
| gRPC | streaming、deadline、Cancel、结构化失败 | 可写；需现场解释失败语义 |
| OCR | PaddleOCR 3.x 适配器、坐标/异常/元数据测试 | 可写“接入”，不可写“训练”或虚构准确率 |
| 正式装配 | `serve.py` 按配置注入 gRPC/OCR并关闭资源 | 默认 unavailable，不伪造结果 |
| 治理 | capability 与五类预算进入 checkpoint/主循环 | 可写本地工作流护栏，不是分布式限流 |
| API | FastAPI/SQLite/WebSocket 断线补发 | 本机单进程，无认证/TLS |
| 评测 | 严格 JSONL、准确率/错误正结论/p50/p95/帧统计 | 工具已完成，真实数据尚未采集 |
| Git/CI | 本地基线与改造提交、Actions 配置 | 远端公开运行尚未验证 |

## 本轮非参数改造

1. `PaddleOcrTextRecognizer` 把 PaddleOCR `predict()` 结果归一化为 `TextRegion`，模型只
   初始化一次；错误进入 `RecognitionFailure`，Evidence metadata 记录 provider、版本、
   模型、引擎、图片尺寸和推理耗时。
2. `serve.py` 根据 `[perception]`/`[vision]` 配置创建 gRPC 与 OCR 适配器，注入正式
   TaskService；C++ 服务保持独立进程，退出时释放 channel、SQLite 和 OCR 资源。
3. `GovernanceUsage` 随 checkpoint 保存 command/observation/external/cost 用量，主图在
   迭代、受限 Action、观察、OCR、规格检索和模型调用边界执行策略；超限进入 FAILED。
4. 设备评测契约和 CLI 从 JSONL 复算字段准确率、错误正结论、端到端 verdict、p50/p95
   和 produced/consumed/dropped，不允许用 schema 示例冒充成绩。

## 仍未完成且不能夸大

- PaddleOCR 已在隔离环境完成真实模型冒烟，但输入不是充电器标签，不能提供字段准确率。
- 尚未用真实摄像头、充电器和笔记本完成固定 30 样本矩阵。
- 教学规格目录不是授权、持续维护的真实厂商知识库。
- 规则不验证线缆 e-marker、端口能力、USB PD 动态协商、多口分配或温升。
- 没有认证、TLS、密钥托管、跨实例事件总线、分布式调度、速率限制和可观测平台。
- 没有检测/跟踪、稳定 `track_id`、相机位姿、VIO、SLAM 或多摄像头空间状态。
- GitHub 公开仓库、远端 Actions 徽章和演示视频需要仓库所有者最后确认并发布。

## 简历准入判定

当前代码改造已满足“至少两个非参数改造”和自动化测试的工程部分；真正准入还差：

1. 真实 PaddleOCR 与摄像头完整演示。
2. 30 条真实记录及脚本生成报告。
3. 缺失/冲突样本错误正结论为 0。
4. 一个公开失败样例及修复/限制说明。
5. 公开 GitHub Actions 绿灯和 2～4 分钟演示。
6. 30 分钟脱稿答辩与 60 分钟现场小改造。

满足后使用“复现并扩展”，把报告中的真实数字填入简历模板；在此之前可以放在个人学习
仓库或简历的“项目实践中”，但不要作为最强核心项目声称已经完成真实视觉准确率验收。
