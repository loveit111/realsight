# 第 5 章 主动观察与中断恢复：让现实世界有时间回答 Agent

> 本章关键词：主动观察、human-in-the-loop、LangGraph interrupt、Command resume、checkpoint、thread_id、interrupt_id、幂等、恢复输入校验、InMemorySaver

第四章已经把 RealSight 的领域对象、LangGraph State 和 Protobuf 边界固化为正式契约。状态图能够判断：缺少充电器标签时产生 REQUEST_VIEW，缺少笔记本型号时产生 ASK_USER，证据齐全时产生 RUN_RULES。

但第四章每次运行到这些出口就结束了。现实世界不会在一次函数调用中立即给出答案：用户需要拿起充电器、翻到背面、对准摄像头；C++ 运行时需要等待清晰帧；视觉分析需要把 Observation 转为 Evidence；用户还可能几分钟后才找到笔记本型号。

如果 Agent 只能一直占着函数等待，服务重启、请求超时或用户离开都会让任务丢失。如果每次回来都重新开始，又会重复拍摄和询问已经完成的步骤。

第五章把第四章的“建议下一步”升级为真正的控制流：

> 工作流在证据缺口处保存状态并暂停，外部世界准备好新观察或用户输入后，用同一个 thread 和当前 interrupt 恢复；恢复值经过校验、合并到 BeliefState，再由图重新规划。

本章使用真实 LangGraph `interrupt()`、`Command(resume=...)` 和 checkpointer，不用 `input()`、布尔暂停标志或手写 while 循环冒充可恢复工作流。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：
1. 解释普通函数等待、异步等待和可恢复中断的区别。
2. 说明主动感知为什么天然需要外部事件驱动的暂停点。
3. 使用 `interrupt()` 暴露 JSON 可序列化暂停 payload。
4. 使用同一 `thread_id` 与 `Command(resume=...)` 恢复工作流。
5. 理解恢复时节点会从开头重新执行，而不是从 Python 下一行继续。
6. 解释 interrupt 之前的副作用为什么必须幂等或移到独立节点。
7. 设计 ObservationResume 与 UserResume 的 Pydantic 契约。
8. 在消费 interrupt 前，结合 checkpoint 预检恢复输入。
9. 使用 `interrupt_id` 拒绝过期页面和错误暂停点的回答。
10. 区分 Observation 成功与 Evidence 已经提取成功。
11. 使用 InMemorySaver 完成同进程 checkpoint 教学测试。
12. 配置严格 msgpack 类型白名单，避免未来反序列化策略变化。
13. 运行“背面标签 -> 接口特写 -> 笔记本型号 -> 规则准备”完整循环。
14. 说明第六章为什么还必须引入持久化 checkpointer 与产物存储。

### 1.2 学习前提

需要掌握：

- 第一章 Reality Object、Observation、Evidence、Belief State、Action。
- 第二章 Action 与 RunEvent 的区别。
- 第三章异步等待、取消、服务和 Agent 的职责边界。
- 第四章 Pydantic、StateGraph、ObservationRequest、schema_version。
- `TaskSession.thread_id == session_id` 的课程 MVP 不变量。
- Protobuf Observation 只是感知结果，不直接等于业务证据。

不要求已经实现 SQLite、C++ 摄像头或真实 gRPC。第五章仍使用模拟 Observation；第六章解决跨进程持久化，第十章连接 C++ 感知运行时。

### 1.3 本章产物

- `SessionStatus.WAITING_OBSERVATION` 会话状态。
- 稳定模块中的 `AwaitingKind` 与 `ActiveObservationState`。
- ObservationPausePayload、UserPausePayload。
- ObservationResume、UserResume。
- 三次真实 interrupt/resume 的主动观察状态图。
- thread_id + interrupt_id + request_id 多层关联检查。
- 严格 msgpack serializer 白名单。
- 22 项第五章测试。
- 独立质量鉴定报告。

本章新增的 Demo 和测试 Python 文件都在文件顶部说明整体逻辑、技术栈、调用流程和能力边界；关键节点和恢复验证函数也有中文注释。

---

## 2. 三种“等待”不是一回事

### 2.1 阻塞等待

~~~python
answer = input("请提供笔记本型号：")
~~~

当前进程和终端必须一直存在。关闭程序后，函数栈和已收集状态全部消失。这可以做十行脚本，不能支撑可恢复 Agent 服务。

### 2.2 异步等待

~~~python
observation = await perception_service.observe(request)
~~~

第三章学习的异步等待会让出事件循环，其他任务仍能推进。但持有这个 coroutine 的进程仍然需要存在。若服务重启，内存中的 Task 通常会丢失。

### 2.3 可恢复中断

~~~python
resume_value = interrupt(pause_payload)
~~~

LangGraph 保存当前 thread 的状态和下一执行位置，把 pause payload 返回给调用方。任务不需要占住一个等待中的 Python 调用。以后使用同一 thread 的 `Command(resume=...)`，图从 checkpoint 恢复。

三者解决的问题不同：

| 机制 | 是否释放当前执行 | 是否允许其他任务运行 | 进程重启后能否恢复 |
|---|---:|---:|---:|
| `input()`/阻塞 I/O | 否 | 取决于线程模型 | 否 |
| `await` | 是 | 是 | 默认否 |
| interrupt + 持久 checkpointer | 是 | 是 | 是 |

第五章使用 InMemorySaver，因此只满足“同一进程内可恢复”。真正跨重启的第三列能力要到第六章使用 SQLite checkpointer 后才成立。

---

## 3. 为什么主动感知需要 interrupt

普通聊天 Agent 的输入大多已经以文本存在。RealSight 的输入可能尚未被创造：

- 充电器背面当前朝下，摄像头看不到标签。
- USB-C 接口被手遮挡，需要用户调整角度。
- 反光导致 OCR 无法提取功率档位。
- 用户只说“我的电脑”，没有设备型号。

Agent 发现缺口后，不应猜测，也不应无限随机抽帧。它要产生带理由的 Action：

~~~text
缺 charger_label
-> REQUEST_VIEW(back_label)
-> 暂停
-> 用户翻转物体，C++ 选择清晰帧
-> Observation + Evidence 恢复
~~~

如果 Observation 没有生成 Evidence，例如画面合格但文字仍无法识别，工作流会再次评估缺口并提出新请求。主动感知不是“一次请求必然成功”，而是证据驱动的可控循环。

---

## 4. thread、checkpoint 和 interrupt ID

这三个概念容易混淆。

### 4.1 thread_id：任务历史的稳定指针

LangGraph checkpointer 使用 `thread_id` 保存和读取一个执行线程的 checkpoint。官方持久化文档说明：启用 checkpointer 后，调用必须在 `configurable` 中提供 thread_id；没有它，系统无法知道应保存或恢复哪条状态历史。[LangGraph Persistence：Threads](https://docs.langchain.com/oss/python/langgraph/persistence#threads)

本课程 MVP 使用：

~~~python
config = {
    "configurable": {"thread_id": session.thread_id},
    "recursion_limit": 50,
}
~~~

换一个 thread_id 不是“给同一任务新答案”，而是进入另一条执行历史。

### 4.2 checkpoint：某个 super-step 后的状态快照

checkpointer 在图步骤之间保存 State、下一节点、任务信息和中断信息。`graph.get_state(config)` 返回 StateSnapshot，可查看：

- `values`：当前状态值。
- `next`：下一步节点。
- `tasks`：待执行任务、错误和 interrupts。
- `config`：checkpoint 标识。

第一次暂停后，Demo 的 `next` 是：

~~~python
("wait_for_observation",)
~~~

而 State 中已经有 pending_request、REQUEST_VIEW Action、WAITING_OBSERVATION 状态。也就是说，系统先保存“正在等什么”，再进入等待节点。

### 4.3 interrupt_id：某次具体暂停的标识

同一 thread 可以连续产生很多 interrupt：标签观察、接口观察、用户型号。每个 Interrupt 都有自己的 ID。官方 Command API 支持用 `{interrupt_id: resume_value}` 映射定向恢复。[LangGraph Command Reference](https://reference.langchain.com/python/langgraph/types/Command)

第五章恢复入口同时要求：

- 正确 thread_id。
- 当前 interrupt_id。
- ObservationRequest.request_id。
- 正确 target_id 与字段。

这不是重复。它们分别关联任务历史、暂停点、感知请求和现实对象。

---

## 5. interrupt 的真实执行语义

### 5.1 第一次调用会暂停

~~~python
raw_resume = interrupt(pause.model_dump(mode="json"))
~~~

第一次运行到此处时，`interrupt()` 通过框架内部控制异常暂停图。它不会返回普通值给后续代码。LangGraph 捕获中断、保存 checkpoint，并把 payload 放在调用结果的 `__interrupt__` 中。

官方文档要求 interrupt payload 可 JSON 序列化；函数、连接、Pydantic 实例或打开的文件不应直接放进去。[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

### 5.2 恢复时节点从开头重跑

调用：

~~~python
graph.invoke(
    Command(resume={interrupt_id: normalized_payload}),
    config=config,
)
~~~

LangGraph 会从包含 interrupt 的节点开头重新执行。再次运行到同一个 interrupt 时，框架找到对应恢复值，让 `interrupt()` 返回该值，后续校验和状态更新才继续。

它不是恢复 Python 调用栈的下一条机器指令。官方参考明确说明节点会从头重新执行，因此 interrupt 之前的逻辑会再次运行。[interrupt API Reference](https://reference.langchain.com/python/langgraph/types/interrupt)

### 5.3 不要捕获 interrupt 的内部异常

错误写法：

~~~python
try:
    value = interrupt(payload)
except Exception:
    return {"failed": True}
~~~

这可能把框架用于暂停的特殊异常误当成业务异常，破坏中断传播。第五章不会在 interrupt 外包普通 `try/except`。

### 5.4 interrupt 前的副作用必须幂等

下面的代码会在恢复时重复写数据库：

~~~python
database.insert_audit_row(...)
answer = interrupt(payload)
~~~

可选策略：

1. 把副作用放到 interrupt 之后。
2. 让副作用按唯一键幂等。
3. 像本章一样拆成“准备节点”和“等待节点”。

本章选择第三种，因为学习边界最清楚。

---

## 6. 为什么要拆成准备节点和等待节点

错误的单节点形态：

~~~text
节点开始
-> 创建 request_id
-> 追加 ACTION_REQUIRED 事件
-> 写 pending_request
-> interrupt
~~~

节点在 interrupt 前还没有 return，状态更新未作为该节点结果提交；恢复时又会创建一遍 request_id 和事件。

第五章使用两步：

~~~mermaid
flowchart LR
    P["prepare_observation"] -->|"提交 State 更新"| W["wait_for_observation"]
    W -->|"interrupt"| X["外部世界"]
    X -->|"Command resume"| W
    W -->|"校验后的恢复结果"| A["apply_resumed_evidence"]
~~~

`prepare_observation` 负责：

- 选择缺失字段。
- 创建 ObservationRequest。
- 创建 REQUEST_VIEW Action。
- 更新 SessionStatus。
- 追加 RunEvent。

该节点正常 return 后，checkpointer 才推进到 wait 节点。

`wait_for_observation` 负责：
- 读取已经提交的请求和动作。
- 生成纯 JSON pause payload。
- 调用 interrupt。
- 恢复后校验输入。
- 返回 Observation 和 resumed_evidence 更新。

它在 interrupt 前没有网络请求、随机 ID、数据库写入或计数修改，因此重跑是安全的。

用户输入也采用：

~~~text
prepare_user_input
-> wait_for_user_input
-> apply_resumed_evidence
~~~

---

## 7. 第五章状态模型

### 7.1 SessionStatus 增加观察等待态

第四章已有 WAITING_USER。第五章增加：

~~~python
WAITING_OBSERVATION = "waiting_observation"
~~~

这比统一写成 `paused` 更可审计：接口层可以知道应展示摄像头引导还是文本输入框。

### 7.2 AwaitingKind

~~~python
class AwaitingKind(str, Enum):
    OBSERVATION = "observation"
    USER_INPUT = "user_input"
~~~

它描述 checkpoint 期待哪一类恢复值。恢复入口不会只看调用者提交的 `kind`，还会读取当前 State.awaiting_kind 做交叉检查。

### 7.3 ActiveObservationState

~~~python
class ActiveObservationState(RealSightGraphState):
    awaiting_kind: AwaitingKind | None = None
    expected_user_field: FieldName | None = None
    resumed_evidence: tuple[Evidence, ...] = ()
    completed_interrupts: int = 0
~~~

新增字段的意义：

- `awaiting_kind`：当前等待观察还是用户。
- `expected_user_field`：ASK_USER 正在等待哪个字段。
- `resumed_evidence`：一次恢复产生、等待原子合并的证据批次。
- `completed_interrupts`：成功恢复次数，不等于暂停尝试次数。

模型验证器保证：

- OBSERVATION wait 必须是 WAITING_OBSERVATION，且存在 pending_request。
- USER_INPUT wait 必须是 WAITING_USER，且有 expected_user_field。
- 等用户时不能遗留 pending_request。
- 不等待用户时不能残留 expected_user_field。

### 7.4 为什么状态模型移动到 contracts.models

若可持久化 Pydantic 类只定义在可直接执行的示例脚本中，运行 `python file.py` 时模块路径可能是 `__main__`。换成 Web 服务导入后，路径又可能变成包名，跨进程反序列化不稳定。

因此 AwaitingKind 和 ActiveObservationState 放在稳定的 `contracts.models`。节点和 Demo 仍在 examples。这个修改是为第六章跨进程持久化提前清理类路径问题，不是提前实现数据库。

---

## 8. 暂停 payload 与恢复 payload

### 8.1 暂停 payload

观察暂停：

~~~python
class ObservationPausePayload(StrictContract):
    kind: Literal["observation_required"]
    session_id: Identifier
    thread_id: Identifier
    action: Action
~~~

用户暂停结构相同，但 kind 为 `user_input_required`，Action 是 ASK_USER。

调用 interrupt 前使用：

~~~python
pause.model_dump(mode="json")
~~~

所以暴露给 FastAPI/WebSocket/CLI 的是普通 dict/list/string/number，不是 Python 类实例。

### 8.2 ObservationResume

~~~python
class ObservationResume(StrictContract):
    kind: Literal["observation_result"]
    observation: Observation
    evidence: tuple[Evidence, ...] = ()
~~~

evidence 允许为空，因为：

> 一次画面合格的 Observation 不保证 OCR 或视觉模型一定提取出业务 Evidence。

空 Evidence 恢复后，observed_views 会记录已看过的视角，但缺口仍存在，图会再次规划观察。

### 8.3 UserResume

~~~python
class UserResume(StrictContract):
    kind: Literal["user_input"]
    target_id: Identifier
    field: FieldName
    value: JsonValue
    source_id: Identifier
~~~

`None` 被拒绝，因为它代表仍未知。数字 0 和布尔 False 不会被误拒绝，它们可能是合法业务值。

### 8.4 判别联合

Pause 和 Resume 都使用 `kind` 作为 Pydantic discriminator。这样 wrong kind 会在明确模型中失败，不会进入任意 dict 分支。

---

## 9. 恢复输入为什么要校验两次

### 9.1 节点内校验是最后防线

观察恢复必须满足：

- payload kind 是 observation_result。
- observation.request_id 等于 pending_request.request_id。
- target_id 等于当前 RealityObject。
- view_type 等于请求视角。
- Observation 状态是 ACCEPTED。
- Evidence target 相同。
- Evidence.source_id 等于 observation_id。
- Evidence.field 属于 required_features。
- Evidence 状态是 confirmed 或 probable。

用户恢复必须满足：

- kind 是 user_input。
- target_id 与当前目标一致。
- field 等于 expected_user_field。
- value 非 null。

这些检查在 wait 节点恢复后再次执行，防止内部调用者绕过 API 封装。

### 9.2 只在节点内校验会出现什么问题

初版对抗测试把错误 request_id 直接放入 Command。发生过程是：

~~~text
Command(resume=错误值)
-> interrupt 消费恢复值并返回
-> 节点校验发现 request_id 错误
-> 节点抛异常
-> checkpoint 进入 error task 状态
~~~

原 interrupt 已被消费，不能简单地再发一个正确值“重试同一次中断”。这不是测试写错，而是 interrupt 的真实执行语义。

### 9.3 应用层预检

因此 `resume_active_observation()` 在调用 Command 之前：

1. 用 thread_id 读取 StateSnapshot。
2. 确认 checkpoint 存在且 next 是等待节点。
3. 校验 State.session.thread_id。
4. 读取当前 task 中的 active Interrupt。
5. 比对调用者提交的 interrupt_id。
6. 根据 awaiting_kind 校验完整 resume payload。
7. 只有全部通过，才调用 Command。

错误值在第六步失败时，Command 尚未发送，checkpoint 没被消费，用户可以修正后重试。

节点内保留同样校验，形成“入口预检 + 节点防线”。重复的是安全不变量，不是业务处理。

### 9.4 为什么仍不能宣称解决全部并发

预检和 invoke 之间仍存在极小时间窗口。两个请求若完全并发操作同一 thread，需要 API 层的单线程化、锁、乐观版本或幂等键。interrupt_id 定向 Command 能减少错配，但本章没有实现分布式锁。

第七章系统治理会加入每 thread 并发限制和重复恢复策略；第十四章 API 也必须只暴露安全恢复入口，不能把原始 graph.invoke 直接开放给客户端。

---

## 10. 严格 checkpoint serializer

### 10.1 初次运行出现的真实警告

第一版 Demo 虽能暂停恢复，但进程结束时 LangGraph 警告：正在反序列化未注册的 Pydantic/Enum 类型，未来严格 msgpack 模式会阻止。

忽略警告意味着课程代码可能在依赖升级后突然无法恢复 checkpoint。

### 10.2 显式类型白名单

本章使用：

~~~python
serializer = JsonPlusSerializer(
    allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES,
)
saver = InMemorySaver(serde=serializer)
~~~

白名单包含 checkpoint 确实使用的领域模型和枚举，不使用 `allowed_msgpack_modules=True` 的全放行模式。

最终测试还设置：

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
~~~

在严格模式下 22 项测试全部通过，Demo 无反序列化警告。

### 10.3 安全边界

显式白名单不是数据库访问控制的替代品。若攻击者能直接篡改 checkpoint 数据，反序列化自定义 Python 类型仍需谨慎。第六章 SQLite 文件应限制写权限，应用只恢复自己生成的版本化数据。

### 10.4 InMemorySaver 的定位

官方参考明确说明 InMemorySaver 只适合调试与测试，生产应使用持久化 saver。[InMemorySaver Reference](https://reference.langchain.com/python/langgraph.checkpoint/memory/InMemorySaver)

本章选择它是因为：

- 不引入新数据库依赖。
- 先看清 interrupt 语义。
- 测试之间可创建独立 saver。
- 失败后不会污染用户磁盘。

它的明确限制：Python 进程退出后 checkpoint 全部丢失。

---

## 11. 主动观察状态图

~~~mermaid
flowchart TD
    START(["START"]) --> ASSESS["assess_evidence"]
    ASSESS --> ROUTE{"证据缺口类型"}
    ROUTE -->|"可视觉观察"| PREP_O["prepare_observation"]
    PREP_O --> WAIT_O["wait_for_observation / interrupt"]
    WAIT_O --> APPLY["apply_resumed_evidence"]
    ROUTE -->|"需要用户上下文"| PREP_U["prepare_user_input"]
    PREP_U --> WAIT_U["wait_for_user_input / interrupt"]
    WAIT_U --> APPLY
    APPLY --> ASSESS
    ROUTE -->|"无缺口"| RULES["prepare_rules"]
    RULES --> END(["END"])
~~~

### 11.1 观察顺序

required_fields 的顺序同时表达 MVP 优先级：

~~~python
REQUIRED_FIELDS = (
    "charger_label",
    "charger_port_type",
    "laptop_model",
)
~~~

可观察字段映射到固定视角：

| 字段 | ViewType | 用户引导 |
|---|---|---|
| charger_label | BACK_LABEL | 展示背面标签并保持文字清晰 |
| charger_port_type | PORT_CLOSEUP | 展示输出接口完整轮廓 |
| laptop_model | 不可从充电器观察 | 询问用户 |

这里不用模型自由决定视角，因为 USB-C MVP 的视角集合很小且可确定。未来扩展到复杂电子设备时，主 Agent 可以在受控枚举内规划，但 ObservationRequest 仍需 Pydantic 校验。

### 11.2 apply 后回到 assess

每次恢复不是直接写死“去下一个请求”。`apply_resumed_evidence` 合并证据后回到 assess，由当前 BeliefState 重新决定：

- 已确认字段不再请求。
- 空 Evidence 会重复观察缺口。
- probable 仍可能被视为缺失。
- 不同值进入 conflicts，继续请求复核。

这比固定步骤向导更接近 Agent 工作流：路由由状态决定，不由“当前是第几页”决定。

---

## 12. Evidence 合并策略

第五章恢复需要将新 Evidence 合并到不可变 BeliefState。

### 12.1 相同 evidence_id

- 内容完全相同：幂等忽略。
- 内容不同：拒绝，说明 ID 被重复用于不同事实。

### 12.2 同字段相同值

例如视觉 OCR 和人工复核都为 65W：

- 保留两条 ledger 记录。
- supporting_evidence_ids 同时记录两个来源。
- 字段仍是 confirmed，不进入 conflict。

### 12.3 同字段不同值

例如一条为 65W，另一条为 45W：

- 从 confirmed/probable 移除。
- 收集已有支持证据与新证据。
- 写入 conflicts。
- 下一轮 assess 仍把该字段视为缺口。

### 12.4 probable 不能降级 confirmed

已确认字段收到同值 probable Evidence 时，只记入 ledger，不降低当前状态。confirmed 新证据可以提升 probable。

本章没有实现冲突消解投票或来源权重。自动“多数票覆盖”可能隐藏伪造标签或 OCR 系统性错误；后续应通过重新观察、资料核验或人工复核显式解决。

---

## 13. 小 Demo：连续三次暂停与恢复

### 13.1 运行命令

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
outputs\realsight\.venv\Scripts\python.exe `
  outputs\realsight\examples\ch05_active_observation_interrupts.py
~~~

当前验证依赖沿用第四章：

- Python 实际环境 3.13，课程目标 3.12。
- LangGraph 1.2.10。
- langgraph-checkpoint 4.1.1。
- Pydantic 2.13.4。

Python 3.12 尚未在本机实跑，仍是第八章环境工程的明确任务。

### 13.2 第一次暂停：背面标签

关键输出：

~~~json
{
  "step": "1-back-label",
  "pause_kind": "observation_required",
  "session_status": "waiting_observation",
  "route": "request_observation",
  "action_type": "request_view",
  "request": {
    "view_type": "back_label",
    "required_features": ["charger_label"]
  }
}
~~~

checkpoint.next 是 `wait_for_observation`。pending_request 已在状态中，因此应用重启到同一 checkpoint 时知道正在等哪一个请求。

### 13.3 第二次暂停：接口特写

提交 label Observation/Evidence 后，图合并证据并重新评估：

~~~json
{
  "step": "2-port-closeup",
  "view_type": "port_closeup",
  "required_features": ["charger_port_type"],
  "completed_interrupts": 1
}
~~~

新的 interrupt_id 与第一次不同，request_id 也从 `obsreq-001` 变为 `obsreq-002`。

### 13.4 第三次暂停：笔记本型号

接口 Evidence 恢复后：

~~~json
{
  "step": "3-laptop-model",
  "pause_kind": "user_input_required",
  "session_status": "waiting_user",
  "route": "await_user",
  "action_type": "ask_user",
  "expected_field": "laptop_model",
  "completed_interrupts": 2
}
~~~

pending_request 已清空，因为这次不是摄像头请求。

### 13.5 正常结束：准备规则

提交用户型号后：

~~~json
{
  "step": "4-ready-for-rules",
  "route": "run_rules",
  "action_type": "run_rules",
  "confirmed_fields": [
    "charger_label",
    "charger_max_power_w",
    "charger_port_type",
    "charger_protocol",
    "laptop_model"
  ],
  "observed_views": ["back_label", "port_closeup"],
  "completed_interrupts": 3,
  "checkpoint_next": [],
  "event_count": 11
}
~~~

`checkpoint_next=[]` 表明图已经正常到 END，不再存在可恢复 interrupt。恢复入口会拒绝继续向完成线程提交答案。

---

## 14. 测试设计

第五章有 22 项测试，且在 `LANGGRAPH_STRICT_MSGPACK=true` 下通过。

### 14.1 InterruptLifecycleTests

覆盖：

- 首次 invoke 在请求已保存后暂停。
- pause payload 可 JSON 序列化。
- 标签恢复进入接口特写。
- 接口恢复进入用户问题。
- 用户恢复进入 RUN_RULES。
- 三次恢复后的 RunEvent sequence 严格递增。
- 完成线程不能再次 resume。

### 14.2 ResumeBoundaryTests

覆盖：

- 未知 thread_id。
- 过期 interrupt_id。
- 错 resume kind。
- 错 request_id。
- 错 target_id。
- 错 view_type。
- FAILED Observation。
- Evidence.source_id 与 observation_id 不同。
- 提交未请求字段。
- 空 Evidence 后重复观察。
- 错 user field。
- null 用户值。

错误预检后还会检查：

- pending_request 没有变化。
- completed_interrupts 没增加。
- checkpoint.next 仍是原等待节点。
- 后续正确值可以继续恢复。

### 14.3 ThreadIsolationTests

同一个 InMemorySaver 中启动 session-a 与 session-b，只恢复 A 的标签：

- A 进入下一个视角。
- B 仍等待自己的标签。
- 两个 BeliefState 和 target_id 没有交叉。

### 14.4 BeliefMergeTests

覆盖：

- 相同值增加 supporting source。
- 不同值形成 conflict，不静默覆盖。

---

## 15. 常见错误与排查顺序

### 15.1 换 thread_id 恢复

症状：找不到 checkpoint，或框架尝试从空状态开始。

排查：比较 TaskSession.session_id、TaskSession.thread_id 与 config thread_id。MVP 中三者必须一致。

### 15.2 在 wait 节点 interrupt 前写日志或生成随机 ID

症状：恢复一次却出现两条事件、两个 request_id 或重复数据库记录。

处理：将准备逻辑移到独立节点，或按稳定幂等键写入。

### 15.3 用 try/except 包 interrupt

症状：图没有真正暂停，反而进入业务失败分支。

处理：不要捕获 interrupt 的控制异常。只在恢复值返回后验证业务数据。

### 15.4 直接把错误值交给 Command，再希望重试

症状：interrupt 已消费，节点在校验后失败，snapshot.next 为空或 task 进入 error。

处理：通过安全恢复入口，在发送 Command 前结合 checkpoint 预检。

### 15.5 只检查 thread，不检查 interrupt/request

症状：同一任务旧页面的响应可能回答新的暂停点。

处理：同时检查 interrupt_id、request_id、target_id 和 expected field。

### 15.6 把 Observation 当成一定有 Evidence

症状：Observation 成功后系统错误地认为字段已确认。

处理：Evidence 为空时只记录 observed_view，重新评估缺口。

### 15.7 把 InMemorySaver 当持久化

症状：Demo 中能恢复，重启进程后全部丢失。

处理：第六章切换 SQLite checkpointer，并进行真实进程重启测试。

---

## 16. 能力边界

本章已经完成：

- 真实动态 interrupt。
- 同 thread 多次 Command resume。
- 暂停前 checkpoint 状态提交。
- observation/user 两类恢复契约。
- 恢复前预检与节点内二次校验。
- interrupt_id 定向恢复。
- Evidence 幂等、支持与冲突合并。
- 严格 serializer 白名单。
- 两 thread 隔离测试。

本章没有完成：



- 跨进程或重启恢复。
- SQLite/Postgres checkpointer。
- checkpoint schema migration。
- 图像文件生命周期与会话目录。
- 同一 thread 的分布式并发锁。
- 真实 C++ ObservationEvent 自动触发恢复。
- 用户身份认证与恢复权限。
- 观察次数、成本和时间上限。
- USB-C 最终规则。

这些边界会分别进入第六、七、十、十二和十四章。

---

## 17. 课后练习

### 练习 1：观察 checkpoint

第一次暂停后打印：

~~~python
snapshot = graph.get_state(config)
print(snapshot.values)
print(snapshot.next)
print(snapshot.tasks)
~~~

找出 pending_request、interrupt ID 和下一节点分别在哪里。

### 练习 2：验证节点重跑

在 wait_for_observation 的 interrupt 前增加一个仅打印、不修改状态的计数提示，观察首次暂停和恢复时打印几次。然后解释为什么不能在这里执行扣费。

不要提交会改变正式状态的实验代码。

### 练习 3：空 Evidence

提交 accepted Observation，但 evidence 为空。验证：

- observed_views 包含 back_label。
- charger_label 仍未 confirmed。
- 图产生新的 back_label request。
- request_id 与上一请求不同。

### 练习 4：过期 interrupt ID

保存第一次 interrupt ID，完成标签恢复后，在接口暂停点使用旧 ID。确认恢复入口拒绝，并说明 thread_id 为什么不足以防止该问题。

### 练习 5：制造证据冲突

第一次提交 65W，第二个独立来源提交 45W。检查 conflicts、ledger 和 supporting_evidence_ids。设计一个“请求人工复核”的新 Action，但暂不自动决定谁正确。

### 练习 6：设计重启验收

为第六章写出测试步骤：

1. 进程 A 启动图并暂停。
2. 退出进程 A。
3. 进程 B 使用同一数据库与 thread_id。
4. 读取 checkpoint。
5. 使用当前 interrupt_id 恢复。
6. 验证事件不重复、Evidence 不丢失。

### 练习 7：并发恢复思考

两个浏览器几乎同时提交同一 interrupt 的答案。列出可能的竞态，并思考使用每 thread 锁、checkpoint version 或唯一幂等键的方案。

---

## 18. 本章小结与第六章桥接

第五章把第四章的状态路由变成了真正能等待现实世界的工作流：

~~~text
检查证据
-> 准备请求并保存状态
-> interrupt
-> 外部拍摄或用户回答
-> thread_id + interrupt_id 定向恢复
-> Pydantic 预检与节点二次校验
-> 合并 Evidence
-> 重新检查证据
~~~

Demo 在同一 thread 中依次请求背面标签、接口特写和笔记本型号，最终准备运行规则。错误 thread、旧 interrupt、错 request、错目标和错来源都会在消费 checkpoint 前失败。

但当前 InMemorySaver 只存在于一个 Python 进程中。关闭 Demo 后，所有 checkpoint 都消失；Observation 图片也只是路径字符串，没有会话目录、原子写入和清理策略。

第六章将把本章循环升级为真正的持久执行：

~~~text
ActiveObservationState
-> SQLite LangGraph checkpointer 保存执行状态
-> Evidence Ledger 保存可查询证据
-> artifacts/session_id/ 保存图片与派生产物
-> 进程退出
-> 新进程读取同一 thread_id
-> 验证 schema_version 后继续 resume
~~~

第五章证明“图可以暂停”；第六章要证明“任务不会因为进程消失而失忆”。
