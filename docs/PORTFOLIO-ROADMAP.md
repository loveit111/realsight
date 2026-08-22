# RealSight：零基础到简历准入的四周路线

> 本文件是 24 天总览。逐日的概念讲解、代码调用链、故障实验、陷阱和答辩答案要点，
> 已拆成 [RealSight 24 天精读实战课](course/README.md)。零基础学习请从课程正文开始，
> 不要只按本页清单打卡。

## 目标与使用方法

这不是阅读清单，而是一套 24 天、每天约 6 小时的实作课程。每天必须留下代码、测试、
图、命令记录或脱稿讲解中的至少一项。先在离线回放上学会边界，再启用真实摄像头和
PaddleOCR；否则外部依赖会把基础问题藏起来。

每一天使用同一循环：

1. 60 分钟学习概念，并用自己的话写五句话。
2. 90 分钟沿当天入口阅读调用链，不逐行抄注释。
3. 90 分钟运行、改参数、制造一个预期故障。
4. 60 分钟写或修改测试，先写预期再运行。
5. 30 分钟画数据流、状态图或时序图。
6. 30 分钟合上资料录音复述；不能讲清的内容加入次日复习。

## 掌握层级

| 级别 | 可以做什么 | 证据 |
|---|---|---|
| L1 | 识别概念和文件 | 能指出类型定义和入口 |
| L2 | 沿调用链讲清并改参数 | Demo 行为符合事前预测 |
| L3 | 独立定位故障、补测试、写适配器 | 有自己的非参数改造和回归测试 |
| L4 | 重新划边界并解释取舍 | 能比较替代方案及失败模式 |

简历准入目标：Agent/Evidence/Action 达 L4；Python、LangGraph、契约、API、并发、gRPC、
OpenCV 和 OCR 应用达 L3；C++ 语言基础至少 L2；认证、TLS、分布式和 SLAM 只需知道缺口。

## 第一周：编程基础、契约与整体认知

### Day 1：环境、命令行、验收和 Git

- 学：路径、进程、端口、环境变量、退出码；解释器与编译器；依赖锁。
- 读：`pyproject.toml`、`uv.lock`、`CMakePresets.json`、`.github/workflows/ci.yml`。
- 做：运行 doctor、完整 pytest、CMake/CTest、第 10/13/14 章 Demo；记录成功标志。
- 故障：把 gRPC 地址改到无服务端口，分辨“连接失败”和“代码构建失败”。
- 产物：`docs/BASELINE-VALIDATION.md` 的本机记录；解释 Python 执行与 C++ 构建的差异。
- 验收：能说明源码、生成代码、构建目录、运行工件分别在哪里。

### Day 2：Python 最小基础

- 学：值、容器、函数、分支、循环、异常、类、dataclass、Enum、类型标注、Protocol。
- 读：`contracts/models.py` 中 `Evidence`，`workflow/replay.py` 中三个 Protocol 实现。
- 做：不复制项目代码，写最小 Evidence/Belief/Action 练习，输入 Evidence 后列出缺失字段。
- 测：正常、缺失、重复 ID、冲突值、错误 target 五例 pytest。
- 验收：解释 `tuple`/`list`、不可变数据/运行时连接、异常/业务状态的差异。

### Day 3：Pydantic 与跨语言契约

- 学：严格类型、字段约束、validator、判别联合、Protobuf 字段号和兼容性。
- 读：`StrictContract`、`ObservationRequest`、`Observation`、`Evidence`、`Action`、`RunEvent`。
- 做：构造多余字段、无时区时间、0.0～1.0 外置信度、混合 Action payload。
- 测：记录错误在 HTTP、Pydantic、Proto 还是业务层被拒绝。
- 验收：能解释为何 checkpoint 可保存 ID/状态，却不能保存摄像头、channel 或锁。

### Day 4：Agent 与证据驱动边界

- 学：Agent、workflow、service、tool；幻觉、可追溯性和确定性决策。
- 读：`planner.py` 与 `compatibility/rules.py`，列出各自能决定和不能决定的内容。
- 做：用假模型返回一个越权工具、错误参数和自由文本结论，观察边界如何拒绝。
- 产物：一张“模型 / OCR / C++ / 规则”的决策权限表。
- 验收：能回答“为什么模型不能直接说兼容”和“为什么 OCR 文本还不是 Evidence”。

### Day 5：异步、超时、取消、中断、恢复和存储

- 学：同步/异步/线程；deadline/cancel/retry/idempotency；checkpoint/ledger/artifact。
- 读：`main_agent.py` 的 interrupt/resume 校验和 `TaskService.with_sqlite`。
- 做：错误 interrupt ID、错误 request ID、取消后恢复、失败后恢复、SQLite 重启读取。
- 验收：画出 thread ID、interrupt ID、Observation request ID 的生命周期，不得混用。

### Day 6：第一周答辩

- 讲 15 分钟：问题、架构、八个核心类型、完整数据流、能力边界。
- 做 20 道自测题，至少 16 道无需提示答对；错误题必须定位到一个源文件重讲。
- Gate：不会解释 Observation/Evidence 或模型/规则边界时，不进入真实 OCR。

## 第二周：正式 Python Agent 闭环

### Day 7：Observation 到 Evidence

- 读：`vision/evidence_agent.py` 和 `vision/paddle_ocr.py`。
- 学：画面质量、OCR 置信度、Evidence 置信度三者为何不可互换。
- 做：空文本、低置信度、坐标塌缩、OCR 异常、功率冲突；观察 EvidenceGap。
- 测：增加一种标签失败路径，证明不能形成 confirmed Evidence。

### Day 8：规格检索与规则

- 读：`compatibility/catalog.py`、`models.py`、`rules.py` 和教学规格 JSON。
- 做：手算 30/45/65/100W 与 PD/非 PD 组合，再以参数化测试验证。
- 讲：冲突、缺失、协议、最低功率、推荐功率的判断优先级。
- 验收：能指出 `conditions_met` 的每个未知边界。

### Day 9：Planner

- 读：确定性 Planner 和 OpenAI Planner 的共同 `Planner` 端口。
- 做：为每种 State 列允许 Action；用假 Responses 测试越权工具和错误参数。
- 验收：模型函数调用最终必须重新通过严格 Action 契约，不能创造 Evidence。

### Day 10：LangGraph 主循环

- 画：`plan → prepare → interrupt → resume → apply → retrieve → rules → answer`。
- 做：逐节点记录 State 差异；加入一个 RunEvent；制造 OCR 缺口触发再次观察。
- 测：错误恢复不消费原 interrupt；FAILED/CANCELLED/COMPLETED 都是不可恢复终态。
- 验收：10 分钟讲清一次任务所有状态和 checkpoint 内容。

### Day 11：FastAPI、TaskService 与 WebSocket

- 读：`application/api.py`、`task_service.py`、`serve.py`。
- 学：DTO/领域模型、`asyncio.to_thread`、404/409、WebSocket 关闭码、断线补发。
- 做：创建、查询、恢复、取消；以 `after_sequence` 从中间序列重连。
- 测：增加一个错误载荷和一个失败终态 WebSocket 测试。

### Day 12：独立重写最小闭环

- 不看 `main_agent.py` 写 150～250 行：缺标签请求观察、缺型号询问、查规格、跑规则、回答。
- 允许不用 LangGraph，但 Observation、Evidence、Action 必须是不同对象。
- Gate：60 分钟内加一个 Action 或错误路径并补测试；否则回到 Day 7～11。

## 第三周：C++ 实时感知与跨语言边界

### Day 13：C++20 基础

- 学：头/源文件、namespace、对象生命周期、栈/堆、引用/指针、RAII、智能指针、移动语义。
- 读：`frame.hpp/.cpp`、`frame_source.hpp/.cpp`。
- 做：解释 Frame 禁止复制；写一个只移动不复制的小类测试。
- 验收：能说明 `cv::Mat` 浅拷贝与关键帧 `clone()` 的关系。

### Day 14：并发队列与背压

- 学：mutex、condition variable、producer/consumer、`jthread`、stop token、关闭语义。
- 读：`frame_queue.cpp`、`perception_runtime.cpp`。
- 做：容量 1、增加消费延迟，比较 `drop-oldest` 和 `block` 的统计。
- 测：停止时队列不死锁、不误报消费、每个生命周期结果可解释。

### Day 15：摄像头与视频源

- 学：`VideoCapture`、摄像头失败与视频 EOF、单调时钟与 UTC。
- 做：回放视频；真实摄像头 `--camera 0`；错误编号；任务期间断开设备。
- 记录：source backend、分辨率、FPS、停止原因和退出码。

### Day 16：质量评估与关键帧

- 学：Laplacian 方差、灰度曝光、HSV 高亮低饱和反光、ROI 比例。
- 做：清晰/模糊/反光/过暗各组图片，事前预测 issues 后运行。
- 改：调整一个阈值并补 C++ 测试，记录影响；不要把质量分称为 OCR 成功概率。

### Day 17：Protobuf 与 gRPC

- 学：message/enum/optional/oneof/service、字段号、stream、deadline、cancel、status。
- 做：手动启动 C++ server，用 Python 客户端验证成功、取消、超时、无帧、错误请求。
- 验收：能解释为何只跨边界传 Observation 与工件引用，而非逐帧视频。

### Day 18：C++ 周答辩

- 脱稿画生产者—队列—消费者—关键帧—gRPC 时序图。
- 独立加一个统计字段或质量 issue，完成测试、Proto 影响分析和构建验证。
- Gate：不能解释背压、移动语义、deadline/cancel 时，不进入真实设备作品集采集。

## 第四周：真实适配器、治理、数据和公开交付

### Day 19：PaddleOCR 适配器

- 安装 `ocr` extra，阅读 `PaddleOcrTextRecognizer`。
- 验证模型只初始化一次、像素多边形归一化、异常转换和 provider metadata。
- 普通测试用 fake pipeline；真实模型测试标记 `integration`。

### Day 20：OCR 小评测

- 用同一标签采集正常、倾斜、反光、模糊、暗光图。
- 保存原始文本、置信度、字段 Evidence 与耗时，不只截一张“成功图”。
- 调整的是预处理/阈值/拍摄方式；不能声称训练了模型。

### Day 21：正式 C++ / OCR / API 装配

- 读：`serve.py` 的配置工厂和资源关闭路径。
- 做：独立启动 C++ 摄像头 gRPC，再启动 PaddleOCR API，完成创建→自动观察→型号恢复。
- 故障：错误地址、服务中途停止、无 accepted Observation、缺 OCR extra。
- 验收：默认 unavailable 不伪造结果；真实 provider 只在显式配置时启用。

### Day 22：治理预算

- 读：`governance/policy.py` 和 `main_agent.py` 中所有 `consume`。
- 做：分别耗尽 iteration、command、observation、external attempt、cost unit；拒绝 capability。
- 验收：产生 `TASK_FAILED`、会话进入 FAILED、旧 interrupt 不能恢复、不会无限观察。

### Day 23：30 个真实设备样本

- 按 [REAL-DEVICE-VALIDATION.md](REAL-DEVICE-VALIDATION.md) 的固定矩阵采集并人工标注。
- 运行 `evaluate_device_dataset.py`，保留 JSONL、JSON 和 Markdown；不手改报告数字。
- Gate：证据缺失/冲突中的错误正结论必须为 0，否则先修复再谈简历。

### Day 24：公开作品集与模拟面试

- 清理密钥、绝对个人路径、模型缓存、构建目录和大文件；确认 README 两种启动模式。
- 提交架构图、真实指标、失败样例、限制、CI 徽章和 2～4 分钟演示视频链接。
- 做 30 分钟连续答辩和 60 分钟现场小改造。
- 只有下节所有准入项通过，才把项目放在简历核心位置。

## 简历准入 Gate

- [ ] 不看文档完成回放和真实摄像头演示。
- [ ] 30 秒、3 分钟、10 分钟三个版本都能讲清。
- [ ] 解释 Observation/Evidence/Belief/Action/RunEvent 与双 target 边界。
- [ ] 解释 C++ 队列、背压、停止、`cv::Mat::clone()` 和 gRPC 取消。
- [ ] 解释 LangGraph thread/interrupt/request ID 和 WebSocket 补发。
- [ ] 至少两个非参数改造有自己写的测试；当前 OCR、正式装配、治理都可作为候选。
- [ ] 至少 30 条真实样本、可复算报告、失败样例和 0 次不安全错误正结论。
- [ ] 能现场定位一个 provider、OCR、契约、checkpoint 或规则故障。
- [ ] 主动说明认证、TLS、真实规格、PD 协商、多实例和 SLAM 尚未完成。
- [ ] 公开提交历史能分辨上游教学基线与自己的扩展。

## 三种脱稿讲法

30 秒：

> 我复现并扩展了一个证据驱动主动视觉系统。C++ 从连续摄像头帧中做背压和关键帧
> 质量筛选，通过 gRPC 只把 Observation 交给 Python；Python 用 PaddleOCR、Evidence、
> LangGraph 和确定性规则完成可暂停恢复的 USB-C 条件判断。模型只选动作，证据不足或
> 冲突时系统不会给正结论。

3 分钟必须覆盖：问题与非目标、C++/Python 分工、Observation→Evidence、两次中断、
规格与规则、治理、实测数字与一个失败样例。

10 分钟必须再覆盖：队列和停止时序、Proto 边界、OCR 坐标和元数据、checkpoint 身份、
WebSocket 重放、五类预算、三个最重要取舍和生产化缺口。

## 面试自测题

1. 为什么不把视频逐帧传给 Python？
2. `Observation` 和 `Evidence` 分别证明什么？
3. OCR 文本为何不能直接进入规则？
4. `confirmed/probable/unknown/conflict` 如何转换？
5. 模型可以决定什么，为什么不能决定 verdict？
6. 为什么最终回答使用规则模板？
7. `drop_oldest` 与 `block_producer` 各牺牲什么？
8. Frame 为何禁止复制，关键帧何时需要 `clone()`？
9. 队列 close、stop request、Cancel RPC 有何不同？
10. deadline 与 Cancel RPC 为什么都需要？
11. Proto 字段号为什么不能复用？
12. thread ID、interrupt ID、request ID 各解决什么身份问题？
13. 错误恢复为何必须在 `Command(resume)` 前拒绝？
14. checkpoint、Evidence ledger、artifact 为什么分开？
15. `after_sequence` 如何支持 WebSocket 重连？
16. 为什么同步图放入 `asyncio.to_thread`？
17. 五类治理预算分别阻止什么失败？
18. `conditions_met` 仍然有哪些未知边界？
19. 真实指标的分母是什么，错误正结论如何定义？
20. 如果要升级为空间感知，第一条需要改变的契约是什么？

## 简历模板

项目名：`RealSight：基于 C++ 实时感知与 Python Agent 的证据驱动主动视觉系统`

- 复现并扩展 C++20/OpenCV 感知运行时，以 `jthread`、`stop_token` 和有界队列实现
  可取消采集与背压，在 30+ 真实样本上记录 `[实际 FPS/丢帧率]`。
- 以 Protobuf/gRPC server streaming 打通 C++ 与 Python，支持 deadline、Cancel RPC、
  进度和结构化失败，仅传 Observation 元数据与关键帧引用。
- 使用 Pydantic、LangGraph、SQLite、FastAPI、WebSocket 和本地 PaddleOCR 构建可暂停
  恢复的 Evidence 工作流，以确定性规则输出 USB-C 条件 verdict。
- 建立 Python/C++ CI 与真实设备评测，在缺失/冲突样本中实现 `[实际错误正结论]`，并
  报告 `[实际端到端成功率/p95]`。

方括号只能替换为评测脚本输出的真实数字。禁止写“生产级平台”“训练 OCR”“真实 PD
协商”或没有可复算数据支撑的准确率。
