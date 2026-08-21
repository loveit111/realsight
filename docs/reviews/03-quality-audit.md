# 第 3 章质量鉴定报告

鉴定对象：

- 正文：../chapters/03-async-runtime-and-boundaries.md
- 共享领域模型：../../examples/realsight_domain.py
- Demo：../../examples/ch03_async_runtime.py
- 测试：../../tests/test_ch03_async_runtime.py
- 前置章节：第 1～2 章正文与代码

鉴定日期：2026-08-02

## 1. 总体结论

第 3 章通过逻辑、技术栈、小 Demo 和前后桥接四项质量门禁，可以进入用户评审。

通过范围是：标准库 asyncio 下的异步 Tool Calling、服务进度、工具超时、两类取消和独立任务并发。它没有实现真实 C++ 摄像头、gRPC、LangGraph checkpoint 或 DeepAgents 子 Agent，也没有把这些能力写成已完成。

正式判定前的反例审计发现并修复了两项不会立即造成测试失败、但会影响后续工程的缺陷：

1. SERVICE_REQUESTED/PROGRESS/COMPLETED 最初没有 call_id；同一会话出现多个异步请求时无法可靠关联。运行时现已为服务事件补入 call_id 和 tool_name，测试验证三类事件一致。
2. 模拟 Observation 的 image_path 最初只包含 target 与 view，并发请求会出现路径碰撞。现在路径包含唯一 Observation ID，并加入断言。

## 2. 逻辑鉴定

结论：通过。

### 2.1 异步主线

- 从第二章同步 `decide/execute/stream` 自然演进到 `await decide/create_task/astream`，没有跳过 ToolCall 和 ToolResult 协议。
- 正文明确异步改善 I/O 等待调度，不会自动降低单任务延迟或加速 CPU 密集算法。
- coroutine、Task、event loop 和 async iterator 的推导顺序成立。
- `ainvoke()` 消费同一个 `astream()`，没有复制第二套异步执行逻辑。

### 2.2 超时与取消

- 工具 timeout 是运行时策略，不由模型参数决定。
- timeout 会取消并等待工具子 Task，最终状态为 TASK_TIMED_OUT。
- 协作式 cancel_event 产生 TASK_CANCELLED，并完成服务清理。
- 外部 `Task.cancel()` 在清理子 Task 后重新传播 CancelledError，没有吞掉结构化并发的取消语义。
- 正文明确区分取消、超时和 LangGraph 可恢复 interrupt。

### 2.3 分工边界

- C++ 感知运行时被定义为长期服务，不是子 Agent。
- 普通工具、长期服务、工作流节点和子 Agent 的选择标准没有职责冲突。
- 视觉 Evidence 子 Agent 被放在 Observation 之后，不允许 C++ 服务绕过 LangGraph 直接调度。
- DeepAgents 异步子 Agent 被标记为当前预览能力，本章没有为了“多 Agent”而引入。

### 2.4 保留的逻辑风险

- 本章只有单工具 timeout，没有总任务 deadline 和跨服务剩余 deadline 传播。
- 有界的是进度队列；感知服务请求准入、每用户并发和单摄像头串行策略尚未实现。
- TaskGroup Demo 验证并发重叠，但没有单独演示一个成员异常后取消其余成员的 ExceptionGroup 路径。
- 模拟服务与 Agent 在同一进程，不能证明跨进程取消可靠。

这些风险已在正文能力边界中列出，不影响本章限定范围通过。

## 3. 技术栈鉴定

结论：通过。

| 技术 | 是什么 | 为什么本章使用 | 后续位置 |
|---|---|---|---|
| asyncio coroutine/Task | Python 协作式异步执行单元 | 等待模型、工具和服务时让出控制权 | 第 3、14 章 |
| async iterator | 逐步异步产生值 | 保持 RunEvent 流式协议 | 第 3、14 章 |
| asyncio.Queue(maxsize) | 进程内异步有界队列 | 传递低频服务进度并提供背压 | 第 3 章 |
| asyncio.timeout | 异步截止边界 | 终止过慢工具并形成明确超时事件 | 第 3、7 章 |
| asyncio.TaskGroup | 结构化并发作用域 | 管理一组独立任务的生命周期 | 第 3、13 章 |
| Pydantic/Protobuf | 尚未引入 | 当前 Any/字典边界需要正式固化 | 第 4 章 |
| gRPC C++ async | 尚未引入 | 最终替换模拟服务 | 第 10 章 |

正文使用 Python 3.12 官方文档解释 Task、TaskGroup、timeout、Queue 和 CancelledError，并使用 DeepAgents 官方资料说明子 Agent 的上下文隔离价值和异步子 Agent 的预览状态。

环境限制仍需说明：课程目标环境为 Python 3.12，本次实际解释器为 Python 3.13.13。使用的 `TaskGroup`、`asyncio.timeout`、`IsolatedAsyncioTestCase` 和类型语法均在 3.12 可用，但尚未在独立 3.12 环境执行；第 8 章建立 `uv` 环境时补齐。

## 4. Demo 鉴定

结论：通过。

Demo 满足“小、可运行、输出可验证”：

- 只依赖 Python 标准库。
- 不调用真实模型、摄像头、网络或 DeepAgents。
- 正常场景产生 12 个有序事件，并返回 `obs-async-001`。
- 并发场景使用 TaskGroup，服务记录最大活跃请求为 2，而不是依赖墙钟耗时断言。
- 超时场景产生 TASK_TIMED_OUT，服务取消数为 1，活跃请求恢复为 0。

实际命令：

~~~powershell
python examples/ch03_async_runtime.py
python -m unittest discover -s tests -v
~~~

结果：Demo 正常退出；全量 41/41 项通过，其中第三章 13/13。

第三章测试覆盖：

1. 正常服务生命周期和最终 Observation。
2. 服务事件 call_id 与唯一产物路径。
3. sequence 严格递增。
4. timeout 取消服务并清理。
5. 协作式用户取消。
6. 外部 Task.cancel 清理后传播。
7. TaskGroup 两个独立任务并发。
8. progress queue 容量为 1 时完成事件不丢失。
9. session/belief target 不一致。
10. 未注册异步工具。
11. ToolCall target 不一致。
12. 模型与工具异常转 TASK_FAILED。
13. max_model_steps 终止不收敛模型。

## 5. 前后桥接鉴定

结论：通过。

### 与第 1 章

- 复用 TaskSession、Belief State、ObservationRequest 和 Observation。
- 观察请求仍来自证据缺口和 Action，而不是异步服务自由生成。
- target_id 在会话、状态、工具和 Observation 之间继续校验。

### 与第 2 章

- 复用 ToolCall、ToolResult、ModelDecision 和 RunEvent。
- 只增加异步 adapter/registry 和事件枚举，没有修改原有名称。
- `astream/ainvoke` 与 `stream/invoke` 保持相同业务语义。

### 与第 4～6 章

- 当前 `dict[str, Any]`、ProgressUpdate 和异步工具参数暴露了需要 Pydantic/Protobuf 固化的边界。
- TASK_CANCELLED/TASK_TIMED_OUT 与 LangGraph interrupt 的区别已建立，避免第 5 章概念混用。
- session_id/thread_id 仍是后续 checkpoint 键。

### 与第 9～10、13～14 章

- SimulatedPerceptionService 是未来 C++ gRPC 服务的替身，接口方向已明确。
- 服务与子 Agent 的判断标准为第 13 章是否引入 DeepAgents 提供依据。
- async iterator RunEvent 可在第 14 章映射为 WebSocket，而不暴露内部 Task/Queue。

## 6. 最终门禁

| 门禁 | 判定 | 保留风险 |
|---|---|---|
| 逻辑 | 通过 | 无总 deadline、服务准入和跨进程取消 |
| 技术栈 | 通过 | Python 3.12 目标环境尚未实际锁定 |
| 小 Demo | 通过 | 同进程模拟服务，不代表真实摄像头性能 |
| 前后桥接 | 通过 | 第 4 章必须收紧 Any/字典边界 |
| 自动化测试 | 第三章 13/13，全量 41/41 | 未覆盖真实网络、gRPC 和进程崩溃 |

是否允许进入用户评审：是。

是否自动生成第 4 章：否。按照逐章交付约束，应等待用户确认或提出修改。
