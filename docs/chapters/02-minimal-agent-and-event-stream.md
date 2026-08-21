# 第 2 章 最小 Agent 与事件流：看懂一次 Tool Calling 执行

> 本章关键词：Tool Calling、ModelAdapter、Tool Registry、call_id、invoke、stream、RunEvent、失败关闭

第一章已经能检查证据缺口、生成 Action、执行 USB-C MVP 规则，但它的 `run_demo()` 按固定顺序直接调用函数。那是一段可靠的业务逻辑，还不是完整的 Agent 执行循环。

本章补上最薄的一层 Agent 运行时：模型先产生一个结构化决策；如果决策是工具调用，运行时校验并执行工具，再把结果交回模型；如果决策是最终回答，运行结束。整个过程同时投影为有序的 `RunEvent`，使开发者和未来前端能够知道任务进行到哪一步。

我们仍不接入真实大模型和第三方框架，而是使用一个明确命名为 `DeterministicModelAdapter` 的确定性模型替身。它不是 AI，也不假装理解语言；它的价值是把 Agent 协议本身暴露出来，使每一步都可预测、可断言。等协议被理解后，真实模型只需替换适配器，而不应改写工具、事件和领域规则。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 区分大模型、Agent 运行时、工具、工作流和子 Agent。
2. 解释一次最小 Tool Calling 循环的每一步。
3. 理解“模型请求调用工具”不等于“工具已经执行”。
4. 使用 `call_id` 关联一次工具请求与对应结果。
5. 区分 `invoke()` 的聚合结果和 `stream()` 的过程事件。
6. 区分业务事件流、状态更新流和 token 文本流。
7. 使用工具白名单、参数校验、目标校验和最大步数让 Agent 失败关闭。
8. 运行两个 Demo 场景，并阅读完整事件轨迹。

### 1.2 学习前提

你应当已经理解第一章的：

- TaskSession 和 Reality Object。
- Observation、Evidence 与 Belief State。
- Action 是机器可执行的下一步，不只是自然语言建议。
- USB-C 判断必须经过确定性规则门禁。

本章不要求 LangGraph、DeepAgents、`asyncio` 或任何模型 API 经验。

### 1.3 本章产物

- 一个只依赖 Python 标准库的最小 Agent 循环。
- `ModelAdapter`、`ToolCall`、`ToolResult`、`ToolSpec` 与 `ToolRegistry`。
- 可以 `invoke` 也可以 `stream` 的统一运行协议。
- 基于第一章 `RunEvent` 扩展的事件类型与递增序号。
- 两个 RealSight 工具：证据缺口检查和 USB-C MVP 规则执行。
- 12 项第二章自动化测试；与第一章合计 28 项。

---

## 2. 第一章还缺哪一层

第一章的逻辑大致是：

~~~python
actions = plan_next_actions(belief)
result = evaluate_compatibility(belief)
answer = build_evidence_answer(result)
~~~

代码调用顺序由开发者提前写死。这对业务规则是好事，但尚未回答三个问题：

1. 谁决定当前应该先检查证据，还是直接运行规则？
2. 工具执行后，结果怎样回到决策者并触发下一步？
3. 一个需要数秒甚至数分钟的运行过程，怎样向外部持续报告进度？

Agent 运行时补的是这一层“决策—行动—反馈”循环，而不是替代第一章的确定性函数。

### 2.1 五个角色不要混在一起

| 角色 | 在本章做什么 | 不做什么 |
|---|---|---|
| 用户 | 提出“能否充电” | 不选择内部工具 |
| 模型适配器 | 产生 ToolCall 或 FinalAnswer | 不直接执行 Python 函数 |
| Agent 运行时 | 循环、校验、调用、记录、终止 | 不发明 USB-C 规则 |
| 工具 | 读取 Belief State，返回结构化结果 | 不自行选择下一个工具 |
| 确定性规则 | 根据完整证据判断 MVP 条件 | 不理解开放式用户语言 |

真实大模型是一个可能的决策实现，不等于 Agent 本身。Agent 是模型、工具、状态、循环和治理规则组成的系统。

### 2.2 为什么不能让模型直接回答

如果把用户问题和 Belief State 一次性发给模型，然后接受一段文本，会失去：

- 必填证据门禁。
- 工具调用记录。
- 来源追踪。
- 规则可重复性。
- 超时、取消和最大步数治理。
- 失败发生在哪一步的信息。

因此 RealSight 的模型只能提出受约束决策。真正影响外部系统或最终业务结论的动作由运行时和确定性工具执行。

---

## 3. 最小 Tool Calling 循环

一次最小 Agent 运行可以表示为：

~~~mermaid
flowchart TD
    U["用户输入 + TaskSession + Belief State"] --> M["ModelAdapter.decide"]
    M --> D{"决策类型"}
    D -->|ToolCall| V["校验工具名、参数与 target_id"]
    V --> X["ToolRegistry 执行工具"]
    X --> T["ToolResult + call_id"]
    T --> M
    D -->|FinalAnswer| F["结束本轮运行"]
    V -->|失败| E["TASK_FAILED"]
    M -->|超过步数| E
~~~

对应伪代码：

~~~python
tool_results = []

for step in range(max_model_steps):
    decision = model.decide(user_input, session, tool_results)

    if decision.kind == "final_answer":
        return decision.final_answer

    tool_call = decision.tool_call
    tool_result = registry.execute(tool_call, context)
    tool_results.append(tool_result)

raise MaxStepsExceeded
~~~

虽然只有几行，里面包含 Agent 最重要的协议。

### 3.1 第一次模型决策

运行时会把用户问题、会话上下文、可用工具描述和已有工具结果交给模型适配器。模型只能返回两类结构之一：

~~~text
ModelDecision(tool_call=...)
ModelDecision(final_answer=...)
~~~

本章故意不允许一条决策同时既调用工具又宣称最终答案。这样状态清楚，也方便测试。

### 3.2 ToolCall 是请求，不是执行结果

本章的 ToolCall 包含：

~~~json
{
  "call_id": "call-001",
  "name": "inspect_evidence_gaps",
  "arguments": {
    "target_id": "charger-01"
  }
}
~~~

它表达的是：“模型希望运行时调用这个工具，并使用这些参数。”此时工具尚未执行，数据库没有被查询，摄像头也没有收到命令。

这是 Tool Calling 最常见的概念误区。模型输出中的函数名和 JSON 参数，只是一份结构化请求。只有 Agent 运行时在白名单中找到工具、完成校验并调用 handler 后，副作用才真正发生。

### 3.3 ToolResult 必须带回 call_id

工具执行结果包含同一个 `call_id`：

~~~json
{
  "call_id": "call-001",
  "name": "inspect_evidence_gaps",
  "output": {
    "missing_fields": ["power_profiles", "supported_protocol"],
    "actions": ["request_view", "ask_user"]
  }
}
~~~

未来一个模型可能一次请求多个工具，工具也可能并行返回。如果只靠工具名关联，两个相同工具调用会混在一起。`call_id` 是请求与结果的相关标识，也是日志排障的基本字段。

### 3.4 再次决策与收束

模型收到 ToolResult 后重新决策：

- 若缺少证据，生成本轮最终输出，说明需要哪些 Action。
- 若证据齐全，调用 `evaluate_mvp_charging`。
- 规则工具完成后，生成证据化最终回答。

“最终回答”表示当前这次 Agent 运行已经收束，不代表整个现实任务永远结束。证据不足时，它只是停在外部输入边界。真正把该边界持久化为 interrupt 是第 5～6 章的工作。

---

## 4. 为什么本章不直接使用 DeepAgents

参考的深度研搜课程在快速入门中直接使用 DeepAgents，并从 `messages`、模型节点和工具节点解析执行过程。这种方式适合快速看到框架效果，但 RealSight 更需要先弄清底层合同。

DeepAgents 官方将其定位为 Agent harness：它在基本 Tool Calling 循环之上加入文件系统、上下文管理、子 Agent、长期记忆、权限和可选任务规划，并使用 LangGraph 运行时获得持久执行与流式能力。对于本章只有两个工具的任务，先引入完整 harness 会让学习者难以判断哪些行为来自模型、哪些来自框架中间件、哪些来自自己的代码。

本课程顺序是：

1. 第 2 章：手写同步最小循环和业务事件。
2. 第 3 章：加入异步、超时、取消，并解释服务与子 Agent 的边界。
3. 第 4～6 章：用 LangGraph 固化状态、检查点和中断恢复。
4. 第 13 章：根据真实复杂度评估 DeepAgents 的上下文和子 Agent 能力。

这不是排斥框架，而是在引入抽象前先获得判断力。未来换用框架时，你应能指出它替换了本章哪一段循环，以及哪些 RealSight 契约必须保留。

---

## 5. 本章新增类型

### 5.1 ModelAdapter：隔离模型提供商

~~~python
class ModelAdapter(Protocol):
    def decide(
        self,
        *,
        user_input: str,
        session: TaskSession,
        tool_results: tuple[ToolResult, ...],
    ) -> ModelDecision:
        ...
~~~

Agent 运行时依赖这个协议，不依赖某个云模型 SDK。真实适配器以后负责：

- 把工具描述转换为提供商需要的 schema。
- 把会话与工具结果转换为模型消息。
- 把提供商返回的 tool call 转成项目内 `ToolCall`。
- 把模型文本转成 `ModelDecision(final_answer=...)`。
- 标准化错误、用量和调用元数据。

本月 MVP 保持 provider-agnostic。模型选择会受预算、可用区域、多模态能力、结构化输出稳定性和隐私要求影响，不应在第二章用一个示例 SDK提前锁死。

### 5.2 ModelDecision：一次决策只能走一条路

`ModelDecision` 包含：

- `kind`：tool_call 或 final_answer。
- `summary`：可以公开记录的简短决策说明。
- `tool_call`：工具决策时必填。
- `final_answer`：最终回答时必填。

`summary` 不是模型私有思维链。企业系统通常记录可解释的操作理由、输入输出摘要和审计字段，不应依赖或向用户暴露不可控的隐藏推理文本。

数据类在 `__post_init__` 中维护形状不变量：tool_call 决策缺少 ToolCall 会失败，final_answer 决策缺少文本也会失败。

### 5.3 ToolSpec 与 ToolRegistry

工具不只是一个 Python 函数。`ToolSpec` 明确保存：

- 稳定名称。
- 给模型和开发者看的职责描述。
- 必填参数。
- 真正执行的 handler。

`ToolRegistry` 是工具白名单。模型即使输出 `delete_everything`，只要该名字没有注册，运行时就返回 TASK_FAILED，而不是寻找同名全局函数或执行任意字符串。

本章用标准库元组描述必填参数；第 4 章会使用 Pydantic 与 Protobuf 固化更完整的类型、范围和跨语言契约。

### 5.4 ToolContext：运行时注入，不让模型伪造

工具需要 TaskSession 和 Belief State，但不应要求模型把整个对象序列化进参数。运行时通过 `ToolContext` 注入可信上下文，模型只提供 `target_id` 等任务参数。

执行前同时检查：

~~~text
tool_call.target_id
== session.target_id
== belief.target_id
~~~

这延续了第一章的目标隔离不变量。模型参数不是可信事实；它也必须经过验证。

### 5.5 RunEvent：稳定的业务事件投影

第一章已经定义 RunEvent，本章只新增事件类型和 `sequence` 字段，没有改名：

| 事件 | 含义 | 未来前端可显示 |
|---|---|---|
| TASK_STARTED | 一次运行开始 | 正在分析任务 |
| MODEL_DECISION | 模型给出结构化下一步 | 正在决定下一步 |
| TOOL_CALL_REQUESTED | 工具请求已通过基本形状检查 | 正在检查证据 |
| TOOL_CALL_COMPLETED | 工具真实执行完成 | 已取得工具结果 |
| ACTION_REQUIRED | 需要视角或用户输入 | 请翻转充电器 / 提供型号 |
| RULE_COMPLETED | 确定性规则已执行 | 已完成规则验证 |
| FINAL_ANSWER | 本轮有可展示输出 | 展示回答 |
| TASK_FAILED | 运行被错误或治理条件终止 | 显示可处理错误 |

`sequence` 在单次 session 事件流中从 1 递增。它不能替代未来全局唯一 event_id，但足以让当前消费者检查顺序、去发现丢包或错误重排。

---

## 6. 两个工具如何复用第一章

### 6.1 inspect_evidence_gaps

输入：

~~~json
{"target_id": "charger-01"}
~~~

运行时注入当前 Belief State，工具复用：

~~~python
missing_evidence_fields(context.belief)
plan_next_actions(context.belief)
~~~

输出包含缺失字段和结构化 Action。工具不向用户直接说“肯定能充电”，也不修改摄像头。它只回答当前证据状态允许回答的问题。

### 6.2 evaluate_mvp_charging

这个工具复用第一章：

~~~python
result = evaluate_compatibility(context.belief)
answer = build_evidence_answer(result)
~~~

它只在证据缺口工具返回 `run_rules` 后被确定性模型策略选择。更强的保护将在后续加入工具前置条件，使运行时自身也能拒绝越过门禁，而不是只依赖模型遵守顺序。

### 6.3 工具输出为什么保持结构化

工具结果同时包含：

- 机器继续决策需要的 `decision`、`rule_name` 和字段。
- 前端或日志需要的状态摘要。
- 最终可展示的证据化回答。

如果工具只返回一句自然语言，下一轮模型必须重新猜测其中的状态，测试也只能做脆弱的字符串匹配。结构化结果用于控制流，自然语言用于展示，两者可以共存但职责不同。

---

## 7. invoke 与 stream

### 7.1 invoke：等待完整运行结果

~~~python
result = agent.invoke(
    session=session,
    belief=belief,
    user_input="这个充电器能给我的笔记本充电吗？",
)
~~~

`invoke()` 内部仍经历多次模型决策和工具调用，只是调用者最后一次性拿到：

- `final_answer`。
- 完整 `events`。
- `failed` 状态。

它适合单元测试、命令行批处理和只关心最终状态的后端调用。

### 7.2 stream：每产生一个业务事件就交给消费者

~~~python
for event in agent.stream(
    session=session,
    belief=belief,
    user_input="这个充电器能给我的笔记本充电吗？",
):
    print(event.sequence, event.event_type)
~~~

`stream()` 返回迭代器。消费者不必等到规则和最终回答全部完成，就能先获得 TASK_STARTED、TOOL_CALL_REQUESTED 或 ACTION_REQUIRED。

在第 14 章中，这些 RunEvent 会被映射到 WebSocket；但本章不需要启动 Web 服务，也不会用 `print()` 冒充网络流。这里验证的是“生产者逐条 yield，消费者逐条处理”的协议。

### 7.3 业务事件流不等于 token 流

三种“流”经常被混淆：

| 流类型 | 粒度 | RealSight 用途 |
|---|---|---|
| token/text delta | 几个字符或 token | 逐字展示模型回答，可选 |
| 框架状态/chunk | 节点状态更新、消息对象 | 调试或框架内部集成 |
| RunEvent 业务事件 | 工具开始、动作请求、规则完成 | 稳定 API、日志和前端进度 |

当前 LangChain 官方文档提供面向消息、工具调用、状态和自定义更新的事件投影。RealSight 不应该让前端直接解析某个框架版本的内部 chunk，而应在后端把它映射成自己的 RunEvent。这样未来从手写循环迁移到 LangGraph 或 DeepAgents，外部 WebSocket 合同不必跟着重写。

### 7.4 invoke 与 stream 必须语义一致

本章的 `invoke()` 直接消费同一个 `stream()`，而不是维护第二套执行逻辑。因此：

- 工具调用顺序一致。
- 失败条件一致。
- 最终回答来自 FINAL_ANSWER 事件。
- 测试可以比较两种入口的事件类型轨迹。

如果 invoke 和 stream 各自复制一套循环，它们很快会出现重试、错误处理或门禁行为不一致。

---

## 8. 可运行 Demo

### 8.1 文件

- 共享领域模型：../../examples/realsight_domain.py
- 第一章证据逻辑：../../examples/ch01_evidence_loop.py
- 第二章 Demo：../../examples/ch02_minimal_agent_stream.py
- 第二章测试：../../tests/test_ch02_minimal_agent_stream.py

### 8.2 运行

在 `outputs/realsight` 目录执行：

~~~powershell
python examples/ch02_minimal_agent_stream.py
~~~

Demo 不需要 API Key，不访问网络，不安装第三方包。

### 8.3 场景 A：证据不足的 stream

初始 Belief State 只有 `source_port_type = USB-C`。事件顺序是：

~~~text
1 TASK_STARTED
2 MODEL_DECISION: 调用 inspect_evidence_gaps
3 TOOL_CALL_REQUESTED: call-001
4 TOOL_CALL_COMPLETED: call-001
5 ACTION_REQUIRED: request_view(back_label)
6 ACTION_REQUIRED: ask_user(device_model)
7 MODEL_DECISION: 当前存在外部输入边界
8 FINAL_ANSWER: 当前证据不足
~~~

这个结果说明：

- 请求与结果用同一个 `call-001` 关联。
- 规则工具没有被调用。
- 两个 Action 被分别投影，未来前端可以用不同组件展示。
- FINAL_ANSWER 只是本轮输出，不代表 checkpoint 已经保存。

### 8.4 场景 B：证据齐全的 invoke

第二个场景预先注入第一章已验证的标签和虚构设备资料。事件类型为：

~~~text
TASK_STARTED
MODEL_DECISION
TOOL_CALL_REQUESTED
TOOL_CALL_COMPLETED
MODEL_DECISION
TOOL_CALL_REQUESTED
TOOL_CALL_COMPLETED
RULE_COMPLETED
MODEL_DECISION
FINAL_ANSWER
~~~

最终结果包含：

~~~json
{
  "failed": false,
  "event_count": 10,
  "decision": "meets_mvp_charging_requirements"
}
~~~

回答中的来源会回到 `obs-ch02-label`、`power_profile_parser_v1` 和 `demo-device-db:v1`。Agent 循环没有破坏第一章的证据链。

### 8.5 沿代码主线阅读

建议按下面顺序阅读：

1. `ToolCall`、`ModelDecision`、`ToolResult`。
2. `ToolRegistry.execute()` 的白名单和 target 校验。
3. `DeterministicModelAdapter.decide()` 的固定策略。
4. `MinimalAgent.stream()` 的循环。
5. `MinimalAgent.invoke()` 如何消费相同事件流。
6. 两个工具如何调用第一章函数。
7. `run_demo()` 如何构造不足与完整两种 Belief State。

不要先背完整代码。先观察循环中“谁产生请求、谁执行、谁返回结果、谁决定结束”。

---

## 9. 确定性模型替身与真实模型的边界

### 9.1 它为什么不是 Agent 智能的证明

`DeterministicModelAdapter` 使用固定策略：

~~~text
没有工具结果 -> inspect_evidence_gaps
缺口工具说可以 run_rules -> evaluate_mvp_charging
缺口工具返回外部动作 -> final answer
规则工具完成 -> final answer
~~~

这只是测试替身。它不会理解同义句，不会选择新工具，也不会从开放文本生成参数。把它称为“无需大模型的智能 Agent”是不诚实的。

但它证明了更基础的工程事实：模型边界可替换，工具调用协议可测试，事件流无需依赖云服务才能验证。

### 9.2 接入真实模型时保留什么

未来真实适配器必须保持：

- 输出仍落到 `ModelDecision`。
- 工具名来自 registry 暴露的集合。
- 参数必须结构化并经过运行时校验。
- call_id 必须能够关联请求和结果。
- 模型文本不能绕过规则工具修改决策。
- 提供商异常要转成项目内错误，而不是泄漏 SDK 对象到 API。

可以变化的是：消息格式、模型名称、鉴权、token 用量字段、流式 token 解析和供应商特有重试策略。

### 9.3 工具描述为什么重要

真实模型不会扫描 Python 源码来理解工具。Agent 会把工具名称、说明和参数结构交给模型，模型根据这些信息决定是否产生 ToolCall。因此工具描述应：

- 说明何时调用。
- 说明输入语义与单位。
- 说明返回值，不承诺不存在的能力。
- 避免两个工具职责高度重叠。
- 避免把用户不可控数据写进工具说明。

本章的工具只有两个，容易区分。以后工具变多时，工具选择本身会成为需要评测的能力。

---

## 10. 面向 2026 企业项目的基本治理

“模型会调工具”只是起点。企业系统更关心错误是否可控、过程是否可观察、权限是否最小化。

### 10.1 工具白名单

运行时只执行注册的 ToolSpec。未知工具失败关闭。模型输出不是代码执行许可，也不能拼接为 shell 命令。

### 10.2 参数与上下文双重校验

本章校验必填参数和 target_id。第 4 章会增加类型、枚举、范围与 Protobuf 契约。来自模型的 JSON、来自用户的输入、来自 C++ 的 Observation 都位于信任边界上。

### 10.3 最大模型步数

模型可能在两个工具之间循环，或者持续产生相同调用。本章使用 `max_model_steps` 终止不收敛运行，并发送 TASK_FAILED。后续还需要：

- 单工具超时。
- 总任务超时。
- 重试次数。
- 调用预算。
- 用户取消。

这些属于第 3 章的异步治理。

### 10.4 相关标识

当前至少有三种标识：

- `session_id/thread_id`：哪一次 RealSight 任务。
- `call_id`：哪一次工具调用。
- `sequence`：该 session 中事件顺序。

以后还会加入 Observation ID、Evidence ID、run ID 和全局 event ID。每个标识解决不同关联问题，不应拿一个自增数字替代全部语义。

### 10.5 可观察性不等于打印所有内容

事件应该记录工具名、阶段、耗时、状态、错误类别和来源标识，但不应默认记录：

- 完整用户隐私数据。
- API Key。
- 未裁剪的图片内容。
- 超长工具原始输出。
- 模型私有推理文本。

本章 Demo 为了教学直接打印完整结构；正式日志会在第 7 章加入字段筛选、级别和审计策略。

### 10.6 副作用与幂等性

两个工具当前都是只读或确定性计算，重复执行不会改变外部世界。以后“保存证据”“发送摄像头请求”具有副作用，重试时必须使用 call_id 或幂等键防止重复写入与重复动作。LangGraph interrupt 恢复时节点可能重新执行，这一原则尤其重要。

---

## 11. 测试与验收

### 11.1 执行测试

~~~powershell
python -m unittest discover -s tests -v
~~~

当前实际结果：28 项全部通过，其中第一章 16 项、第二章 12 项。

### 11.2 第二章可执行验收表

| 场景 | 预期 | 测试状态 |
|---|---|---|
| 证据不足 | 产生两个 ACTION_REQUIRED，不运行规则 | 通过 |
| 证据完整 | 先检查缺口，再运行规则并最终回答 | 通过 |
| 工具请求与结果 | 同一 call_id 成对出现 | 通过 |
| 事件顺序 | sequence 从 1 严格递增 | 通过 |
| invoke 与 stream | 使用相同事件协议和执行语义 | 通过 |
| session/belief target 不同 | 模型调用前 TASK_FAILED | 通过 |
| 模型请求未注册工具 | 失败关闭，不执行任意函数 | 通过 |
| ToolCall target 不同 | 工具执行前失败 | 通过 |
| 模型不收敛 | 达到 max_model_steps 后失败 | 通过 |
| ModelDecision 形状非法 | 构造时拒绝 | 通过 |
| 模型适配器抛出异常 | 转换为 TASK_FAILED | 通过 |
| 工具 handler 抛出异常 | 转换为带 call_id 的 TASK_FAILED | 通过 |

### 11.3 测试没有证明什么

这些测试没有证明真实模型会稳定选择正确工具，也没有测试网络延迟、并发、取消、持久化或进程崩溃。确定性替身让运行时测试稳定；模型质量必须在接入真实适配器后使用评测数据集单独验证。

---

## 12. 本章能力边界

本章已经实现：

- 同步 Tool Calling 循环。
- 工具注册、必填参数和 target 校验。
- 工具请求/结果的 call_id 关联。
- invoke 聚合结果。
- stream 逐事件输出。
- 事件顺序、失败事件和最大步数。
- 第一章证据工具与规则的复用。

本章尚未实现：

- 真实 LLM 和模型 SDK。
- token 级文本流。
- `asyncio` 异步工具。
- 工具并发、超时、取消和重试。
- LangGraph 状态图。
- checkpoint、interrupt 和恢复。
- C++ Observation 服务。
- WebSocket 网络推送。
- DeepAgents 与子 Agent。

边界的意义不是降低目标，而是防止把“同步生成器逐条 yield”夸大为“企业级可恢复实时系统”。

---

## 13. 课后练习

练习一：在纸上画出证据完整场景中的两个 ToolCall，并标出每个 call_id 对应的 ToolResult。

练习二：新增一个只读工具 `get_belief_summary`。为它写清名称、描述、必填参数和结构化返回值，不允许返回任意对象。

练习三：让一个测试模型重复调用 `inspect_evidence_gaps`，把 `max_model_steps` 改为 2，观察最后一个 TASK_FAILED 的 sequence。

练习四：修改 ToolCall 的 target_id 为 charger-02，解释为什么运行时不能相信模型传入的目标。

练习五：给 RunEvent 增加 `event_id` 和 `occurred_at` 设计草案。思考哪些字段由业务产生，哪些由基础设施产生。暂时不要修改正式类型，第 4 章统一固化。

练习六：解释下面两句话的区别：

> 模型生成了工具调用。

> Agent 运行时已经执行完工具。

练习七：列出 `stream()` 在未来映射到 WebSocket 时，哪些字段可以给用户看，哪些只适合内部日志。

练习八：写一个 ModelAdapter 测试替身，让它请求未知工具，确认系统失败关闭；不要通过在 registry 中偷偷注册该工具让测试“变绿”。

---

## 14. 本章小结与第 3 章桥接

本章把第一章的确定性证据逻辑放进了最小 Agent 外循环。现在可以清楚描述一次运行：

~~~text
用户输入
-> ModelAdapter 产生结构化决策
-> ToolRegistry 校验并执行 ToolCall
-> ToolResult 通过 call_id 返回
-> 模型再次决策
-> 运行时产生有序 RunEvent
-> 最终回答或失败事件
~~~

我们没有用 DeepAgents 隐藏这段循环，也没有把确定性模型替身冒充成真实 AI。`invoke()` 和 `stream()` 共享一套执行语义，业务事件与框架 chunk、token 流被明确区分，第一章的 target 隔离和证据来源仍然成立。

第二章回答的是：

> 一个最小 Agent 到底怎样决定、调用工具、接收结果并让外部看见过程？

第 3 章将回答：

> 当观察工具需要等待摄像头、资料工具需要网络 I/O，任务怎样异步执行、超时、取消和收束？C++ 感知服务为什么仍然不是子 Agent？

下一章会把同步 handler 演进为异步边界，引入 `asyncio`、任务取消、超时和受控并发，并继续复用 `TaskSession`、`ToolCall`、`ToolResult` 与 `RunEvent`。它仍不会提前接入真实摄像头；先把慢工具和失败治理做对，再连接 C++ 实时运行时。

---

## 参考资料

- [LangChain 官方：Tools](https://docs.langchain.com/oss/python/langchain/tools)
- [LangChain 官方：Agents](https://docs.langchain.com/oss/python/langchain/agents)
- [LangChain 官方：Event streaming](https://docs.langchain.com/oss/python/langchain/event-streaming)
- [DeepAgents 官方概览](https://docs.langchain.com/oss/python/deepagents/overview)
- [LangGraph 官方：Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [深度研搜第 2 章：DeepAgents 快速入门与流式解析](https://github.com/didilili/ai-agents-from-zero/blob/main/%E5%AE%9E%E6%88%98%E9%A1%B9%E7%9B%AE-%E6%B7%B1%E5%BA%A6%E7%A0%94%E6%90%9C/2-DeepAgents%E5%BF%AB%E9%80%9F%E5%85%A5%E9%97%A8%E4%B8%8E%E6%B5%81%E5%BC%8F%E8%A7%A3%E6%9E%90.md)
