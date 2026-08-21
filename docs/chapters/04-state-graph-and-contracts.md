# 第 4 章 状态图与协议契约：让工作流、Python 与 C++ 对同一事实达成一致

> 本章关键词：Pydantic v2、LangGraph StateGraph、State、Node、Edge、条件路由、Protobuf、proto3、optional、oneof、schema_version、适配层

前三章已经建立了 RealSight 的认知与运行基础：第一章把 USB-C 判断改写成证据闭环；第二章用 Tool Calling 和 RunEvent 跑通最小 Agent；第三章加入异步服务、进度、超时与取消。现在系统里已经出现多个边界：用户输入进入 Python，模型生成工具参数，LangGraph 节点交换状态，Python 向未来的 C++ 运行时发出 ObservationRequest，C++ 再返回 Observation。

只要这些边界仍依赖“大家记得字典里有哪些键”，系统就很脆弱。`quality` 写成 `quality_score`、`target_id` 引用了另一件物体、置信度写成 1.2、一个动作声称是 `ask_user` 却携带 `request_view` 参数，都可能一路传播到持久化或设备服务才暴露。

第四章要解决的是：

> 把前三章口头和 dataclass 中的约定，升级为能够校验、序列化、跨语言生成代码并参与状态路由的正式契约。

本章不会把 Pydantic、LangGraph 和 Protobuf 混成一个“万能数据框架”。三者分别解决不同问题：Pydantic 负责 Python 业务语义，LangGraph 负责状态如何经过节点演进，Protobuf 负责 Python/C++ 之间的线协议。职责分清以后，第五章的中断恢复、第六章的检查点和第十章的 gRPC 才有稳定地基。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 解释“Python 业务模型”“工作流状态”和“跨进程消息”为什么不是同一个概念。
2. 使用 Pydantic v2 定义字段约束、对象级约束和跨对象聚合约束。
3. 说明 `extra="forbid"`、严格数字类型和带时区时间的工程价值。
4. 区分 `field_validator` 与 `model_validator` 的适用范围。
5. 理解 LangGraph 的 State、Node、Edge、条件边、START、END 和 compile。
6. 设计只包含可序列化业务数据的 `RealSightGraphState`。
7. 用 proto3 定义枚举、消息、`optional` 字段、`oneof` 和服务。
8. 解释 Protobuf 默认值为什么不能替代“字段是否出现”的判断。
9. 编写 Pydantic 与生成的 Protobuf 类型之间的显式适配器。
10. 运行一个真实 LangGraph + 真实 Protobuf 二进制往返的小 Demo。
11. 用失败测试验证目标隔离、证据账本、动作参数和协议版本。
12. 说明这些产物如何进入第五章的主动观察和中断恢复。

### 1.2 学习前提

需要理解前三章的以下内容：

- `TaskSession` 表示一次任务，`RealityObject` 表示持续存在的现实目标。
- `ObservationRequest` 是请求，`Observation` 是观察结果，`Evidence` 是业务事实。
- `BeliefState` 把字段分到 confirmed、probable、unknown、conflict 四个状态区。
- `Action` 表示接下来做什么，`RunEvent` 表示已经发生什么。
- `target_id` 必须贯穿会话、工具、服务、观察和证据。
- C++ 感知运行时是服务，不是子 Agent。
- 异步解决等待调度，但不负责证明输入是否合法。

不要求你已经会 C++ Protobuf API，也不要求真实 gRPC 服务可用。本章先固定协议并在 Python 内完成真实编译和二进制往返；第十章再生成 C++/Python gRPC stub 并接入摄像头运行时。

### 1.3 本章产物

本章交付以下可运行产物：

- `contracts/models.py`：八个稳定领域类型和 `RealSightGraphState` 的 Pydantic v2 定义。
- `contracts/realsight.proto`：Python Agent 与未来 C++ 感知运行时之间的 proto3 契约。
- `examples/ch04_state_and_contracts.py`：真实 LangGraph 三分支路由与 Protobuf 往返 Demo。
- `tests/test_ch04_state_and_contracts.py`：29 项契约、路由、传输和序列化测试。
- `requirements-ch04.txt`：本章实际验证的依赖版本快照。
- 一份独立质量鉴定报告。

所有本章手写 `.py` 文件顶部都有整文件中文说明，关键逻辑也有局部注释。`realsight_pb2.py` 由编译器临时生成，不是学习源码，也不会写入仓库。

---

## 2. 为什么 dataclass 和自由字典到这里不够了

### 2.1 dataclass 仍然有价值

第一章的 dataclass 不是错误。它适合：

- 第一次解释领域概念。
- 表达 Python 内部的结构化数据。
- 减少构造器、比较和打印方法的样板代码。
- 在没有第三方依赖时快速运行 Demo。

因此前三章保留原文件，作为学习过程的可运行快照。第四章不是否定它们，而是系统边界增多后增加更强的入口保护。

### 2.2 外部输入不是可信 Python 对象

以下数据都不能假定正确：

- FastAPI 收到的 JSON。
- 模型生成的 Tool Calling 参数。
- SQLite 中旧版本会话恢复出的记录。
- C++ 运行时返回的 Protobuf 消息。
- WebSocket 客户端发来的取消信息。
- 测试夹具和人工修改的配置。

普通 dataclass 会接受很多语义不合法的组合。例如 `confidence=1.2` 在 Python 类型上仍是 float；`thread_id="B"` 在类型上也是字符串，但它可能错误地把会话 A 恢复到线程 B。类型正确不等于业务正确。

### 2.3 自由字典的问题更严重

如果节点之间传递普通字典：

~~~python
state = {
    "target": "charger-001",
    "miss_fields": ["laptop_model"],
}
~~~

拼写错误 `miss_fields` 不会自动报错。下游读取 `missing_fields` 时可能把它当成空值，进而错误地进入规则计算。更危险的是，某些框架会静默忽略未知字段，让错误看起来像“成功运行”。

Pydantic 的目标不是让错误消失，而是让错误尽量在数据进入系统的地方变得明确、可定位、可测试。官方文档将 `BaseModel` 描述为通过类型标注定义 schema 的核心方式，并支持验证、序列化与 JSON Schema 生成。[Pydantic Models 官方文档](https://docs.pydantic.dev/latest/concepts/models/)

---

## 3. 三层契约地图

RealSight 本章使用三类契约，不让任何一种工具越权承担全部责任。

| 层次 | 主要技术 | 负责什么 | 不负责什么 |
|---|---|---|---|
| Python 业务语义 | Pydantic v2 | 字段范围、对象关系、目标隔离、JSON 序列化 | 网络传输、节点执行顺序 |
| 工作流演进 | LangGraph StateGraph | 节点、边、条件路由、状态更新、未来检查点入口 | C++ 类型生成、字段业务真伪 |
| 跨语言传输 | Protobuf / gRPC | Python/C++ 强类型消息、二进制编码、流式服务接口 | USB-C 规则、证据冲突判断 |

数据经过系统时的关系如下：

~~~mermaid
flowchart LR
    I["JSON / 模型参数"] --> P["Pydantic 入口校验"]
    P --> S["RealSightGraphState"]
    S --> N["LangGraph 节点"]
    N --> A["ObservationRequest 业务模型"]
    A --> M["显式适配器"]
    M --> W["Protobuf wire bytes"]
    W --> C["未来 C++ 感知运行时"]
    C --> O["Protobuf Observation"]
    O --> V["适配器 + Pydantic 再校验"]
    V --> S
~~~

这里有两个重要原则。

第一，Protobuf 能证明某字段在线路上是 `double`，但不能自动证明置信度在 0～1，也不能证明 Observation 的 `target_id` 与当前会话一致。业务语义仍由 Pydantic 和工作流入口检查。

第二，Pydantic 能把对象变成 JSON，但它不会为 C++ 自动生成高效的原生类型，也不定义 server streaming RPC。跨语言边界仍使用 Protobuf/gRPC。

---

## 4. Pydantic：把约定写成可执行模型

### 4.1 统一基础模型

所有本章契约继承 `StrictContract`：

~~~python
class StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )
~~~

这些配置分别表示：

- `extra="forbid"`：拒绝模型没有声明的字段，拼写错误不会被静默忽略。
- `frozen=True`：契约对象创建后不能随手原地修改，状态更新应产生新快照。
- `str_strip_whitespace=True`：清理字符串两侧空白。
- `validate_default=True`：默认值也经过验证。

冻结不是为了追求“函数式编程风格”，而是防止一个节点持有状态引用后悄悄改变旧快照。LangGraph 节点应返回更新，框架再合并状态；这个模式更容易记录、测试和恢复。

### 4.2 字段约束不只是类型提示

本章集中定义了可复用约束：

~~~python
Identifier = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

UnitFloat = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
~~~

`Identifier` 拒绝空 ID 和不可控格式；`UnitFloat` 拒绝 1.2、-0.1 以及字符串形式的 `"0.9"`。这里有意使用严格数字类型：在 Agent 系统中，静默类型转换可能掩盖模型或协议适配器的输出错误。

Pydantic 默认可以执行一定程度的数据转换，例如把字符串数字转换成整数。官方文档说明 strict mode 可以关闭这类转换。本项目没有全局开启所有字段严格模式，而是对 ID、置信度、质量分和序号等关键字段使用严格类型；枚举仍允许从 JSON 字符串解析。[Pydantic Models：数据转换与 strict mode](https://docs.pydantic.dev/latest/concepts/models/#data-conversion)

### 4.3 字段验证器与模型验证器

只涉及单个字段时使用 `field_validator`。例如 ObservationRequest 的 `required_features` 必须非空且不重复：

~~~python
@field_validator("required_features")
@classmethod
def required_features_are_unique(cls, features):
    if not features:
        raise ValueError("required_features must contain at least one field")
    if len(set(features)) != len(features):
        raise ValueError("required_features must not contain duplicates")
    return features
~~~

涉及多个字段关系时使用 `model_validator`。例如成功观察不能带失败原因，而失败观察必须说明原因：

~~~python
@model_validator(mode="after")
def status_matches_failure_reason(self):
    if self.status == ObservationStatus.ACCEPTED and self.failure_reason is not None:
        raise ValueError("accepted observation must not contain failure_reason")
    if self.status != ObservationStatus.ACCEPTED and self.failure_reason is None:
        raise ValueError("rejected or failed observation requires failure_reason")
    return self
~~~

验证器应尽量无副作用：不访问摄像头、不写数据库、不调用模型。它们要回答的是“这个对象能否成立”，不是“下一步该做什么”。后者属于 LangGraph 节点。

### 4.4 为什么要求带时区时间

`Observation.captured_at` 与 `RunEvent.occurred_at` 使用 `AwareDatetime`。没有时区的 `2026-08-03 12:00` 无法判断是北京时间还是 UTC；跨 C++、Python、SQLite 和日志系统后，这会造成排序与超时分析错误。

本项目内部统一使用 UTC，界面展示时再转换为用户时区。Protobuf 传输使用 Unix 毫秒时间戳，适配回 Python 后明确加上 UTC 时区。

---

## 5. 八个稳定领域类型如何正式固化

第一章承诺全课程统一使用八个名称。第四章用 Pydantic 将它们升级为正式 Python 契约。

| 类型 | 核心身份 | 本章新增保护 |
|---|---|---|
| `TaskSession` | 一次可恢复任务 | `session_id == thread_id`、状态枚举、目标引用 |
| `RealityObject` | 持续存在的现实目标 | 跟踪状态枚举、稳定 target_id |
| `ObservationRequest` | Agent 想观察什么 | 受控视角、非空特征、超时范围、关联 ID |
| `Observation` | C++ 实际看到了什么 | 带时区时间、质量范围、成功/失败语义 |
| `Evidence` | 可追溯业务事实 | JSON 安全值、来源、置信度、状态、派生链 |
| `BeliefState` | 当前认知快照 | 四区互斥、账本引用、目标隔离、真冲突校验 |
| `Action` | 计划执行的下一步 | 判别联合、动作类型与 payload 一致 |
| `RunEvent` | 已经发生的过程事实 | 正序 sequence、会话关联、时间与数据载荷 |

### 5.1 Observation 仍然不是 Evidence

这个边界在加入 Pydantic 后仍不改变：

~~~text
Observation
  observation_id = observation-001
  image_path = artifacts/observation-001.jpg
  quality.overall_score = 0.93

经过 OCR / 多模态抽取 / 复核

Evidence
  field = charger_max_power_w
  value = 65
  source_id = observation-001
  confidence = 0.97
~~~

Observation 表明某次采集发生了，Evidence 表明系统从某个来源提取出一个可用于决策的事实。C++ 不应直接宣称“兼容”；视觉工具也不应跳过 Observation 来源直接制造无出处的 Evidence。

### 5.2 schema_version 的作用

跨边界对象带有 `schema_version=1`。它不是软件版本号，也不是模型版本号，而是“这一份数据按哪一版结构和语义解释”。

本章只接受版本 1。未来增加兼容字段时可以继续保持版本 1；发生必须迁移的语义变化时，再由明确的迁移器把旧数据转换到新版本。不能遇到未知版本仍按当前结构猜测。

### 5.3 thread_id 为什么暂时等于 session_id

LangGraph 持久化使用 `thread_id` 找到检查点。课程 MVP 让一个 TaskSession 对应一个 thread，避免一个业务会话散落在多个恢复键中。第五章与第六章会沿用该映射。

这不是说所有企业系统都必须一一对应。未来若一个会话需要多个并行子图，可以增加明确的 child run ID；在一个月 MVP 中先保留可解释的不变量。

---

## 6. BeliefState：最容易“看起来有类型，实际已损坏”的地方

BeliefState 同时包含当前结论和历史账本，因此需要最强的聚合校验。

### 6.1 四个状态区必须互斥

同一个字段不能同时出现在：

- `confirmed`
- `probable`
- `unknown`
- `conflicts`

如果 `charger_protocol` 同时在 confirmed 和 unknown，条件路由就无法可靠判断是否继续观察。本章验证器对四个集合两两求交，只要出现重叠就拒绝整份状态。

### 6.2 活跃证据必须存在于 ledger

`confirmed["charger_protocol"]` 指向的 Evidence 必须在 `ledger` 中以自己的 `evidence_id` 找到完全相同的记录。这样“当前认知”才能追溯到证据账本。

`supporting_evidence_ids` 还必须：

- 只服务于 confirmed 或 probable 字段。
- ID 不重复。
- 每个 ID 在 ledger 中存在。
- 指向同一业务字段。
- 包含当前活跃 Evidence 自身的 ID。

如果当前结论指向证据 A，支持列表却只写证据 B，即使二者值相同，也说明引用关系已经损坏。

### 6.3 冲突不是“列表里有两个元素”

本章对冲突增加三项要求：

1. 至少两条证据。
2. evidence_id 互不重复。
3. 至少存在两个不同值。

把同一 Evidence 放两遍不能伪装成冲突；两个来源都读到 65W 也不是冲突，而是共同支持。真正的冲突示例是 OCR 读到 65W，而人工复核读到 45W。

冲突解决仍不在验证器中自动完成。验证器只证明“这确实是一组冲突数据”；第五章以后可以请求重新观察或人工复核，第七章再增加调用次数和治理限制。

---

## 7. Action 使用判别联合，而不是任意 payload 字典

如果 Action 只有 `payload: dict[str, Any]`，下面的对象在类型上很难被拒绝：

~~~python
{
    "action_type": "ask_user",
    "payload": {
        "view_type": "back_label",
        "required_features": ["charger_label"]
    }
}
~~~

动作类型说“询问用户”，参数却像“请求视角”。本章为每类 Action 定义专用 payload：

- `RequestViewPayload`
- `AskUserPayload`
- `RetrieveKnowledgePayload`
- `RunRulesPayload`
- `GenerateAnswerPayload`

它们都含有字面量 `kind`，再通过 Pydantic 判别联合选择模型：

~~~python
ActionPayload = Annotated[
    RequestViewPayload
    | AskUserPayload
    | RetrieveKnowledgePayload
    | RunRulesPayload
    | GenerateAnswerPayload,
    Field(discriminator="kind"),
]
~~~

`Action` 的模型验证器还要求 `action_type.value == payload.kind`。对 REQUEST_VIEW，会继续检查嵌套 ObservationRequest 与 Action 的 target_id 一致。

到 GraphState 层还要再检查：

- 请求的 session_id 与当前会话一致。
- Action 中的 request 与 `pending_request` 是同一个逻辑请求。
- `request_observation` 路由必须有一个且仅一个 REQUEST_VIEW Action。
- `await_user` 路由必须有 ASK_USER Action。
- `run_rules` 路由必须有 RUN_RULES Action。

这体现了三层校验：字段合法、Action 自身合法、Action 放进当前状态后仍合法。

---

## 8. LangGraph：状态如何经过节点演进

LangGraph 官方将图的核心分成三部分：State 是应用当前快照，Nodes 执行业务逻辑并返回状态更新，Edges 决定下一个节点。StateGraph 在执行前必须 compile；编译阶段会进行基础拓扑检查，并接收未来的 checkpointer 与中断设置。[LangGraph Graph API 官方概览](https://docs.langchain.com/oss/python/langgraph/graph-api)

### 8.1 RealSightGraphState 保存什么

本章状态包含：

- session 与 target。
- belief 与 required_fields。
- 本轮 missing_fields。
- pending_request 与 pending_actions。
- last_observation。
- route。
- events。
- iteration。

这些内容都能通过 `model_dump_json()` 序列化。第六章可以在这个基础上接入检查点，而不需要先清理连接对象。

### 8.2 State 不保存什么

以下对象不能进入可检查点状态：

- gRPC channel 或 stub。
- OpenCV VideoCapture。
- 数据库 connection / session。
- LLM client。
- `asyncio.Queue`、Lock、Event、Task。
- 回调函数和打开的文件句柄。

它们是运行时依赖，不是业务事实。当前 LangGraph 官方 API 支持通过 runtime context 向节点提供依赖；业务 State 与运行资源应分离。[LangGraph Runtime context](https://docs.langchain.com/oss/python/langgraph/graph-api#runtime-context)

### 8.3 为什么本章主状态没有 messages

参考的深度研搜课程在把 LangGraph 子图包装成 DeepAgents `CompiledSubAgent` 时强调 `messages`，因为主 Agent 通过消息向子图传递任务并读取结果。这个要求适用于那类“作为对话型子 Agent 接口”的图。

RealSight 第四章构建的是确定性领域工作流，还没有作为 CompiledSubAgent 暴露。它要路由的是证据缺口，不是一段聊天历史，因此不应该为了模仿示例而把全部业务状态塞进 `messages`。

第十三章如果把视觉分析或资料检索子图包装给主 Agent，可以在子图适配边界增加 `messages`，再把输出转换为 Evidence。主领域图仍保留结构化状态。这正是“借鉴深度研搜工程方法，而不是复制项目表面形状”。

### 8.4 为什么选择 Pydantic State

LangGraph 支持 TypedDict、dataclass 和 Pydantic model。官方也提示 Pydantic 递归验证更强，但性能低于 TypedDict 或 dataclass。[LangGraph State schema](https://docs.langchain.com/oss/python/langgraph/graph-api#schema)

RealSight 的选择依据是：

- 主工作流每次处理 Observation、Evidence 或用户输入，频率远低于视频帧率。
- 状态错误会影响中断恢复和最终结论，正确性优先于微小校验开销。
- 高频画面质量计算在 C++，不会让每帧穿过 Pydantic 和 LangGraph。

因此 Pydantic 很适合低频编排层，不适合 30 FPS 的逐帧数据路径。

### 8.5 本章最小图

~~~mermaid
flowchart TD
    START(["START"]) --> A["assess_evidence"]
    A --> R{"route_after_assessment"}
    R -->|"有视觉缺口"| O["build_observation"]
    R -->|"只缺用户上下文"| U["ask_user"]
    R -->|"证据齐全"| C["prepare_rules"]
    O --> END1(["END"])
    U --> END2(["END"])
    C --> END3(["END"])
~~~

图的组装代码是：

~~~python
workflow = StateGraph(RealSightGraphState)
workflow.add_node("assess_evidence", assess_evidence_node)
workflow.add_node("build_observation", build_observation_node)
workflow.add_node("ask_user", ask_user_node)
workflow.add_node("prepare_rules", prepare_rules_node)

workflow.add_edge(START, "assess_evidence")
workflow.add_conditional_edges(
    "assess_evidence",
    route_after_assessment,
    {
        "observe": "build_observation",
        "ask_user": "ask_user",
        "rules": "prepare_rules",
    },
)
graph = workflow.compile()
~~~

`assess_evidence_node` 不直接执行摄像头或规则，只返回 `missing_fields`。条件边读取更新后的状态，再选择下一节点。每个出口节点只构造一个结构化 Action。

### 8.6 节点返回更新，而不是任意修改 State

~~~python
def assess_evidence_node(state: RealSightGraphState) -> dict[str, Any]:
    missing = tuple(
        field_name
        for field_name in state.required_fields
        if field_name not in state.belief.confirmed
        or field_name in state.belief.conflicts
    )
    return {"missing_fields": missing}
~~~

节点只返回自己负责的字段更新。这样边界更容易测试：给定相同 State，节点应返回相同结果。外部 I/O 节点以后仍可异步，但它的输入输出必须保持结构化。

本章图出口再次调用 `RealSightGraphState.model_validate(result)`。即使框架已经按 schema 管理状态，明确的出口校验仍能把完整聚合不变量作为应用边界固定下来。

---

## 9. Protobuf：Python 与 C++ 的线协议

### 9.1 为什么内部服务不继续只用 HTTP/JSON

浏览器到 FastAPI 仍适合 HTTP/JSON 和 WebSocket，因为浏览器原生支持、调试方便。Python 工作流到本地 C++ 感知服务选择 gRPC/Protobuf，原因包括：

- 从同一个 `.proto` 为 Python 和 C++ 生成类型。
- 明确字段编号与兼容演进规则。
- 支持 server streaming ObservationEvent。
- RPC 原生表达 deadline、取消和状态码。
- 二进制消息通常比 JSON 紧凑。

低延迟是收益之一，但不是唯一理由。若请求本身需要等待用户翻转物体 3 秒，序列化节省的几毫秒不是主要矛盾；强类型、流式事件、取消和长期维护更重要。

### 9.2 协议只包含跨 C++ 边界的对象

`realsight.proto` 定义：

- ObservationRequest。
- QualitySignals。
- Observation。
- ObservationProgress。
- ObservationFailure。
- ObservationEvent。
- 取消请求与响应。
- PerceptionRuntime 服务。

它有意不包含 Evidence、BeliefState、Action 和 USB-C 规则结论。C++ 负责实时采集和质量筛选；Python 负责语义证据与决策。把 Python 的全部业务状态复制进 proto 会扩大耦合，迫使 C++ 跟随 Agent 内部结构频繁变更。

### 9.3 枚举的零值必须是 UNSPECIFIED

proto3 枚举的默认数值是 0。本协议使用：

~~~protobuf
enum ViewType {
  VIEW_TYPE_UNSPECIFIED = 0;
  VIEW_TYPE_FULL_OBJECT = 1;
  VIEW_TYPE_FRONT_LABEL = 2;
  VIEW_TYPE_BACK_LABEL = 3;
  VIEW_TYPE_PORT_CLOSEUP = 4;
}
~~~

如果调用方没设置 view_type，解析后得到 UNSPECIFIED，而不是误认为 FULL_OBJECT。适配器会拒绝 UNSPECIFIED，迫使调用方明确意图。

### 9.4 optional 解决“缺失”和“真实零值”

proto3 普通 scalar 字段若没有显式 presence，未设置与设置为默认值在 API 层可能无法区分。例如质量分 0.0 是“画面完全不可用”的合法值，但字段缺失不是一回事。

因此协议使用：

~~~protobuf
message QualitySignals {
  optional double overall_score = 1;
  optional double sharpness = 2;
  optional double exposure = 3;
  optional double glare = 4;
  optional double target_ratio = 5;
}
~~~

`captured_at_unix_ms` 也使用 optional。适配器要求总分、quality 消息和时间戳确实出现；sharpness 等分项可以不存在。Protobuf 官方的 field presence 指南建议 proto3 基础类型在需要区分缺失时使用 `optional`，生成 API 可通过 `HasField`/`has_*` 判断。[Protobuf Field Presence](https://protobuf.dev/programming-guides/field_presence/)

这条规则是本章对抗审读时发现并修正的真实问题：最初把总分和时间写成普通 scalar，缺失字段会变成 0 或 Unix epoch，看起来仍能通过范围校验。

### 9.5 oneof 表达互斥事件

ObservationEvent 一次只能携带以下一种 payload：

- progress
- observation
- failure

~~~protobuf
oneof payload {
  ObservationProgress progress = 10;
  Observation observation = 11;
  ObservationFailure failure = 12;
}
~~~

这比同时放三个 optional 字段更明确：生成代码会提供 oneof case API，设置新成员时会清除旧成员。事件序号和 request_id 则用于关联与排序。

### 9.6 字段编号一旦投入使用不能随意改

Protobuf wire format 使用字段编号识别数据。上线后不能因为“看起来更整齐”就重排编号；删除字段后应 reserve 旧编号和名称，避免未来复用造成旧数据被错误解释。官方 proto3 指南明确警告字段编号不能改变或复用。[Proto3 Language Guide：字段编号与删除](https://protobuf.dev/programming-guides/proto3/#assigning-field-numbers)

本章是版本 1 初始协议，尚无已删除字段，因此不人为添加无意义的 reserved 列表。以后每次兼容性变更都应增加测试；第八章工程化时可引入 Buf 或类似 schema breaking 检查。

### 9.7 服务为什么是 server streaming

~~~protobuf
service PerceptionRuntime {
  rpc Observe(ObservationRequest) returns (stream ObservationEvent);
  rpc Cancel(CancelObservationRequest) returns (CancelObservationResponse);
}
~~~

一次观察可能经历：请求接受、等待目标稳定、检测反光、找到候选帧、筛选完成。server streaming 允许 C++ 在最终 Observation 之前发送进度与可重试失败信息。第三章模拟的 SERVICE_REQUESTED/PROGRESS/COMPLETED 将在第十章映射到这个流。

本章只编译消息与 service descriptor，没有生成或启动真正 gRPC server。接口提前固定，网络行为延后实现。

---

## 10. 为什么需要显式 Pydantic/Protobuf 适配器

字段名称相同不代表可以直接互换。适配器负责：

- Pydantic 枚举与 Protobuf 数值枚举映射。
- datetime 与 Unix 毫秒转换。
- optional presence 判断。
- schema_version 检查。
- repeated 容器与 tuple 转换。
- wire 消息解析后重新执行 Pydantic 语义验证。

请求编码路径是：

~~~text
ObservationRequest(Pydantic)
-> observation_request_to_proto()
-> generated ObservationRequest(Protobuf)
-> SerializeToString()
-> bytes
~~~

解码路径必须反向通过业务校验：

~~~text
bytes
-> ParseFromString()
-> generated ObservationRequest(Protobuf)
-> observation_request_from_proto()
-> ObservationRequest(Pydantic)
~~~

不能因为 Protobuf 成功解析就直接信任消息。一个 schema_version=0、view_type=UNSPECIFIED、required_features 为空的消息在 wire 层完全可以解析，但在 RealSight 业务中不合法。

### 10.1 生成代码为什么不手改

`protoc` 生成的 `realsight_pb2.py` 是构建产物。手工向其中加入业务逻辑或教学注释会带来两个问题：

- 下一次重新生成会全部覆盖。
- Python 与 C++ 的行为可能因人为修改而不再来自同一个 schema。

本章 Demo 用 `TemporaryDirectory` 生成并加载 pb2，进程退出后自动清理。第八章会把生成过程放入正式构建任务；开发者仍然阅读 `.proto`、Pydantic 模型和适配器，而不是阅读生成器内部代码。

---

## 11. 小 Demo：三轮证据路由与二进制往返

### 11.1 环境说明

本章实际验证版本写在 `requirements-ch04.txt`：

~~~text
pydantic==2.13.4
protobuf==6.33.6
grpcio-tools==1.81.1
langgraph==1.2.10
~~~

课程正式目标仍是 Python 3.12。当前机器没有 Python 3.12，因此本次项目本地 `.venv` 使用 Python 3.13 执行；代码只使用 Python 3.12 可用语法和依赖 API。第八章必须建立正式 Python 3.12 + uv 环境并补跑兼容性矩阵，不能把“理论兼容”写成“已在 3.12 验证”。

### 11.2 运行命令

在仓库工作区根目录执行：

~~~powershell
outputs\realsight\.venv\Scripts\python.exe outputs\realsight\examples\ch04_state_and_contracts.py
~~~

只运行第四章测试：

~~~powershell
outputs\realsight\.venv\Scripts\python.exe -m unittest discover `
  -s outputs\realsight\tests -p "test_ch04*.py" -v
~~~

运行前四章全部测试：

~~~powershell
outputs\realsight\.venv\Scripts\python.exe -m unittest discover `
  -s outputs\realsight\tests -v
~~~

### 11.3 初始状态

Demo 假设已经从用户笔记中得到：

- `charger_max_power_w = 65`
- `charger_protocol = usb_pd`

仍缺少：

- `charger_label`
- `laptop_model`

第一轮图发现存在可观察字段 `charger_label`，输出：

~~~json
{
  "missing_fields": ["charger_label", "laptop_model"],
  "route": "request_observation",
  "action_type": "request_view",
  "required_features": ["charger_label"]
}
~~~

注意 missing_fields 仍列出 laptop_model，但 ObservationRequest 只包含摄像头能够补齐的 charger_label。工作流不会要求 C++ 从充电器画面猜测用户的笔记本型号。

### 11.4 Protobuf 往返

Demo 动态编译 `.proto` 后执行：

~~~python
wire_bytes = encode_observation_request(state.pending_request)
restored_request = decode_observation_request(wire_bytes)
~~~

实测输出：

~~~text
protobuf round-trip: {
  'wire_bytes': 181,
  'request_equal': True,
  'generated_in_temp': True
}
~~~

181 字节不是性能基准。它只证明：真实 Protobuf 编译器可接受 schema，生成类型可以编码，解码后能恢复为等价 Pydantic 请求。

### 11.5 第二轮：询问用户

Demo 模拟视觉分析产生：

~~~text
field = charger_label
value = "USB-C PD 65W"
source_type = vision_ocr
source_id = observation-001
confidence = 0.97
~~~

视觉字段已齐全，只剩 laptop_model。图输出：

~~~json
{
  "missing_fields": ["laptop_model"],
  "route": "await_user",
  "action_type": "ask_user",
  "question": "请提供需要充电的笔记本电脑准确型号"
}
~~~

本章图到 END 结束一次调用。第五章会把这个 END 演进为真正的 interrupt：保存状态、等待用户输入，再使用同一 thread_id 恢复。

### 11.6 第三轮：准备规则

模拟用户提供 `ExampleBook Pro 14` 后，所有必要字段齐全：

~~~json
{
  "missing_fields": [],
  "route": "run_rules",
  "action_type": "run_rules",
  "rule_set": "usb_c_compatibility_v1"
}
~~~

这里有意不输出“兼容”。第四章只证明状态与协议，USB-C 电压档位、功率、端口和协议的确定性规则仍在第十二章实现。提前写一个过度简化规则会模糊章节责任。

### 11.7 检查点预演

最后执行：

~~~python
checkpoint_json = state.model_dump_json()
restored_state = RealSightGraphState.model_validate_json(checkpoint_json)
~~~

实测恢复对象与原状态相等。这不等于已经实现持久化：还没有数据库事务、checkpoint_id、并发恢复或图片产物管理。它只证明 State 的内容具备第六章持久化的基本前提。

---

## 12. 测试设计：正常路径只是开始

第四章共有 29 项测试，分为四组。

### 12.1 ContractValidationTests

验证：

- thread_id 与 session_id 不一致时拒绝。
- 未声明字段被拒绝。
- 置信度越界和字符串数字被拒绝。
- Observation 时间缺少时区时拒绝。
- Observation 状态与 failure_reason 不一致时拒绝。
- BeliefState 四区重叠时拒绝。
- 跨 target Evidence 被拒绝。
- 活跃证据不在 ledger 时拒绝。
- 重复 Evidence 伪装成冲突时拒绝。
- supporting 列表不含活跃证据时拒绝。
- Action 类型与判别 payload 不一致时拒绝。
- REQUEST_VIEW 跨目标时拒绝。
- RunEvent sequence 非递增时拒绝。

### 12.2 GraphRoutingTests

验证三条条件边：

- 初始缺标签 -> request_observation。
- 补齐视觉证据 -> await_user。
- 补齐笔记本型号 -> run_rules。

这些测试不依赖 LLM，因此同一输入永远产生同一路由。第十三章接入主 Agent 后，确定性状态路由仍应保留单元测试；模型只参与真正需要语义判断的节点。

### 12.3 ProtobufContractTests

验证：

- `.proto` 能由真实 grpcio-tools 编译。
- PerceptionRuntime.Observe 确实是 server streaming。
- correlation_id 存在和缺失都能保持 presence。
- UNSPECIFIED 枚举被适配器拒绝。
- schema_version=0 被拒绝。
- Observation optional 质量字段往返后保持缺失。
- 缺失 captured_at 或 overall_score 被拒绝。
- 失败 Observation 的原因正确往返。
- proto 没有越界包含 Evidence、BeliefState、Action。
- 仓库没有手工提交生成的 pb2 学习源码。

### 12.4 SerializationTests

验证：

- GraphState JSON 往返等价。
- pending_request 不能来自另一会话。
- Action 中的请求必须等于 GraphState 的 pending_request。

最后一项是审计后新增的重要测试。仅检查 target_id 不够；两个请求可以属于同一目标，却有不同 request_id、视角或特征。如果 Action 和状态各指向一个请求，取消与结果关联都会混乱。

---

## 13. 常见错误与调试顺序

### 13.1 把 ValidationError 当作“框架太严格”

先看错误的 `loc` 与 `type`：

~~~python
except ValidationError as error:
    print(error.errors())
~~~

不要第一反应改成 `Any` 或删除验证器。先确认输入为何违反不变量；若业务确实允许，再修改模型和测试。

### 13.2 在所有地方复制同一份模型

Pydantic 和 Protobuf 会有相似字段，但它们属于不同边界。避免为 HTTP、数据库、图状态、gRPC 各复制一套完全独立而无人维护的模型。当前策略是：

- Pydantic 是 Python 业务语义来源。
- `.proto` 是 C++/Python wire 来源。
- 适配器明确连接两者。
- 持久化以后存 Pydantic 状态的版本化表示。

字段变化时必须同时更新相关 schema、适配器和测试。

### 13.3 把生成代码当成业务代码修改

永远修改 `.proto`，然后重新生成。生成文件只进入构建目录。若生成结果异常，检查 protoc 与 runtime 版本是否匹配，而不是直接改 pb2。

### 13.4 在 State 中放客户端对象

如果 `model_dump_json()` 报“无法序列化 channel/Task/VideoCapture”，根因通常不是“Pydantic 缺一个 encoder”，而是运行时资源放错了层。将它移到 runtime context 或依赖容器。

### 13.5 用 Protobuf 代替全部业务验证

Protobuf 的主要目标是兼容演进和跨语言编码，不提供 proto3 required 字段。官方风格指南说明 required 不利于长期 schema 演进，因此 proto3 移除了 required。[Protobuf Style Guide：Required Fields](https://protobuf.dev/programming-guides/style/#required-fields)

必要字段通过 optional presence、适配器检查和 Pydantic 模型共同保证。wire 可解析不代表业务可接受。

### 13.6 让状态图逐帧运行

LangGraph 是任务编排层，不是实时图像循环。若把每一帧变成 GraphState 更新，Python 校验、检查点和日志都会迅速成为瓶颈。正确路径仍是：

~~~text
C++ 连续处理帧
-> 本地质量判断与关键帧筛选
-> 只在有意义的状态变化或观察完成时发事件
-> Python LangGraph 更新任务状态
~~~

---

## 14. 能力边界与尚未完成的工作

本章已经完成：

- 正式 Pydantic 领域契约。
- 可序列化 LangGraph State。
- 三条确定性条件路由。
- 可编译 proto3 schema。
- 请求与观察的真实二进制往返。
- 聚合不变量和失败路径测试。

本章没有完成：

- 真实摄像头与 OpenCV。
- C++ Protobuf/gRPC 生成与服务实现。
- gRPC 网络 deadline、取消和背压联调。
- LangGraph checkpointer。
- `interrupt()` 与 `Command(resume=...)`。
- OCR 或多模态 Evidence 抽取。
- 设备资料检索。
- USB-C 兼容性最终规则。
- FastAPI 与 WebSocket。

这些不是遗漏，而是章节边界。第四章产出的 ObservationRequest 和 State 会继续演进，不会在后续重命名核心类型。

### 14.1 当前环境偏差

课程目标是 Python 3.12，本机本章实际运行在项目本地 Python 3.13 `.venv`。这意味着：

- 本章功能和测试已真实验证。
- Python 3.12 兼容性尚未在本机实跑。
- 第八章工程初始化必须创建 3.12 环境并将同一测试套件加入 CI。

质量报告会把它列为残余风险，而不是写成“全部无条件通过”。

---

## 15. 课后练习

### 练习 1：阅读 ValidationError

把 `confidence=0.97` 改成 `"0.97"`，运行 Demo，找到错误中的：

- loc
- type
- input

解释为什么本项目不在此处自动转换。

### 练习 2：增加一个可观察字段

把 `connector_shape` 加入 required_fields 和 OBSERVABLE_FIELDS，选择合理的 ViewType。要求：

- 初始图把它放进 ObservationRequest。
- 补齐 Evidence 后不再请求。
- 增加至少两个测试。

不要重命名现有字段。

### 练习 3：制造真冲突与伪冲突

分别构造：

- 65W 与 45W 两条证据。
- 同一条 65W 证据重复两次。
- 两个来源都为 65W。

解释为什么只有第一种应该进入 conflicts。

### 练习 4：检查 Protobuf presence

在生成的临时模块中创建 `QualitySignals(overall_score=0.0)`，验证 `HasField("overall_score")` 为 True；再创建空 QualitySignals，验证为 False。说明二者为什么不能用 `score == 0` 区分。

### 练习 5：画出协议变更审查表

假设 Observation 要增加 `camera_id`，写出需要检查的文件和测试：

- Pydantic 模型是否需要字段。
- `.proto` 使用哪个新编号。
- 适配器如何映射。
- 旧发送端缺少该字段时怎么办。
- Python/C++ 是否可以滚动升级。

### 练习 6：解释 messages 的边界

用自己的话回答：为什么深度研搜的 CompiledSubAgent 图常用 messages，而本章 RealSight 主图没有 messages？何时应该增加适配层？

---

## 16. 本章小结与第五章桥接

本章把“数据结构”提升为三个相互配合的工程契约：

1. Pydantic 保证 Python 业务对象和聚合状态成立。
2. LangGraph 让证据状态通过节点和条件边产生可检查的下一步。
3. Protobuf 固定 Python 与未来 C++ 感知运行时的跨语言边界。

Demo 已经能根据证据缺口选择：请求背面标签、询问笔记本型号、准备运行确定性规则。它还能把 ObservationRequest 编码为真实 Protobuf bytes，再恢复为通过 Pydantic 校验的对象。

但第二条路径当前只是返回 `await_user` 后结束。现实系统需要在这里暂停数分钟甚至数小时，进程重启后仍能知道：正在等哪个字段、属于哪个 session、恢复后应从哪个节点继续。

第五章将以本章的 `TaskSession.thread_id`、`RealSightGraphState`、`Action` 和 ObservationRequest 为输入，学习主动观察与中断恢复：

~~~text
发现 charger_label 缺失
-> 产生 REQUEST_VIEW
-> interrupt，保存可恢复状态
-> 用户翻转充电器或补充型号
-> 使用同一 thread_id resume
-> 校验新输入并继续图
~~~

第四章的关键成果不是“多了几个类”，而是后续每次暂停、传输和恢复都有一份可以被程序验证的事实边界。
