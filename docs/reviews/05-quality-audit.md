# 第 5 章质量鉴定报告

## 1. 鉴定结论

**结论：逻辑闭环、技术栈说明、小 Demo、前后章节桥接四项通过，可以交付第五章；但不能宣称已经实现“跨进程持久恢复”，因为本章使用 InMemorySaver。**

本章不是在正常 Demo 成功后直接判定通过。实现与测试过程中发现并修正三类真实问题：

1. checkpoint 反序列化未注册 Pydantic/Enum 类型产生前向兼容警告。
2. 错误 resume 值直接进入 Command 后会消费 interrupt，再在节点校验阶段失败，不能简单重试。
3. 只使用 thread_id/request_id 仍缺少对同一 thread 内过期暂停响应的精确关联。

最终处理：

- 使用稳定 `contracts.models` 类路径。
- JsonPlusSerializer 显式允许所需类型，不开启 allow-all。
- 在 `LANGGRAPH_STRICT_MSGPACK=true` 下验证。
- 应用层在 Command 前读取 checkpoint 并校验恢复值。
- 节点恢复后保留第二次校验。
- 恢复必须携带当前 interrupt_id，并使用 ID 映射 Command。

---

## 2. 逻辑闭环鉴定

**结果：通过。**

本章逻辑链为：

~~~text
第四章只能输出“建议下一步”
-> 现实观察和用户输入无法在一次调用内立即获得
-> 引入 checkpointer + thread_id
-> 准备节点先提交 Action/Request/Session 状态
-> 无副作用等待节点调用 interrupt
-> 外部获得 JSON pause payload 与 interrupt_id
-> 安全恢复入口读取当前 checkpoint
-> 预检 thread/interrupt/request/target/view/field/source
-> Command 定向恢复
-> 节点从头重跑并二次校验
-> Evidence 原子合并
-> 回到证据评估
-> 三次恢复后到 RUN_RULES
~~~

职责核对：

- `prepare_*` 节点创建请求、动作和事件。
- `wait_*` 节点只负责 interrupt 与恢复校验。
- `resume_active_observation` 是应用边界，不是新 Agent。
- `apply_resumed_evidence` 合并状态，不执行摄像头或最终规则。
- Observation 与 Evidence 保持分离，允许空 Evidence。
- InMemorySaver 明确限定为教学测试。
- 第六章持久化、第七章治理、第十章 C++ 联调没有被提前伪实现。

### 修正问题 A：checkpoint 类型前向兼容

初版运行成功但出现 LangGraph 警告：未注册 Pydantic/Enum 类型未来将在严格 msgpack 下被阻止。

修正：

- `AwaitingKind` 与 `ActiveObservationState` 从 examples 移到稳定 `contracts.models`。
- 使用 `JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES)`。
- 白名单只含实际 checkpoint 类型，不使用全放行。
- 严格环境变量下 Demo 和 22 项测试通过，无警告。

### 修正问题 B：错误恢复消费 interrupt

初版仅在 wait 节点内校验。错误值已经成为 interrupt 返回值，随后节点抛异常，原暂停不再处于普通可重试状态。

修正：

- 恢复入口先读取 StateSnapshot。
- 在发送 Command 前执行与节点相同的上下文校验。
- 预检失败时 checkpoint.next、pending_request 和计数保持不变。
- 测试证明错误后可以提交正确值继续。

### 修正问题 C：过期响应关联

同一个 thread 有多个 ObservationRequest。只校验 thread_id 无法区分标签暂停和接口暂停。

修正：

- 从 snapshot.tasks 读取唯一 active Interrupt。
- 调用方必须回传当前 interrupt_id。
- 不一致时在 Command 前拒绝。
- 使用 `Command(resume={interrupt_id: payload})` 定向恢复。

---

## 3. 技术栈清晰度鉴定

**结果：通过。**

| 技术 | 本章角色 | 不承担的职责 | 后续章节 |
|---|---|---|---|
| `interrupt()` | 动态暂停并暴露 JSON payload | 输入可信性、数据库持久化 | 第 13 章主图复用 |
| `Command(resume=...)` | 给指定 interrupt 恢复值 | API 鉴权、并发锁 | 第 14 章接口 |
| `thread_id` | checkpoint 执行历史主键 | 当前暂停精确标识 | 第 6 章持久化 |
| `interrupt_id` | 单次暂停标识 | 现实目标身份 | 第 14 章客户端关联 |
| InMemorySaver | 同进程教学 checkpoint | 重启恢复、生产存储 | 第 6 章替换 |
| Pydantic | pause/resume 与状态校验 | 节点执行顺序 | 持续复用 |
| JsonPlusSerializer | checkpoint 序列化与类型白名单 | 数据库访问控制 | 第 6 章 |

正文明确解释：

- interrupt 与 async await 的区别。
- 恢复会重跑节点。
- 不能捕获 interrupt 控制异常。
- 副作用为什么要拆出等待节点。
- checkpointer、thread 和 interrupt 的关系。
- InMemorySaver 的非持久边界。
- 错误恢复为何必须 Command 前预检。
- interrupt_id 不能替代 request_id/target_id。

技术说明依据当前 LangGraph 官方 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [Command Reference](https://reference.langchain.com/python/langgraph/types/Command)。

---

## 4. Demo 鉴定

**结果：通过。**

Demo 使用真实 LangGraph 1.2.10 与 langgraph-checkpoint 4.1.1，连续执行：

1. BACK_LABEL interrupt。
2. label Observation/Evidence resume。
3. PORT_CLOSEUP interrupt。
4. port Observation/Evidence resume。
5. laptop_model 用户 interrupt。
6. UserResume。
7. RUN_RULES 正常出口。

关键实测：

~~~text
pause 1 = observation_required / back_label
pause 2 = observation_required / port_closeup
pause 3 = user_input_required / laptop_model
completed_interrupts = 3
confirmed fields = 5
observed views = 2
checkpoint_next = []
event_count = 11
strict msgpack warnings = 0
~~~

Demo 不需要 LLM、摄像头、网络或数据库，规模足以观察中断语义，又没有混入后续专业能力。

### 代码可读性

- 新 Demo 文件顶部有整体逻辑、技术栈、调用流程和关键边界说明。
- 新测试文件顶部有测试结构、技术栈和执行流程说明。
- wait/prepare/apply、恢复预检、Evidence 合并均有中文注释和 docstring。
- 共享 `contracts.models` 原本已有文件级大注释，新增状态紧邻正式 GraphState。

---

## 5. 前后桥接鉴定

**结果：通过。**

### 向前桥接

- 复用第一章 Evidence 缺口与主动观察思想。
- 复用第二章 Action/RunEvent，并保持事件严格递增。
- 复用第三章服务与 Agent 边界；Observation 仍由外部感知流程产生。
- 复用第四章 Pydantic 八类型、GraphState、thread_id、ViewType 和请求关联。
- 只新增 WAITING_OBSERVATION、AwaitingKind 和 ActiveObservationState 字段，没有重命名既有类型。

### 向后桥接

- 第 6 章：将 InMemorySaver 替换为 SQLite saver，加入进程重启测试和产物目录。
- 第 7 章：增加每 thread 并发锁、恢复幂等、观察次数和超时治理。
- 第 10 章：C++ ObservationEvent 到来后，由服务适配层构造 ObservationResume。
- 第 11 章：视觉证据工具产生带 source_id 的 Evidence。
- 第 13 章：主工作流复用本章 interrupt 节点和安全恢复入口。
- 第 14 章：FastAPI/WebSocket 暴露 thread_id、interrupt_id 和 pause payload。

---

## 6. 测试鉴定

第五章：**22/22 通过**。

运行条件：

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
~~~

覆盖：

- 三个暂停点和三个恢复。
- checkpoint 状态先于 interrupt 保存。
- JSON pause payload。
- 完成线程拒绝恢复。
- 事件序号。
- 未知 thread。
- 过期 interrupt ID。
- 错 kind/request/target/view/status/source/field。
- null 用户值。
- 错误预检后 checkpoint 可重试。
- 空 Evidence 重新观察。
- 两 thread 隔离。
- 同值支持和异值冲突。

前五章全项目回归：**92/92 通过**，同样在严格 msgpack 模式下执行，耗时约 0.77 秒。

---

## 7. 残余风险

### R1：InMemorySaver 不跨进程

严重度：高，但属于明确章节边界。

进程退出后 checkpoint 丢失。第五章只能证明中断语义，不能证明重启恢复。

处理：第六章使用 SQLite checkpointer，进行两个独立进程的暂停/恢复测试。

### R2：同 thread 并发恢复未加锁

严重度：中。

interrupt_id 定向恢复减少错配，但预检到 invoke 之间仍可能发生并发竞态。

处理：第七章加入 thread 级互斥、checkpoint 版本或幂等键；API 不允许并发恢复同一暂停。

### R3：真实 C++ 服务尚未接入

严重度：预期内。

Observation 和 Evidence 由确定性 helper 构造，没有验证 gRPC 流、摄像头取消或实际 OCR。

处理：第十、十一章分别联调感知流和视觉 Evidence。

### R4：Python 3.12 尚未实机验证

严重度：中低。

当前项目本地 `.venv` 使用 Python 3.13。代码使用 3.12 可用语法，但不能将理论兼容写成实测。

处理：第八章创建 Python 3.12 + uv 环境并执行全部测试。

### R5：直接绕过安全恢复入口仍会消费错误值

严重度：中。

节点内校验能阻止错误数据写入，但直接调用原始 `graph.invoke(Command(...))` 可能让 checkpoint 进入 error state。

处理：应用只暴露 `resume_active_observation`；第十四章 API 层禁止客户端访问原始 graph 对象。

---

## 8. 最终验收表

| 验收项 | 结论 | 证据 |
|---|---|---|
| 逻辑闭环 | 通过 | 缺口 -> 准备 -> interrupt -> 预检 -> resume -> 合并 -> 重规划 |
| 概念是否清楚 | 通过 | 区分 await、interrupt、thread、checkpoint、interrupt ID |
| 职责边界 | 通过 | 准备/等待/恢复入口/合并节点分工明确 |
| 技术栈说明 | 通过 | 说明是什么、为什么、限制和后续章节 |
| 小 Demo | 通过 | 三次真实中断，无外部服务依赖 |
| 失败路径 | 通过 | 12 类错误恢复和 checkpoint 可重试验证 |
| 严格序列化 | 通过 | strict msgpack 下无警告 |
| 前章桥接 | 通过 | 复用八类型、GraphState、Action、RunEvent |
| 后章桥接 | 通过 | 明确进入 SQLite、治理、C++、主图和 API |
| Python 注释 | 通过 | 新手写文件均有顶部大注释与关键块说明 |
| 跨进程恢复 | 未完成 | 明确留给第 6 章 |
| Python 3.12 实测 | 待第 8 章 | 当前 Python 3.13 本地环境 |

综合判断：第五章可以交付；停止在本章，等待用户审核，不提前生成第六章。
