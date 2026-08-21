# 第 1 章 RealSight：从单图问答到可恢复的主动感知系统

> 本章关键词：连续感知、证据规划、主动观察、状态恢复、C++ 运行时、LangGraph、DeepAgents、确定性规则

RealSight 的第一版不是一套万能商品识别系统，也不是把摄像头画面持续发送给多模态大模型的聊天应用。它要验证的是一条更重要的工程主线：

> C++ 持续接触现实世界并产生有效观察；Python Agent 根据任务判断缺少什么证据；工具和规则把观察转换为可验证的结论；当证据不足时，系统暂停并请求新的视角或用户输入。

这条主线同时包含实时系统、AI Agent、工具调用、状态管理、人机协作和证据工程。它也是从普通 AI 应用开发走向 VR/AR、空间感知与现实世界 Agent 的一个合适过渡点。

本章不急着安装大型框架，也不接入真实大模型。我们先把项目为什么存在、各部分应该负责什么、数据如何流动、什么情况下可以下结论讲清楚。配套 Demo 只验证“证据不足 -> 生成补充动作 -> 注入模拟证据 -> 规则判断 -> 证据化回答”这一段**逻辑切片**。它没有 LangGraph 检查点，也没有真正暂停等待用户；真正的中断与恢复将在第 5～6 章实现。

---

## 1. 本章目标、学习前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 区分单图问答、随机视频抽帧、连续感知运行时和主动感知 Agent。
2. 解释为什么“摄像头 + 多模态大模型”仍然不是完整的现实感知系统。
3. 说明 C++ 运行时、Python 工作流、Agent、子 Agent、工具和规则引擎的职责边界。
4. 理解 Reality Object、Observation、Evidence、Belief State、Action 五个核心概念。
5. 使用证据缺口解释 Agent 为什么要请求新的视角或询问用户。
6. 说明 LangGraph、DeepAgents、gRPC、FastAPI、WebSocket 和 SQLite 在后续项目中的位置。
7. 运行本章 Demo，观察系统如何拒绝在证据不足或证据冲突时过早下结论。

### 1.2 学习前提

本章只要求：

- 能读懂基础 Python 数据结构和函数。
- 知道摄像头视频由连续图像帧组成。
- 知道大模型可以生成文本，也可以通过工具调用请求外部能力。

不要求提前掌握 C++ 多线程、OpenCV、LangGraph、DeepAgents 或 gRPC。它们会在后续章节逐步实现。

### 1.3 本章产物

本章完成后，项目中应出现四类产物：

- 一份主动感知系统架构和业务闭环说明。
- 一份系统能力边界。
- 一组稳定领域类型的初始定义。
- 一个可以运行和测试的 USB-C 证据循环逻辑切片。
- 一张可以直接映射为自动化测试的 USB-C MVP 验收表。

本章 Demo 不是最终项目的缩小版摄像头系统。它先验证两个最重要的不变量：系统是否知道自己还不知道什么，以及一个目标的观察是否会被错误写入另一个目标。

---

## 2. 从一个充电问题开始

用户把一个充电器放到摄像头前，问：

> 这个能给我的笔记本电脑充电吗？

如果只把当前画面发给多模态大模型，模型可能看到一个带 USB-C 接口的充电器，然后回答“看起来可以”。这句话听起来合理，却没有完成真正的兼容性判断。

要回答这个问题，系统至少要获得以下证据：

- 充电器输出接口是否为 USB-C。
- 充电器是否支持目标设备需要的充电协议。
- 充电器有哪些电压、电流输出档位。
- 最大输出功率是多少。
- 目标笔记本的完整型号、接口、协议和最低功率要求是什么。

当前正面画面通常只能提供其中一小部分。USB-C 只描述连接器形态，不能单独证明支持 USB Power Delivery，也不能证明功率足够。一个写着“65W”的充电器，也不代表任意线缆、任意接口和任意设备都能获得 65W。

因此，系统不能把第一次模型输出当成最终事实。更合理的过程是：

1. 先确认用户正在询问哪个现实物体。
2. 把“能否充电”转换成证据需求。
3. 检查当前观察已经提供哪些证据。
4. 发现功率和协议缺失后，请求背面标签视角。
5. 发现目标设备不明确后，询问完整型号。
6. 从清晰标签中提取输出档位。
7. 查询目标设备官方规格。
8. 使用程序执行兼容规则。
9. 输出结论、证据和未知边界。

RealSight 的核心不在于某一次识别有多聪明，而在于它能否持续推进这条证据链。

---

## 3. 从单图问答到主动感知

### 3.1 第一阶段：单张图片问答

最简单的多模态应用是：

~~~text
用户上传图片
-> 模型描述图片
-> 用户继续提问
-> 模型根据这张图片回答
~~~

这种系统适合：

- 描述静态画面。
- 识别明显类别。
- 解释当前可见文字。
- 对单张图片做开放语义理解。

它的问题是，观察内容完全由用户决定。图片没有拍到背面标签时，模型无法主动从现实世界获得背面信息。它最多只能说“请再上传一张图片”，但并不维护连续目标身份，也不知道下一张图片是否仍然是同一个充电器。

### 3.2 第二阶段：随机视频抽帧

为了让模型“看视频”，一种常见方案是每隔若干秒截取一帧：

~~~text
摄像头视频
-> 定时抽帧
-> 将图片发送给模型
-> 汇总模型描述
~~~

这比单图问答多了时间维度，但仍然存在三个根本问题。

第一，抽到的帧不一定有用。画面可能模糊、反光、遮挡，或者目标只占很小区域。

第二，系统不知道为什么选择这一帧。定时器只知道“时间到了”，不知道任务正在缺少功率、接口还是型号。

第三，不同帧缺少稳定身份。前一帧中的充电器和后一帧中的充电器，可能被当成两个无关对象，也可能把两个商品的证据错误合并。

### 3.3 第三阶段：连续感知运行时

连续感知要求一个常驻运行的本地程序持续处理摄像头数据。它不应该把每一帧都发送给 Python 或大模型，而应在本地完成：

- 设备打开、关闭和异常恢复。
- 图像帧读取和生命周期管理。
- 线程队列与背压。
- 目标选择和持续跟踪。
- 清晰度、曝光、反光和目标占比评估。
- 重复视角过滤与关键帧选择。
- 观察请求的执行。

经过这些处理后，高频、冗余的视频流被压缩为低频、有目的的 Observation。

### 3.4 第四阶段：主动感知 Agent

有了 C++ 运行时，系统仍然只解决了“怎样稳定地看”。主动感知还要解决“为了完成当前任务，下一步应该看什么”。

这时 Agent 的职责不是逐帧处理图像，而是：

- 理解用户目标。
- 生成证据计划。
- 检查当前证据是否充分。
- 选择观察、检索、询问或规则动作。
- 处理证据冲突。
- 决定是否允许生成最终回答。

可以用一句话区分：

> 普通多模态问答由用户决定给模型看什么；RealSight 由任务和证据缺口共同决定下一步还需要看什么。

### 3.5 四种系统形态对比

| 系统形态 | 谁选择观察内容 | 是否维护现实状态 | 能否主动补充信息 | 典型任务 |
|---|---|---|---|---|
| 单图问答 | 用户 | 否 | 否 | 图片描述 |
| 随机抽帧 | 定时程序 | 很弱 | 否 | 粗略视频摘要 |
| 连续感知运行时 | C++ 规则与视觉模块 | 是 | 只能响应规则 | 实时检测和跟踪 |
| 主动感知系统 | Agent 规划，C++ 执行 | 是 | 是 | 多步骤现实任务 |

RealSight 选择第四种形态，但不会让 Agent 控制摄像头的每一帧。Agent 只表达任务级观察目标，C++ 决定怎样在实时流水线中满足它。

---

## 4. 深度研搜与 RealSight 的对应关系

深度研搜系统处理的是开放研究问题：主智能体拆解问题，不同子能力查询网络、数据库和知识库，主智能体检查信息是否充分，然后补充研究或生成报告。

RealSight 面对的是现实世界问题，但二者具有相同的上层逻辑：

| 深度研搜 | RealSight |
|---|---|
| 研究问题 | 现实任务 |
| 研究计划 | 证据计划 |
| 网络、数据库、知识库 | 摄像头观察、OCR、设备规格库 |
| 资料片段 | Observation 与 Evidence |
| 信息不足时继续搜索 | 证据不足时请求新视角 |
| 主智能体汇总资料 | 主 Agent 更新 Belief State |
| 生成研究报告 | 生成证据化结论 |

但是两者也有一个重要区别：摄像头是一种连续、高频、对时间敏感的数据源。网络搜索可以等待几秒，而视频帧队列如果处理不过来，会不断堆积或延迟。因此 RealSight 不能把 C++ 运行时简单实现成一个普通 Agent 工具函数，更不能在 Python Agent 中逐帧循环。

### 4.1 为什么不直接做很多 Agent

多 Agent 的价值主要来自：

- 隔离大量上下文。
- 对不同专业任务使用不同提示词和工具。
- 对独立任务并发执行。

USB-C MVP 只需要两个确实存在专业边界的子能力：

1. 视觉证据分析：读取选定观察，调用 OCR 或多模态模型，输出结构化视觉证据。
2. 规格资料检索：根据设备型号查询说明书、规格库或知识库，输出资料证据。

C++ 运行时不是子 Agent，兼容规则也不是子 Agent。前者是持续运行的实时服务，后者是可测试的确定性程序。

### 4.2 为什么使用 LangGraph 作为外层工作流

RealSight 的主流程包含明确状态和循环：

~~~text
理解问题
-> 锁定目标
-> 规划证据
-> 收集观察
-> 验证证据
-> 证据不足则中断
-> 恢复后继续
-> 运行规则
-> 生成回答
~~~

这类流程需要可追踪、可暂停和可恢复的状态图。LangGraph 的持久化机制以 thread 为单位保存检查点，可用于人机协作、错误恢复和执行历史；interrupt 可以在流程中暂停，并使用相同 thread_id 恢复。这正适合“请翻转充电器”或“请补充设备型号”这样的现实交互。

### 4.3 DeepAgents 放在哪里

DeepAgents 是建立在 LangChain 组件和 LangGraph 运行时之上的 Agent harness。它可以提供上下文管理、文件后端、子 Agent、长期记忆等组合能力；当前版本中的任务规划是可选能力，而不是所有 DeepAgents 默认自动拥有的行为。

因此本课程不会在第 2 章就把 DeepAgents 当作基础。第 2 章先手写最小 Tool Calling 循环，第 4～6 章再学习 LangGraph 状态、检查点与中断；到第 13 章，只有当专业子任务的上下文隔离确实有价值时，才决定是否引入 DeepAgents。这样可以先理解 Agent 的基本执行协议，再评价高级 harness 带来了什么。

推荐结构是：

> LangGraph 外层状态图负责确定流程与恢复；Agent 节点负责开放理解和证据规划；子 Agent 负责隔离的专业任务；程序工具负责确定计算和验证。

### 4.4 总体架构

~~~mermaid
flowchart LR
    U["用户 / 前端"] -->|HTTP| API["FastAPI 任务接口"]
    API --> G["LangGraph 证据工作流"]
    G --> P["主 Agent：证据规划"]
    P -->|gRPC 观察请求| C["C++ 感知运行时"]
    C -->|Observation 事件| OR["Observation 接收节点"]
    OR --> V["视觉证据节点"]
    G --> K["规格资料节点 / 子 Agent"]
    V --> E["Evidence Ledger"]
    K --> E
    E --> Q["证据验证节点"]
    Q --> D{"证据充分且无冲突？"}
    D -->|否| H["中断：请求视角或型号"]
    H --> P
    D -->|是| R["确定性兼容规则"]
    R --> O["证据化结论"]
    G --> S["检查点与会话存储"]
    G -->|WebSocket| U
~~~

图中所有任务事件都先进入 LangGraph 节点。C++ 不直接调用视觉子 Agent，主 Agent 也不绕开状态图私自调度专业能力。这个边界决定了 Observation 由谁接收、证据在哪一步写入、失败后从哪个检查点继续。

---

## 5. 系统各部分的职责

### 5.1 C++ 实时感知运行时

C++ 运行时负责把连续视频转换为与任务有关的 Observation。它的核心约束是实时性、资源稳定性和设备可靠性。

它应负责：

- 摄像头和视频文件输入。
- 图像内存、时间戳和帧生命周期。
- 有界队列、丢帧策略和线程退出。
- 目标选择、跟踪和 target_id。
- 图像质量评估和关键帧选择。
- 响应 ObservationRequest。
- 生成 Observation 事件和性能指标。

它不负责：

- 理解用户最终意图。
- 决定 USB-C 是否兼容。
- 用自然语言解释结果。
- 自由调用网络或知识库。

### 5.2 LangGraph 工作流

LangGraph 是流程和状态的骨架。它负责：

- 定义任务节点和状态转移。
- 保存 TaskSession 与 Belief State。
- 根据证据缺口选择下一节点。
- 在需要用户动作时暂停。
- 使用 thread_id 恢复同一次任务。
- 记录失败位置并支持重试。

这里要区分“流程状态”和“业务证据”。检查点回答的是“任务执行到哪里”，Evidence 回答的是“我们凭什么相信某个事实”。

第一版明确采用一个简单约束：`thread_id == session_id`。`session_id` 是 RealSight 业务会话标识，LangGraph 配置中的 `thread_id` 直接复用它。这样 API、检查点、事件和会话目录不会出现两套未映射标识。以后若一个业务会话需要分叉多个执行线程，再增加显式映射表，而不是悄悄改变语义。

### 5.3 主 Agent

主 Agent 是证据编排者，而不是所有问题的直接回答者。它负责：

- 把自然语言问题解析为意图。
- 选择用户关注的目标。
- 生成必要证据字段。
- 比较计划与当前 Belief State。
- 调度视觉分析或规格检索。
- 生成新的观察请求。
- 在规则完成后组织证据化回答。

主 Agent 不直接宣称 OCR 文本为真，也不心算最终功率。它必须引用工具结果和证据状态。

### 5.4 两个专业子 Agent

视觉证据子 Agent 处理：

- 关键帧或目标裁剪图。
- OCR 候选文本。
- 接口和可见特征。
- 视觉证据的来源与置信度。

规格资料子 Agent 处理：

- 完整设备型号。
- 官方说明书或结构化设备库。
- 设备接口、协议和功率要求。
- 资料版本、来源和适用范围。

子 Agent 返回结构化结果，不把大段搜索过程全部塞回主 Agent。

### 5.5 工具和规则引擎

工具负责边界明确、可以复现和测试的任务：

| 工具 | 输入 | 输出 |
|---|---|---|
| OCR | 标签图像 | 文字、位置、置信度 |
| 参数解析 | 标签文字 | 电压电流档位 |
| 功率计算 | 电压、电流 | 功率值 |
| 规格查询 | 完整型号 | 官方规格证据 |
| 兼容规则 | 充电器和设备证据 | 规则结果 |
| 图像质量 | 图像或 ROI | 清晰度、反光、占比 |

规则引擎必须与自然语言生成分开。相同证据输入应得到相同兼容结果，这样才能写测试、定位错误并解释结论。

### 5.6 API、通信和存储

浏览器通过 HTTP 提交任务，通过 WebSocket 接收进度。Python 与 C++ 通过 gRPC/Protobuf 交换强类型请求和事件。SQLite 在 MVP 中保存检查点和结构化元数据，图像产物保存在会话目录。

不要通过 gRPC 持续发送全部原始帧。第一版只传输 Observation 元数据、按需裁剪图或可访问的本地路径。通信优化的第一原则不是换协议，而是减少无价值数据。

---

## 6. 五个核心领域概念

### 6.1 Reality Object：现实物体

Reality Object 表示现实环境中持续存在的目标。它不是一张图片，也不是某一帧中的检测框。

一个充电器从正面翻到背面时，外观变化很大，但仍是同一个对象。系统必须使用稳定 target_id 把不同视角的 Observation 和 Evidence 绑定到同一个目标。

~~~json
{
  "target_id": "charger-01",
  "category": "usb_c_charger",
  "selected": true,
  "tracking_status": "active"
}
~~~

Reality Object 解决的是身份问题：背面标签不能错误绑定到旁边另一只充电器。

### 6.2 Observation：一次有效观察

Observation 是一次对 Reality Object 有任务价值的观察，通常来自经过质量筛选的关键帧。

~~~json
{
  "observation_id": "obs-back-label-01",
  "target_id": "charger-01",
  "view_type": "back_label",
  "quality_score": 0.91,
  "image_path": "artifacts/session-ch01/back_label.jpg",
  "local_features": ["text_region", "low_glare"]
}
~~~

Observation 不等于原始 Frame：

- Frame 是高频采集数据。
- Observation 是低频任务事件。
- Frame 可能被覆盖或丢弃。
- Observation 必须可以追踪来源。

### 6.3 Evidence：结构化证据

Evidence 表示支持某个字段判断的信息。它不能只保存 value，还必须保存：

- 属于哪个目标。
- 说明哪个字段。
- 来源是什么。
- 来源对象是谁。
- 置信度是多少。
- 当前是 confirmed、probable、unknown 还是 conflict。

~~~json
{
  "evidence_id": "ev-label-power",
  "target_id": "charger-01",
  "field": "maximum_output_power_w",
  "value": 65,
  "source_type": "calculated_value",
  "source_id": "power_calculator_v1",
  "confidence": 0.99,
  "status": "confirmed",
  "derived_from": ["ev-obs-back-label-01-profiles"]
}
~~~

“65W”不是从天而降的事实。完整链路应是：`Observation -> 原始 OCR 文本 Evidence -> 解析后的 power_profiles Evidence -> maximum_output_power_w Evidence`。`source_id` 记录直接生成当前证据的观察或工具，`derived_from` 记录父证据 ID。这样既能回到原始 Observation，也能知道是哪一版解析器或计算器产生了中间结果。

### 6.4 Belief State：当前认知状态

Belief State 是系统对目标的结构化认知，不是一段聊天历史。

它至少包含：

- confirmed：已有可靠证据支持。
- probable：可能正确，但需要验证。
- unknown：当前缺失。
- conflicts：不同来源互相冲突。
- observed_views：已经观察过的视角。

如果 OCR 读出 65W，而规格库给出 45W，系统不能简单选择置信度更高的一条然后继续。它应该记录 conflicts，并请求重新观察、核对型号或降低结论确定性。

四种字段状态必须互斥。第一章代码使用下面的状态转移规则：

| 当前状态 + 新证据 | 新状态 | 处理方式 |
|---|---|---|
| confirmed + 同值 confirmed | confirmed | 保留字段结论，合并两个来源到 ledger |
| confirmed + 异值 confirmed | conflict | 移出 confirmed，保留双方证据 |
| confirmed + unknown | confirmed | unknown 记录进 ledger，但不降低已确认结论 |
| probable + confirmed | confirmed | confirmed 覆盖候选状态 |
| 任意非冲突 + 显式 conflict | conflict | 进入冲突集合 |
| conflict + 新证据 | conflict | 继续记录，禁止自动解除，等待显式复核 |

`confirmed`、`probable`、`unknown` 和 `conflicts` 不能同时包含同一字段。证据本身则全部保留在 ledger 中，字段状态变化不等于删除历史。

### 6.5 Action：下一步机器动作

Action 是由证据缺口产生的下一步动作。它既要有人类可读说明，也要有机器可执行字段。

~~~json
{
  "action_type": "request_view",
  "target_id": "charger-01",
  "view_type": "back_label",
  "required_features": [
    "maximum_output_power_w",
    "supported_protocol"
  ],
  "instruction": "请将充电器翻到有输出参数文字的一面，减少反光并保持稳定。",
  "reason": "需要从背面标签确认最大输出功率和充电协议。"
}
~~~

Action 可以是：

- request_view：向 C++ 发出观察请求。
- ask_user：询问用户型号或偏好。
- retrieve_knowledge：查询设备资料。
- run_rules：执行确定性规则。
- generate_answer：生成最终回答。

### 6.6 五个概念如何形成闭环

~~~mermaid
flowchart LR
    R["Reality Object"] --> O["Observation"]
    O --> E["Evidence"]
    E --> B["Belief State"]
    B --> A["Action"]
    A -->|请求新视角| O
    A -->|信息充分| D["规则与回答"]
~~~

这五个概念分别回答：

- 我们在看谁？
- 这次看到了什么？
- 它能证明什么？
- 当前还相信什么、缺什么？
- 下一步做什么？

### 6.7 先运行一次逻辑切片

理解五个概念后，可以先在项目根目录运行：

~~~powershell
python examples/ch01_evidence_loop.py
~~~

你会先看到 `request_view(back_label)` 和 `ask_user`，随后看到模拟标签形成三段派生证据，最后得到 `meets_mvp_charging_requirements`。此处的“补齐”由脚本直接注入，不是系统真的暂停并等待用户。第 11 节会逐步拆解输出，第 5～6 章才实现检查点式恢复。

---

## 7. 被动视觉、主动观察和信息增益

### 7.1 被动视觉

被动视觉只处理当前获得的画面：

~~~text
输入图像 -> 检测/OCR/描述 -> 输出结果
~~~

它可以回答“当前画面里看到了什么”，但不会围绕任务主动寻找缺失信息。

### 7.2 主动观察

主动观察从目标反推所需证据：

~~~text
用户目标
-> 必要证据
-> 当前证据缺口
-> 最有价值的下一次观察
-> 更新认知
~~~

在 USB-C 案例中，系统不是随意要求“再拍一张”，而是明确请求 back_label，并说明需要 maximum_output_power_w 和 supported_protocol。

### 7.3 Agent 不控制每一帧

Agent 适合低频决策，不适合高频视频处理。如果让 Agent 决定每一帧是否保留，会带来：

- 模型调用成本失控。
- 网络延迟进入实时环路。
- 帧队列堆积。
- 系统行为难以预测。
- 摄像头异常无法及时恢复。

正确分工是：

~~~text
Agent：我需要背面标签，要求文字清晰且低反光。
C++：持续读取帧，评估质量，满足条件后返回 Observation。
~~~

### 7.4 信息增益

信息增益可以简单理解为：一次新观察能够减少多少关键未知。

当前缺少功率和协议时：

- 再拍一张相同正面图，信息增益很低。
- 拍背面标签，可能同时获得功率、协议和型号，信息增益很高。
- 拍包装颜色，通常与兼容性无关。

第一版不需要实现复杂数学优化，只需使用以下优先级：

1. 优先请求能够补齐多个必要字段的视角。
2. 优先解决阻止规则执行的关键未知。
3. 不重复请求已经清晰观察过的相同视角。
4. 达到最大尝试次数后停止，并明确输出未知。

---

## 8. USB-C 兼容性的完整证据循环

### 8.1 建立任务会话

每次任务都需要稳定 `session_id`。在本课程 MVP 中同时令 `thread_id == session_id`，它用于关联：

- 用户问题。
- 当前目标。
- LangGraph 检查点。
- Observation 和 Evidence。
- WebSocket 事件。
- 会话文件目录。

~~~json
{
  "session_id": "session-ch01",
  "thread_id": "session-ch01",
  "intent": "compatibility_check",
  "target_id": "charger-01",
  "status": "running"
}
~~~

第 1 章的数据类会强制这一相等关系。真正把 `thread_id` 传给 LangGraph checkpointer 是第 5～6 章的内容。

### 8.2 锁定现实目标

第一版可以让用户点击目标，或者默认选择画面中心最大的移动物体。后续所有观察都必须绑定同一 target_id。

如果跟踪丢失，系统不能把新出现的商品自动当成原目标。它应暂停或要求重新选择。

### 8.3 生成证据计划

MVP 的必要证据字段为：

- source_port_type
- back_label_ocr_text
- power_profiles
- maximum_output_power_w
- supported_protocol
- target_device_model
- target_device_port_type
- target_device_minimum_power_w
- target_device_required_voltage_v
- target_device_protocol

其中 `target_device_model` 是检索资料的入口。`maximum_output_power_w` 用于摘要和审计，但最终规则不能只比较最大值，还必须在 `power_profiles` 中确认设备所需电压档位存在且该档位功率达到阈值。

### 8.4 获取初始观察

正面观察可能确认：

- 当前目标大致是充电器。
- 输出接口外形为 USB-C。

但下面字段仍然未知：

- 最大输出功率。
- 支持协议。
- 笔记本完整型号。
- 笔记本接口、协议和最低功率。

此时不能运行最终规则。

### 8.5 规划补充动作

系统可以同时生成两个独立动作：

1. request_view(back_label)：请求背面标签，补充功率和协议。
2. ask_user(target_device_model)：询问完整笔记本型号。

两个动作来自不同信息源，可以并行推进。用户翻转充电器时，系统也可以等待用户输入型号。

### 8.6 C++ 完成观察请求

C++ 收到 ObservationRequest 后继续处理视频，并检查：

- target_id 是否仍然可见。
- 目标是否已经翻转。
- 文字区域是否出现。
- 清晰度是否达标。
- 反光是否过强。
- 目标占画面比例是否足够。

条件满足后才生成 back_label Observation。它不会把等待期间的每一帧发送给 Python。

### 8.7 OCR、参数解析和功率计算

标签可能包含：

~~~text
5V/3A
9V/3A
15V/3A
20V/3.25A
~~~

程序解析为：

| 电压 | 电流 | 功率 |
|---:|---:|---:|
| 5V | 3A | 15W |
| 9V | 3A | 27W |
| 15V | 3A | 45W |
| 20V | 3.25A | 65W |

最大输出功率为 65W。这个值是程序计算结果，不是模型凭文字生成的结论。代码会把原始 OCR 文本、完整档位列表和最大值分别存为 Evidence，并用 `derived_from` 串联，避免解析后只剩一个失去来源的数字。

如果 OCR 只识别出电压电流，却没有可靠识别协议标识，supported_protocol 应保持 unknown。系统不应因为接口是 USB-C 就自动填入 USB-PD。

### 8.8 查询目标设备规格

用户只说“我的笔记本”时，系统应询问完整型号。即使用户说“ThinkPad T14”，也可能存在不同代际和配置，不能随意选择一个版本。

规格证据应记录：

- 完整型号。
- 文档或数据库来源。
- 文档版本。
- 支持的充电接口和协议。
- 设备所需的电压档位。
- 官方电源适配器或项目定义的最低功率要求。

本章 Demo 使用虚构 DemoBook 14 Gen 2 和 45W 最低要求，避免把示例数据误当成真实商品规格。

### 8.9 确定性兼容规则

本项目的简化规则名为 `meets_mvp_charging_requirements_v1`：

~~~text
接口兼容
AND 协议兼容
AND 存在设备要求的电压档位
AND 该档位功率 >= 项目定义的设备最低功率
=> 当前证据满足课程 MVP 充电要求
~~~

规则结果包含：

~~~json
{
  "rule_name": "meets_mvp_charging_requirements_v1",
  "decision": "meets_mvp_charging_requirements",
  "meets_mvp_charging_requirements": true,
  "port_status": "compatible",
  "protocol_status": "compatible",
  "power_status": "sufficient"
}
~~~

这是课程 MVP 的工程规则，不是对所有 USB-C/USB-PD 设备行为的完整电气规范。“满足 MVP”也不等于获得 USB-IF 认证或现实使用必然安全。第一版仍未验证：标签功率是单端口还是多端口总功率、端口是输入还是输出、线缆额定能力、PPS 与完整 PDO 协商、多端口同时使用时的降额、真伪和内部质量。低于原装适配器功率的电源有时仍可能慢速充电，有些设备则会拒绝充电；课程为了可测试性采用明确阈值，并在回答中说明范围。

### 8.10 中断与恢复

如果缺少背面标签，目标系统中的 LangGraph 工作流应保存状态并暂停。用户完成翻转后，使用同一 `thread_id` 恢复。

~~~text
运行到 VERIFY_EVIDENCE
-> 发现 maximum_output_power_w 缺失
-> interrupt，输出 ObservationRequest
-> 保存检查点
-> 用户翻转商品
-> C++ 返回 back_label Observation
-> 使用相同 thread_id 恢复
-> 从证据验证继续
~~~

恢复不是重新从头问一遍。之前确认的 target_id、接口证据和用户问题都应继续存在。

这一小节定义的是后续实现契约，不是本章 Demo 已完成的功能。本章脚本会在一个 Python 进程里直接注入 Observation 和设备资料，没有 checkpointer、序列化重载或真实等待。第 5 章实现 interrupt/恢复，第 6 章实现持久化后，才允许把“可恢复”列为已通过能力。

### 8.11 证据化回答

最终回答应包含：

- 结论。
- 观察事实。
- OCR 或标签证据。
- 程序计算。
- 设备资料。
- 规则判断。
- 未知边界。

示例：

> 根据课程 MVP 的接口、协议、所需电压档位和功率阈值规则，当前证据支持满足充电要求。充电器与设备接口均为 USB-C，标签文字证据显示 USB-PD，解析档位包含 20V/3.25A（65W），设备资料要求 20V 且至少 45W。回答同时列出 Observation ID、解析器/规格来源和父证据 ID。该结论不验证端口功率分配、线缆能力、PPS 协商、真伪、内部质量或长期可靠性。

---

## 9. 系统能力边界

### 9.1 第一版能够完成

- 在单摄像头中管理一个用户关注目标。
- 从实时视频或预录视频中选择有效观察。
- 评估基础图像质量。
- 请求正面、接口特写和背面标签视角。
- 从标签文字中解析部分输出档位。
- 查询已接入的小型设备规格库。
- 使用确定性程序计算功率和兼容结果。
- 在证据不足或冲突时拒绝确定结论。
- 输出证据来源和未知边界。

### 9.2 第一版不能保证

- 识别所有现实物体。
- 判断商品真伪。
- 判断内部材料、做工和安全质量。
- 替代电气安全检测。
- 从画面或知识库中不存在的信息推断事实。
- 在型号不完整时自动选择某个设备版本。
- 在关键证据缺失时给出绝对结论。
- 同时稳定管理大量相似商品。
- 控制机器人主动移动摄像头。

### 9.3 为什么不能把所有任务交给大模型

适合模型的任务：

- 理解开放式用户表达。
- 描述可见特征。
- 生成候选解释。
- 从非结构化资料中提取候选字段。
- 组织人类可读回答。

更适合程序的任务：

- 20V × 3.25A。
- 比较 65W 是否达到 45W。
- 校验必填字段。
- 检查证据来源和冲突。
- 管理超时、重试和调用次数。
- 摄像头帧队列和资源释放。

系统原则是：

> 开放理解交给模型，确定计算交给程序，高频感知交给 C++，任务编排交给状态图和受约束 Agent。

### 9.4 不把 Probable 当作 Confirmed

视觉模型认为“这可能是 65W 充电器”，只能进入 probable。只有标签、规格资料或可验证计算支持时，maximum_output_power_w 才能进入 confirmed。

置信度不是事实的替代品。0.95 的错误预测仍然是错误预测，来源与验证关系比单个浮点数更重要。

---

## 10. 技术栈地图

本节只建立位置感，不要求立即安装全部技术。

| 技术 | 在 RealSight 中的职责 | 为什么选择 | 正式实现章节 |
|---|---|---|---|
| C++20 | 摄像头、帧队列、质量和关键帧 | 资源控制、实时性、RAII | 第 9～10 章 |
| OpenCV | 摄像头与基础图像处理 | 成熟的跨平台视觉基础库 | 第 9～10 章 |
| Python 3.12 | Agent、工具和服务端 | AI 生态完整、教学稳定 | 全程 |
| dataclasses | 本章领域模型 | 标准库、无依赖、易理解 | 第 1～3 章 |
| Pydantic | 服务边界数据校验 | 明确输入输出与错误 | 第 4 章 |
| LangGraph | 状态图、检查点、中断恢复 | 适合可恢复的长流程 | 第 4～6、13 章 |
| 最小 Tool Calling 循环 | 模型决策、工具执行、事件轨迹 | 先看懂 Agent 基本协议 | 第 2～3 章 |
| DeepAgents（候选） | 上下文隔离、文件后端和专业子 Agent | 复杂任务需要时再引入 | 第 13 章评估 |
| gRPC/Protobuf | C++ 与 Python 的强类型通信 | 流式、超时、取消、契约 | 第 4、10 章 |
| FastAPI | 浏览器任务接口 | Python 异步 Web 服务 | 第 14 章 |
| WebSocket | 实时进度和最终事件 | 长任务不阻塞普通 HTTP | 第 14 章 |
| SQLite | 本地检查点和元数据 | 单机 MVP 简单可恢复 | 第 6 章 |
| unittest | 学习阶段逻辑测试 | Python 标准库即可运行 | 第 1～7 章 |

### 10.1 当前暂定与暂不选定

企业工程中的“暂未决定”也应写入设计，而不是藏在开发者脑中：

| 事项 | 一个月 MVP 决策 | 后续替换边界 |
|---|---|---|
| 大模型提供商 | provider-agnostic；第 2 章先用确定性模型替身 | 通过 ModelAdapter 接入任意支持结构化 Tool Calling 的模型 |
| OCR | 第 1～2 章直接注入文本；第 11 章再比较开源 OCR 与多模态抽取 | Observation 到 Evidence 的接口不变 |
| 设备资料 | 先使用带版本号的本地 JSON/测试数据 | 第 12 章可替换为说明书检索或受控网络来源 |
| 图像引用 | 同机 MVP 暂用本地路径 | 跨机器部署时改为 artifact ID/对象存储，不在协议里传播宿主机路径 |
| DeepAgents | 暂不引入 | 第 13 章按任务复杂度和上下文隔离收益决定 |

### 10.2 通信为什么采用混合方案

浏览器与 Python：

- HTTP/JSON：提交任务、上传文件、查询结果。
- WebSocket：接收长任务进度、观察请求、中断和最终回答。

Python 与 C++：

- gRPC/Protobuf：发送 ObservationRequest，接收 Observation 和运行时事件。

混合方案不是为了追求技术数量，而是因为两条通信链路的需求不同。浏览器更适合 Web 协议；C++ 与 Python 需要稳定类型和双向流式控制。

### 10.3 为什么第一章不用这些框架

如果一开始就安装 LangGraph、DeepAgents、gRPC、OpenCV 和数据库，你很容易把“依赖安装成功”误认为“理解了系统”。

第一章只用标准库验证：

- 数据对象是否清晰。
- 缺失证据是否能被发现。
- 下一步动作是否可解释。
- 规则是否可测试。
- 冲突是否能阻止结论。

后续框架只是承载这些已经明确的业务语义。

---

## 11. 可运行 Demo

### 11.1 文件

- Demo：../../examples/ch01_evidence_loop.py
- 共享领域模型：../../examples/realsight_domain.py
- 测试：../../tests/test_ch01_evidence_loop.py

Demo 使用八个后续章节保持稳定的类型：

- TaskSession
- RealityObject
- ObservationRequest
- Observation
- Evidence
- BeliefState
- Action
- RunEvent

此外定义 `PowerProfile` 和 `CompatibilityResult` 表示解析档位与确定性规则输出。稳定领域类型放在共享模块中，使第 2 章可以复用，而不是从第一章脚本反向导入。

### 11.2 运行环境

使用 Python 3.12：

~~~powershell
python --version
python examples/ch01_evidence_loop.py
~~~

如果系统默认 Python 不是 3.12，应使用项目指定的 3.12 解释器。第 8 章会正式建立 uv 项目和锁定环境。

### 11.3 Demo 执行过程

第一步只加入一条 confirmed Evidence：

~~~text
source_port_type = USB-C
~~~

系统检查必要字段后，发现缺少：

- back_label_ocr_text
- power_profiles
- maximum_output_power_w
- supported_protocol
- target_device_model
- target_device_port_type
- target_device_minimum_power_w
- target_device_required_voltage_v
- target_device_protocol

随后生成两个动作：

~~~text
request_view(back_label)
ask_user(target_device_model)
~~~

Demo 再模拟一个合格的背面标签 Observation，解析：

~~~text
USB Power Delivery Output:
5V/3A, 9V/3A, 15V/3A, 20V/3.25A
~~~

程序依次保存原始 OCR、解析档位和 65W 最大值三层 Evidence，并模拟规格库返回：

~~~text
设备接口 = USB-C
设备协议 = USB-PD
设备所需电压 = 20V
设备最低功率 = 45W
~~~

证据齐全后，下一动作变为 `run_rules`，规则输出 `meets_mvp_charging_requirements`。规则实际检查 20V 档位的功率，而不是只看任意档位中的最大瓦数。

### 11.4 为什么 Demo 不调用大模型

这个 Demo 要验证的不是语言理解，而是业务骨架：

- Agent 输出必须落到 Action。
- 观察结果必须转换为 Evidence。
- Belief State 必须区分未知和冲突。
- 规则必须拒绝不完整输入。
- 最终回答必须说明来源和边界。

这个定位必须说得准确：脚本展示的是证据规划与规则门控，不是中断恢复。第 2 章会在其外层加入最小 Agent Tool Calling 循环和 `RunEvent` 事件流，但不会改变这些领域含义。

### 11.5 运行测试

~~~powershell
python -m unittest discover -s tests -v
~~~

本章共有 16 项测试，覆盖：

1. 缺少标签和设备型号时生成正确动作。
2. 原始 OCR、档位和最大功率的派生关系。
3. 不同 `target_id` 的 Observation 被拒绝且不修改状态。
4. 标签没有协议标识时保持 unknown。
5. 20V/3.25A 对 20V、45W 要求通过 MVP 规则。
6. 20V/1.5A 对 45W 要求不通过。
7. 最大 65W 但缺少 20V 档位时不通过。
8. 端口不匹配和协议不匹配。
9. 证据冲突阻止结论。
10. `thread_id == session_id` 约束。
11. confirmed/unknown、同值合并、probable 晋级、显式 conflict 和冲突保持等状态转移。

### 11.6 阅读代码时关注什么

不要急着记住所有 dataclass 字段。先沿着主线阅读：

1. build_demo_belief 建立初始证据。
2. missing_evidence_fields 找缺口。
3. plan_next_actions 把缺口变成动作。
4. add_label_evidence 把标签变成带父子关系的证据链。
5. evaluate_compatibility 运行确定性规则。
6. build_evidence_answer 生成证据化回答。

你应当能够回答：如果 supported_protocol 不存在，程序为什么不输出 incompatible，而是 insufficient_evidence？因为“没有证据证明兼容”和“已有证据证明不兼容”是两个不同状态。

---

## 12. 本章设计产物与课后练习

### 12.1 主动感知业务流程

~~~text
用户提出现实问题
-> 建立 TaskSession
-> 锁定 Reality Object
-> 生成必要 Evidence 字段
-> 检查 Belief State
-> 缺少视觉证据：生成 ObservationRequest
-> 缺少用户信息：生成 ask_user
-> C++ 返回 Observation
-> 工具生成 Evidence
-> 查询规格资料
-> 更新 Belief State
-> 检查 unknown 和 conflicts
-> 证据充分：运行确定性规则
-> 输出结论、依据和未知边界
~~~

### 12.2 基础证据类型

第一版至少使用：

- visible_feature：直接可见接口或结构。
- observation_ocr：从某个 Observation 得到的原始标签文字。
- parsed_value：从原始文字解析出的协议或功率档位。
- manual_spec：说明书或设备规格。
- user_provided：用户提供的型号。
- calculated_value：程序计算值。
- rule_result：确定性规则结果。
- unknown：当前无法获得。
- conflict：来源互相冲突。

### 12.3 USB-C 证据表

| 证据字段 | 是否必要 | 获取方式 | 缺失时动作 |
|---|---:|---|---|
| 商品类别 | 辅助 | 视觉观察 | 请求正面视角 |
| 输出接口 | 是 | 接口特写 | request_view(port_closeup) |
| 原始标签文字 | 是 | 标签 OCR | request_view(back_label) |
| 完整输出档位 | 是 | 标签解析 | 重新观察或核对解析 |
| 最大输出功率 | 审计/摘要 | 从档位计算 | 重新观察或核对解析 |
| 支持协议 | 是 | 标签、说明书、规格库 | 重新观察或检索 |
| 设备完整型号 | 检索前提 | 用户输入 | ask_user |
| 设备接口 | 是 | 官方规格 | retrieve_knowledge |
| 设备最低功率 | 是 | 官方规格 | retrieve_knowledge |
| 设备所需电压档位 | 是 | 官方规格 | retrieve_knowledge |
| 商品真伪 | 不支持 | 当前系统无法验证 | 明确边界 |

### 12.4 USB-C MVP 可执行验收表

| 场景 | 预期动作或结果 | 本章状态 |
|---|---|---|
| 初始只有接口证据 | 必须 `request_view(back_label)` | 自动化测试通过 |
| 缺少设备完整型号 | 必须 `ask_user` | 自动化测试通过 |
| 标签协议未知 | `insufficient_evidence`，不能判不兼容 | 自动化测试通过 |
| 20V/3.25A（65W），设备需 20V/45W | `meets_mvp_charging_requirements` | 自动化测试通过 |
| 20V/1.5A（30W），设备需 20V/45W | `does_not_meet_mvp_charging_requirements` | 自动化测试通过 |
| 最大 65W，但没有设备所需 20V 档 | 不满足 MVP 规则 | 自动化测试通过 |
| 接口或协议不匹配 | 不满足 MVP 规则，并指出失败维度 | 自动化测试通过 |
| 关键证据冲突 | `insufficient_evidence`，禁止确定结论 | 自动化测试通过 |
| Observation 属于另一个 target_id | 拒绝写入，Belief State 不变化 | 自动化测试通过 |
| 中断恢复后保留 target_id 与接口证据 | 相同 thread 加载原检查点 | 第 5～6 章待实现，不能在本章标为通过 |

### 12.5 课后练习

练习一：把 Demo 中充电器档位改为 20V/1.5A，解释为什么结果是 `does_not_meet_mvp_charging_requirements`，而不是 `insufficient_evidence`。

练习二：删除标签中的 USB Power Delivery 字样，观察 supported_protocol 为什么保持 unknown。

练习三：加入第二组不同的 `power_profiles` confirmed Evidence，观察冲突如何阻止规则执行。

练习四：为 Action 增加 timeout_seconds 字段。思考这个字段属于任务语义还是通信实现。

练习五：设计“接口特写”的 ObservationRequest，列出 required_features 和质量条件。

练习六：解释为什么 TaskSession、RunEvent 不属于原始五个现实感知概念，却仍然是工程系统需要的稳定类型。

练习七：用自己的话回答：

> 为什么 RealSight 不是“让大模型多看几张图”，而是“管理现实状态和证据缺口”？

---

## 13. 本章小结与下一章桥接

本章从一个 USB-C 兼容问题出发，建立了 RealSight 的核心定位：它不是单图识别系统，而是一套持续感知、证据规划、主动观察和确定性验证系统。

我们明确了四层责任：

- C++ 负责高频、连续、资源敏感的现实感知。
- LangGraph 负责确定流程、检查点、中断与恢复。
- Agent 和子 Agent 负责开放理解、规划与专业信息整理。
- 工具和规则负责确定计算、校验和可复现结论。

我们建立了 Reality Object、Observation、Evidence、Belief State 和 Action 五个核心概念，并补充了 TaskSession 与 RunEvent 两个工程类型。随后通过纯 Python 逻辑切片验证：系统能够隔离 target、发现证据缺口、生成新视角和用户询问动作、保存 OCR 到计算值的证据派生链、检查所需电压档位，并在证据冲突时拒绝下结论。它尚未验证 LangGraph 中断恢复，这项能力明确留在第 5～6 章。

第一章回答的是：

> RealSight 要解决什么问题，它为什么需要这些组件？

第 2 章将回答：

> 一个最小 Agent 执行过程中，模型决策、工具调用、事件流和最终回答分别长什么样？

下一章会继续使用共享模块中的 TaskSession、Action 和 RunEvent，先使用确定性 ModelAdapter 和模拟工具，手写最小 Tool Calling 循环，观察 `模型决策 -> 工具请求 -> 工具结果 -> 再次决策 -> 最终回答` 的完整事件轨迹。它不会提前引入 DeepAgents、C++ 摄像头或 gRPC：先看懂基础协议，再逐层替换实现。

---

## 参考资料

- 原始课程素材：《章节1.docx》，RealSight 第一章。
- [DeepAgents 官方概览](https://docs.langchain.com/oss/python/deepagents/overview)
- [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [gRPC C++ Basics](https://grpc.io/docs/languages/cpp/basics/)
- [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/)
- [USB-IF：USB Charger (USB Power Delivery)](https://www.usb.org/usb-charger-pd)
- [深度研搜：DeepAgents 基础与核心概念](https://github.com/didilili/ai-agents-from-zero/blob/main/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/1-DeepAgents%E5%9F%BA%E7%A1%80%E4%B8%8E%E6%A0%B8%E5%BF%83%E6%A6%82%E5%BF%B5.md)
