# 第 3 章 异步任务与分工边界：让 Agent 等待服务而不阻塞系统

> 本章关键词：asyncio、coroutine、Task、async iterator、timeout、cancellation、TaskGroup、backpressure、服务与子 Agent

第二章的最小 Agent 已经能完成 Tool Calling 循环，但它的模型和工具都是同步调用：运行时调用一个函数，必须等函数返回后才能继续。现实中的 RealSight 不会这么顺利。模型 API 可能等待网络，资料检索可能等待数据库，ObservationRequest 可能等待用户翻转充电器和 C++ 运行时筛选清晰帧。

如果把这些等待都写成阻塞调用，一个任务等待摄像头时，其他任务、取消请求和进度事件也可能无法及时处理。第三章要解决的不是“让一个摄像头观察更快”，而是：

> 当多个任务都在等待外部 I/O 时，让事件循环有机会推进其他工作；当等待超过边界或用户取消时，及时清理子任务并给出明确终态。

本章还要处理一个架构误区：一个组件耗时、异步或独立运行，不代表它就是子 Agent。C++ 感知运行时没有自己的语言模型、任务推理和独立上下文，它是一个长期运行的服务。专业视觉子 Agent 则可能基于 Observation 做多步语义分析。两者都可能被主工作流调用，但职责和生命周期完全不同。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 解释 coroutine、Task 和 event loop 的基本关系。
2. 说明异步并发为什么适合 I/O 等待，以及为什么不会自动加速 CPU 密集计算。
3. 把第二章的同步 `stream/invoke` 演进为 `astream/ainvoke`。
4. 使用有界 `asyncio.Queue` 把工具内部进度传给 RunEvent 消费者。
5. 使用 `asyncio.timeout()` 为单个异步工具设置截止边界。
6. 区分协作式用户取消与外部 `Task.cancel()`。
7. 在清理资源后正确重新传播 `CancelledError`。
8. 使用 `asyncio.TaskGroup` 并发推进独立任务。
9. 判断能力应实现为普通工具、长期服务、工作流节点还是子 Agent。
10. 运行并测试模拟感知服务的正常、并发、超时和取消路径。

### 1.2 学习前提

需要掌握前两章的：

- TaskSession、Belief State、ObservationRequest 和 Observation。
- ToolCall 是请求，ToolRegistry 才真正执行工具。
- call_id 关联工具请求与结果。
- RunEvent 是框架无关的业务事件投影。
- target_id 必须在会话、状态、工具和观察之间保持一致。

不要求线程、进程、gRPC、LangGraph 或 DeepAgents 经验。

### 1.3 本章产物

- `AsyncModelAdapter` 与 `AsyncToolRegistry`。
- 支持超时、取消和进度流的 `AsyncMinimalAgent`。
- 一个明确不是 Agent 的 `SimulatedPerceptionService`。
- SERVICE_REQUESTED、SERVICE_PROGRESS、SERVICE_COMPLETED、TASK_TIMED_OUT 和 TASK_CANCELLED 事件。
- 一个展示正常事件流、两个任务并发和服务超时的小 Demo。
- 13 项第三章异步测试；全项目合计 41 项。

---

## 2. 同步版本会在哪里停住

第二章同步运行时的核心形态是：

~~~python
decision = model.decide(...)
result = registry.execute(call, context)
~~~

如果 `execute()` 内部等待 5 秒：

~~~text
进入工具
-> 当前执行线程等待 5 秒
-> 工具返回
-> 才能产生下一个事件
~~~

命令行一次只跑一个任务时，这可能看不出问题。但到第 14 章的 Web 服务中，多个用户会同时提交任务；C++ 感知运行时也会持续返回状态。阻塞式等待会带来：

- 其他会话不能及时推进。
- WebSocket 心跳和进度更新延迟。
- 用户点击取消后迟迟没有响应。
- 一个慢网络请求占住工作线程。
- 并发量只能靠不断增加线程勉强维持。

### 2.1 异步解决的是等待调度

异步函数在遇到可等待操作时使用 `await` 暂时让出控制权：

~~~python
await asyncio.sleep(0.1)
await model_client.request(...)
await grpc_stub.Observe(...)
~~~

事件循环可以在这段等待期间推进其他就绪任务。异步不会把 100 毫秒网络延迟变成 20 毫秒，也不会让模型生成速度变快；它改善的是多个等待任务共同存在时的响应性和吞吐。

### 2.2 异步不等于 CPU 并行

下面这种代码即使写在 `async def` 中，也会阻塞事件循环：

~~~python
async def bad_cpu_work():
    return very_expensive_image_algorithm()
~~~

函数内部没有可让出控制权的 `await`，CPU 仍在当前线程连续工作。RealSight 的高频图像处理将放在 C++ 运行时；少量 Python CPU 任务可以以后使用线程池、进程池或原生扩展，但不能仅靠加一个 `async` 关键字解决。

### 2.3 异步也不等于多线程

本章 Demo 使用单线程事件循环中的协作式并发。任务只有在 `await` 处让出执行权，其他任务才能运行。线程由操作系统抢占调度；协程由代码在可等待点协作调度。二者可以组合，但概念不能混用。

---

## 3. coroutine、Task 与 event loop

### 3.1 调用 async 函数得到 coroutine

~~~python
coroutine = agent.ainvoke(...)
~~~

这里通常还没有完成 Agent 运行，只创建了一个 coroutine 对象。它需要被 `await`，或交给 `asyncio.create_task()`/TaskGroup 才会由事件循环推进。

### 3.2 Task 是被调度的 coroutine

~~~python
task = asyncio.create_task(service.observe(request))
result = await task
~~~

Task 持有运行状态、结果、异常和取消状态。第三章运行时把异步工具包装成 Task，因为它需要同时等待：

- 工具是否完成。
- 工具是否产生进度。
- 用户是否请求取消。
- 工具是否超过超时边界。

### 3.3 event loop 负责推进就绪任务

`asyncio.run(main())` 创建事件循环、运行入口 coroutine，并在结束时完成异步生成器和执行器清理。应用通常只在最外层调用一次 `asyncio.run()`，不要在已经运行的异步函数中嵌套调用。

### 3.4 async iterator 对应事件流

第二章使用普通 generator：

~~~python
for event in agent.stream(...):
    ...
~~~

第三章使用 async generator：

~~~python
async for event in agent.astream(...):
    ...
~~~

消费者每次请求下一项时，生成器可以等待模型、工具、队列或取消信号。RunEvent 的业务含义保持不变，只是生产方式从同步迭代变为异步迭代。

---

## 4. 工具、服务、子 Agent 与工作流节点

参考的深度研搜第 3 章重点讲子智能体和异步执行。这个方法对“网络研究助手、数据库分析助手、知识库研究助手”等多步语义任务很有价值，但不能机械套到感知运行时。

### 4.1 四类组件的选择表

| 组件 | 核心特征 | RealSight 示例 | 是否通常调用模型 |
|---|---|---|---:|
| 普通工具 | 一次确定操作，输入输出清楚 | 功率计算、字段校验 | 否 |
| 长期服务 | 独立生命周期、设备或网络 I/O、可被多次请求 | C++ 摄像头与跟踪运行时 | 否 |
| 工作流节点 | 更新显式状态并决定确定路径 | Observation 接收、证据验证 | 不一定 |
| 子 Agent | 独立提示词、上下文、工具和多步推理 | 视觉证据分析、复杂资料研究 | 是 |

### 4.2 C++ 感知运行时为什么不是子 Agent

C++ 运行时负责：

- 打开和维护摄像头。
- 读取高频视频帧。
- 管理线程、缓冲区、背压和资源释放。
- 跟踪 target_id。
- 根据 ObservationRequest 选择满足质量条件的帧。
- 返回 Observation 与性能事件。

它没有自由规划、独立对话上下文或自然语言推理。把它包装成子 Agent 不会增加能力，只会掩盖设备生命周期、实时队列和错误恢复责任。

### 4.3 视觉证据子 Agent 与感知服务的区别

未来视觉证据子 Agent 可能接收 C++ 已筛选的 Observation，然后：

- 判断应调用 OCR 还是多模态模型。
- 对多个候选文本做语义核对。
- 识别字段冲突并建议重新观察。
- 返回结构化 Evidence。

这段工作包含开放语义、多步工具选择和独立上下文，才可能值得使用子 Agent。数据路径是：

~~~text
C++ 感知服务
-> Observation
-> LangGraph Observation 接收节点
-> 视觉 Evidence 工具或子 Agent
-> Evidence Ledger
~~~

服务不直接调用子 Agent，所有路由仍由工作流掌握。

### 4.4 子 Agent 也不等于 Python async

Python `async/await` 是程序执行机制；子 Agent 是任务分工和上下文隔离方式。同步子 Agent、异步子 Agent、同步普通工具和异步服务都可能存在。官方文档也特别区分异步子 Agent 与 Python 语法层面的 async 概念。

当前 DeepAgents 的异步子 Agent 还是预览能力，适合独立、长时间、可中途调整或取消的 Agent 任务。本章不依赖该预览 API，因为我们要模拟的是确定性感知服务，而不是后台研究 Agent。

---

## 5. 第三章异步架构

~~~mermaid
sequenceDiagram
    participant U as 用户任务
    participant A as AsyncMinimalAgent
    participant M as AsyncModelAdapter
    participant T as AsyncToolRegistry
    participant Q as 有界进度队列
    participant S as 感知服务

    U->>A: astream(session, belief)
    A->>M: await decide()
    M-->>A: ToolCall(inspect_evidence_gaps)
    A->>T: create_task(execute)
    T-->>A: ToolResult(actions)
    A->>M: await decide(tool_results)
    M-->>A: ToolCall(request_observation)
    A->>T: create_task(execute)
    T->>S: await observe(request)
    S->>Q: SERVICE_REQUESTED
    S->>Q: SERVICE_PROGRESS
    S->>Q: SERVICE_COMPLETED
    Q-->>A: RunEvent 投影
    S-->>T: Observation
    T-->>A: ToolResult
    A->>M: await decide(tool_results)
    M-->>A: FinalAnswer
~~~

运行时同时关注工具 Task、进度 Queue 和取消 Event。任何一条路径到达终态，都必须正确收束其余等待对象。

---

## 6. 本章新增接口

### 6.1 AsyncModelAdapter

~~~python
class AsyncModelAdapter(Protocol):
    async def decide(...) -> ModelDecision:
        ...
~~~

它继续返回第二章的 ModelDecision，不另造一套决策模型。真实云模型适配器将使用异步客户端；本章的 `AsyncPerceptionModelAdapter` 仍是确定性测试替身。

### 6.2 AsyncToolSpec

异步工具在名称、说明和必填参数之外增加：

~~~python
timeout_seconds: float
handler: AsyncToolHandler
~~~

超时属于工具执行策略，不应该让模型决定。模型可以提出调用，但运行时控制最多允许等待多久。

### 6.3 AsyncToolContext

上下文保存可信的 TaskSession、Belief State 和服务引用。模型只传结构化参数，不能自行构造感知服务对象。

当前 `services: dict[str, Any]` 仍是教学阶段的宽松写法。第 4 章会用正式 schema 和协议边界替换这类 Any，质量报告也不会把它称为最终企业契约。

### 6.4 ProgressUpdate

异步工具不应直接创建 RunEvent sequence，因为事件顺序由当前 Agent run 统一拥有。工具只发出 ProgressUpdate：

- SERVICE_REQUESTED。
- SERVICE_PROGRESS。
- SERVICE_COMPLETED。

Agent 从有界队列读取更新，补入 `session_id`、`sequence`、`call_id` 和工具名，再投影为 RunEvent。这样同一会话中多个服务请求仍能关联到对应 ToolCall。

### 6.5 AsyncAgentRunResult

`ainvoke()` 聚合事件并返回：

- `final_answer`。
- `events`。
- `terminal_event`。
- 派生的 `failed` 与 `cancelled` 属性。

终态至少区分 FINAL_ANSWER、TASK_FAILED、TASK_TIMED_OUT 和 TASK_CANCELLED。超时、取消和普通异常不应全塞进一个模糊的 error 字符串。

---

## 7. 模拟感知服务

`SimulatedPerceptionService` 是第 9～10 章 C++ 服务的替身。它接收 ObservationRequest，异步等待，并产生：

1. 已接收请求。
2. 正在筛选质量帧。
3. 已生成 Observation。

最终 Observation 包含：

~~~json
{
  "observation_id": "obs-async-001",
  "target_id": "charger-01",
  "view_type": "back_label",
  "quality_score": 0.93,
  "image_path": "artifacts/charger-01/obs-async-001.jpg"
}
~~~

图片路径包含 Observation ID，避免并发请求使用同一文件名。服务不会真的创建图片，也不会执行 OCR；它只模拟异步协议和生命周期。

### 7.1 为什么服务内部捕获取消

服务使用：

~~~python
try:
    ...
except asyncio.CancelledError:
    cancelled_requests += 1
    raise
finally:
    active_requests -= 1
~~~

`finally` 保证活跃请求计数被恢复；`CancelledError` 在完成局部清理后重新抛出。若服务吞掉取消，外层 timeout、TaskGroup 和应用关闭可能误以为任务仍正常运行。

### 7.2 模拟 sleep 与真实异步 I/O

Demo 使用 `asyncio.sleep()` 表示等待，不是为了制造性能成绩。真实实现中的可等待点可能是 gRPC 异步响应、网络模型请求或数据库驱动。C++ 摄像头内部的帧循环不会由 Python sleep 替代。

---

## 8. 进度队列与背压

工具 Task 和 Agent async generator 需要一条通道传递中间进度。本章使用：

~~~python
asyncio.Queue[ProgressUpdate](maxsize=8)
~~~

### 8.1 为什么队列必须有界

如果生产者持续产生事件而消费者变慢，无界队列会不断占用内存。有界队列满时，`await queue.put()` 会暂停生产者，形成最小背压。

本章测试还把队列缩小到 1，确认进度生产者会等待消费者，同时最终 SERVICE_COMPLETED 不丢失。

### 8.2 这不是视频帧队列

这里存的是低频 ProgressUpdate，不是原始图像帧。真正的视频队列由 C++ 运行时管理，需要定义丢帧、覆盖和实时期限策略。不要把高频 Frame 放进 Python Agent 的 asyncio.Queue。

### 8.3 事件关联

服务产生的 ProgressUpdate 不知道全局事件序号。Agent 转换时加入：

~~~text
session_id + sequence + call_id + tool_name
~~~

`call_id` 在 session 内关联同一次异步工具请求；Observation ID 关联最终感知产物。它们解决不同问题。

---

## 9. 超时：结束一次过慢的等待

### 9.1 使用 asyncio.timeout

Python 3.12 提供 `asyncio.timeout()` 上下文管理器：

~~~python
try:
    async with asyncio.timeout(spec.timeout_seconds):
        result = await tool_task
except TimeoutError:
    ...
~~~

超时机制内部使用取消。超时发生后，当前等待会收到取消，离开 timeout 上下文后转换为 `TimeoutError`。运行时随后确保工具 Task 已被取消和等待，最后输出 TASK_TIMED_OUT。

### 9.2 超时不是普通失败，也不是用户取消

| 终态 | 原因 | 是否重试 | 是否表示用户意图 |
|---|---|---|---:|
| TASK_FAILED | 参数、服务或代码异常 | 按错误类型决定 | 否 |
| TASK_TIMED_OUT | 超过策略期限 | 可能重试或请求人工 | 否 |
| TASK_CANCELLED | 用户或上层主动终止 | 通常不自动重试 | 是 |

将三者分开，日志、前端提示和治理策略才有依据。

### 9.3 deadline 应逐层收紧

未来从 HTTP 到 Python、再到 gRPC/C++ 时，每层都应感知剩余 deadline。内层调用不能比外层任务允许时间更长。第三章只有工具级 timeout；总任务 deadline、重试预算和跨进程 deadline 会在后续逐步加入。

---

## 10. 取消：清理后传播

本章演示两种取消。

### 10.1 协作式用户取消

调用方传入 `asyncio.Event`：

~~~python
cancel_event = asyncio.Event()
cancel_event.set()
~~~

Agent 同时等待工具 Task 和 `cancel_event.wait()`。事件被设置后：

1. 取消正在运行的工具 Task。
2. 等待工具完成清理。
3. 产生 TASK_CANCELLED。
4. 结束当前事件流。

这种方式适合第 14 章把用户的“取消任务”操作映射为业务终态。

### 10.2 外部 Task.cancel

应用关闭、TaskGroup 失败或上层请求被取消时，消费 Agent 的 Task 可能直接收到 `cancel()`。这时运行时必须：

1. 取消工具子 Task。
2. 等待其 finally 清理。
3. 重新抛出 `CancelledError`。

它不转换为一个普通 FINAL_ANSWER，也不吞掉取消继续运行。Python 官方文档明确指出，TaskGroup 和 timeout 等结构化并发能力依赖取消语义，吞掉 `CancelledError` 可能使它们行为异常。

### 10.3 取消不等于中断恢复

- 取消：结束当前运行，通常不从原位置继续。
- timeout：因时间策略终止当前等待。
- LangGraph interrupt：保存检查点，等待外部输入后用同一 thread_id 恢复。

第 5 章实现的是第三种。不能把“捕获 CancelledError 后重新运行函数”称为恢复。

---

## 11. 并发与 TaskGroup

### 11.1 两个任务并发推进

Demo 使用：

~~~python
async with asyncio.TaskGroup() as group:
    group.create_task(run_one("A"))
    group.create_task(run_one("B"))
~~~

两个会话都在等待模拟感知服务，服务观察到 `maximum_active_requests == 2`。这证明两个请求发生了重叠等待，不是第一个完全结束后才启动第二个。

### 11.2 为什么选择 TaskGroup

`asyncio.gather()` 适合收集一组 awaitable；TaskGroup 提供结构化并发作用域：离开作用域前会等待成员任务，成员出现非取消异常时会取消其余任务并组合异常。对于“这些任务属于同一个批次”的场景，TaskGroup 更容易说明所有权。

本章仍会介绍 gather 的存在，但正式 Demo 优先使用 Python 3.12 的 TaskGroup，让任务生命周期归属于明确作用域。

### 11.3 并发不是无限并发

Demo 允许两个模拟服务请求重叠，不代表生产环境应无限创建任务。后续需要根据资源定义：

- 每用户并发上限。
- 每模型提供商并发上限。
- 感知请求队列容量。
- 单摄像头是否串行执行 ObservationRequest。
- 拒绝、排队、合并或覆盖策略。

本章有界的是进度队列，不是完整的服务准入队列。C++ 帧队列和请求背压将在第 9～10 章正式设计。

---

## 12. 可运行 Demo

### 12.1 文件

- 共享领域模型：../../examples/realsight_domain.py
- 第二章同步运行时：../../examples/ch02_minimal_agent_stream.py
- 第三章异步 Demo：../../examples/ch03_async_runtime.py
- 第三章测试：../../tests/test_ch03_async_runtime.py

### 12.2 运行命令

在 `outputs/realsight` 目录执行：

~~~powershell
python examples/ch03_async_runtime.py
~~~

不需要 API Key、网络或第三方包。

### 12.3 场景一：正常异步事件流

关键事件顺序为：

~~~text
1  TASK_STARTED
2  MODEL_DECISION
3  TOOL_CALL_REQUESTED: inspect_evidence_gaps
4  TOOL_CALL_COMPLETED
5  MODEL_DECISION
6  TOOL_CALL_REQUESTED: request_observation
7  SERVICE_REQUESTED
8  SERVICE_PROGRESS
9  SERVICE_COMPLETED
10 TOOL_CALL_COMPLETED
11 MODEL_DECISION
12 FINAL_ANSWER
~~~

三个 service 事件都携带 `async-call-002`，最终 Observation 为 `obs-async-001`。这说明业务事件、工具调用和感知产物可以沿不同 ID 关联。

### 12.4 场景二：两个任务并发

预期摘要：

~~~json
{
  "maximum_active_requests": 2,
  "completed_requests": 2,
  "terminal_events": {
    "A": "final_answer",
    "B": "final_answer"
  }
}
~~~

测试依据是活跃请求重叠，不依赖脆弱的墙钟耗时比较。

### 12.5 场景三：超时

模拟服务等待 0.1 秒，而工具 timeout 为 0.01 秒：

~~~json
{
  "terminal_event": "task_timed_out",
  "cancelled_service_requests": 1,
  "active_service_requests": 0
}
~~~

最后一个字段为 0 很重要：超时不是只向外报告一条消息，服务子 Task 也确实完成了清理。

### 12.6 沿代码主线阅读

1. 先看 `SimulatedPerceptionService.observe()` 的 try/except/finally。
2. 再看 `AsyncToolSpec` 如何声明 timeout。
3. 看 `AsyncToolRegistry.prepare()` 如何在执行前校验。
4. 看 `AsyncMinimalAgent.astream()` 如何同时等待工具、进度和取消。
5. 看 timeout 与 CancelledError 的不同分支。
6. 最后看 TaskGroup 并发 Demo 和 IsolatedAsyncioTestCase。

---

## 13. 自动化测试与验收

### 13.1 执行

~~~powershell
python -m unittest discover -s tests -v
~~~

当前实际结果：41 项全部通过，其中第一章 16 项、第二章 12 项、第三章 13 项。

### 13.2 第三章验收表

| 场景 | 预期 | 状态 |
|---|---|---|
| 正常观察 | service 生命周期事件后返回 Observation | 通过 |
| 服务事件关联 | SERVICE_* 事件携带同一 call_id | 通过 |
| 产物隔离 | image_path 包含唯一 Observation ID | 通过 |
| 事件顺序 | sequence 从 1 严格递增 | 通过 |
| 工具超时 | TASK_TIMED_OUT，服务子 Task 被取消 | 通过 |
| 协作式用户取消 | TASK_CANCELLED，资源清理完成 | 通过 |
| 外部 Task.cancel | 清理后重新传播 CancelledError | 通过 |
| 两个独立任务 | TaskGroup 内发生重叠等待并全部完成 | 通过 |
| 有界进度队列 | maxsize=1 时仍不丢最终完成事件 | 通过 |
| target 不一致 | 模型或工具执行前失败关闭 | 通过 |
| 未注册异步工具 | TASK_FAILED | 通过 |
| 模型/工具异常 | 转为结构化 TASK_FAILED | 通过 |
| 模型不收敛 | 达到 max_model_steps 后终止 | 通过 |

### 13.3 测试没有证明什么

测试使用 asyncio sleep 和内存对象，没有证明：

- C++ 摄像头可以稳定运行。
- gRPC deadline 和 cancellation 已经跨进程传播。
- 实际模型 SDK 完全非阻塞。
- 多用户负载下吞吐满足生产目标。
- 进程重启后任务可以恢复。
- 服务准入和帧级背压已经完成。

这些边界分别属于第 5～6、9～10、13～14 章。

---

## 14. 工程注意事项

### 14.1 不在事件循环里调用阻塞 SDK

某个库提供普通同步函数时，直接在 async handler 中调用仍会阻塞。短期可以评估 `asyncio.to_thread()`，但线程取消通常不能强制停止已经运行的底层函数。更稳妥的方案是使用原生异步客户端、独立服务或可取消的 RPC。

### 14.2 超时后要等待取消完成

仅调用 `task.cancel()` 不代表任务已停止。代码还要 `await task`，让 finally 和资源释放运行。Python 的 `wait_for()` 也可能因为等待实际取消完成而超过名义 timeout；deadline 监控需要理解这一点。

### 14.3 进度事件要限量

服务不应为每一帧产生 WebSocket 事件。进度应是低频状态变化，例如请求已接收、质量筛选中、Observation 已生成。帧率、队列深度等高频指标应进入监控系统或采样日志。

### 14.4 取消操作要幂等

用户可能重复点击取消，网络也可能重试取消请求。取消同一个 session/run 应得到稳定结果，不应重复释放资源或把已完成任务改写成取消。第 7 章会进一步加入治理和审计。

### 14.5 共享可变状态需要所有权

本章每个 Agent run 拥有自己的事件 sequence 和 tool_results；模拟服务的统计字段由单事件循环更新。真实跨线程 C++ 状态不能照搬这一假设，需要互斥、原子操作、消息队列或单所有者模型。

---

## 15. 能力边界与课后练习

### 15.1 本章已完成

- 异步模型和工具接口。
- async generator 业务事件流。
- 模拟异步感知服务。
- 有界进度队列。
- 工具 timeout。
- 协作式取消与外部 Task 取消。
- TaskGroup 并发。
- 正常与异常终态测试。

### 15.2 本章尚未完成

- 真实 C++ 摄像头和 gRPC。
- 真实模型异步客户端。
- 服务准入队列和速率限制。
- LangGraph State、节点和条件边。
- checkpoint、interrupt 与恢复。
- Pydantic/Protobuf 正式契约。
- 多进程部署和 WebSocket。
- DeepAgents 子 Agent。

### 15.3 课后练习

练习一：用自己的话解释为什么 `async def cpu_heavy()` 不会自动让 CPU 算法并行。

练习二：把 progress_queue_size 改为 1，逐步说明生产者为什么不会无限写入。

练习三：在 service progress 事件中删除 call_id，设计一个同会话双请求场景，解释消费者会失去什么。

练习四：为 SimulatedPerceptionService 设计“最多一个观察请求”的准入策略。比较排队、拒绝和覆盖旧请求三种选择，不必立即实现。

练习五：在外部 Task.cancel 测试中故意吞掉 CancelledError，观察测试和资源统计可能发生什么，再恢复正确实现。

练习六：画出“用户取消”和“LangGraph interrupt”的状态差异，说明哪一个可以用同一 thread_id 恢复。

练习七：判断以下能力属于工具、服务、节点还是子 Agent：功率计算、摄像头采集、说明书多轮研究、Evidence 冲突验证。

练习八：给每个异步工具设计 timeout、重试次数和幂等键，说明依据。

---

## 16. 本章小结与第 4 章桥接

本章把第二章同步循环演进为异步执行：

~~~text
await 模型决策
-> 创建异步工具 Task
-> 同时等待工具、进度、取消和 timeout
-> 用有界 Queue 传递服务进度
-> 正常完成、失败、超时或取消
-> 产生统一 RunEvent
~~~

我们也建立了重要分工：确定函数是工具，持续设备能力是服务，显式状态转换属于工作流节点，拥有独立上下文和多步推理的任务才可能成为子 Agent。C++ 感知运行时属于服务，不因为它长期运行或异步就变成 Agent。

第三章回答的是：

> Agent 怎样等待慢服务，同时保持进度、超时、取消和其他任务的响应性？

第 4 章将回答：

> 当前这些 dataclass、Any 字典和 Python 内部对象，怎样固化为可校验、可版本化、可供 C++/Python 共同使用的状态与协议契约？

下一章会引入 Pydantic 与 Protobuf，定义 LangGraph State 的初始结构，正式固化 TaskSession、ObservationRequest、Observation、Evidence、Action 和 RunEvent。第三章的异步服务仍然保留为测试替身；到第 10 章，再把它替换为 C++ gRPC 感知运行时。

---

## 参考资料

- [Python 3.12 官方文档：Coroutines and Tasks](https://docs.python.org/3.12/library/asyncio-task.html)
- [Python 3.12 官方文档：Queues](https://docs.python.org/3.12/library/asyncio-queue.html)
- [DeepAgents 官方：Subagents](https://docs.langchain.com/oss/python/deepagents/subagents)
- [DeepAgents 官方：Async subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents)
- [深度研搜第 3 章：子智能体进阶与异步执行](https://github.com/didilili/ai-agents-from-zero/blob/main/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/3-%E5%AD%90%E6%99%BA%E8%83%BD%E4%BD%93%E8%BF%9B%E9%98%B6%E4%B8%8E%E5%BC%82%E6%AD%A5%E6%89%A7%E8%A1%8C.md)
