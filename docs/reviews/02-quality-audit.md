# 第 2 章质量鉴定报告

鉴定对象：

- 正文：../chapters/02-minimal-agent-and-event-stream.md
- 共享领域模型：../../examples/realsight_domain.py
- Demo：../../examples/ch02_minimal_agent_stream.py
- 测试：../../tests/test_ch02_minimal_agent_stream.py
- 前置章节：../chapters/01-realsight-active-perception.md

鉴定日期：2026-08-01

## 1. 总体结论

第 2 章通过逻辑、技术栈、小 Demo 和前后桥接四项门禁，可以进入用户评审。它完成的是**同步、provider-agnostic 的最小 Tool Calling 与业务事件流**，没有宣称已经接入真实模型、异步 I/O、LangGraph 或中断恢复。

在正式判定前，反例审核发现并修复了两项协议问题：

1. `ModelDecision` 原本只检查必填分支，没有禁止同时携带 ToolCall 和 FinalAnswer；现已强制两种分支互斥。
2. 模型适配器或工具抛出 `RuntimeError` 时原本可能逃出事件协议；现已统一转换为 TASK_FAILED，并保留错误类型及工具 call_id。

修复后第二章测试由 10 项增至 12 项，全项目 28 项全部通过。

## 2. 逻辑鉴定

结论：通过。

检查结果：

- 第一章的固定函数调用被清楚地包裹为“模型决策 -> 运行时校验 -> 工具执行 -> 结果回传 -> 再决策”循环。
- 正文明确区分模型、Agent 运行时、工具、规则和工作流，没有把大模型等同于 Agent。
- ToolCall 被定义为请求，只有 ToolRegistry 才执行 handler，职责边界成立。
- 请求与结果使用同一 call_id，能够支持未来重复工具名和并发调用的关联需求。
- ToolCall/FinalAnswer 两种 ModelDecision 形状互斥，非法状态在构造时失败。
- session、belief 和工具参数的 target_id 必须一致，延续第一章目标隔离不变量。
- 未注册工具、非法目标、不收敛模型、模型异常和工具异常都失败关闭。
- `invoke()` 消费同一个 `stream()`，没有复制第二套运行逻辑。
- 证据不足时不会运行规则；证据齐全时先检查缺口，再运行第一章确定性规则。

残余限制：ToolRegistry 尚未声明“某工具只允许在哪个状态调用”的前置条件。任意真实模型若直接调用规则工具，规则本身仍会因证据不足返回 `insufficient_evidence`，不会产生错误兼容结论；但 ACTION_REQUIRED 的投影可能不完整。正式 capability/precondition 门禁将在第 4 章契约和第 7 章治理中补充。

## 3. 技术栈鉴定

结论：通过。

正文对技术与概念说明了“是什么、为什么使用、何时替换”：

| 技术或接口 | 本章定位 | 后续演进 |
|---|---|---|
| Python Protocol/dataclasses | 无依赖 ModelAdapter 和结构化消息 | 第 4 章用 Pydantic 固化边界 |
| Iterator/yield | 同步业务事件流 | 第 3 章演进为异步流 |
| ModelAdapter | 隔离模型提供商 | 接入真实模型时只替换适配器 |
| ToolRegistry | 白名单、参数和目标校验 | 第 4、7 章增加 schema、权限和治理 |
| RunEvent | 框架无关的业务事件 | 第 14 章映射到 WebSocket |
| DeepAgents | 本章不引入 | 第 13 章按复杂度评估 |

正文引用 LangChain Tools、Agents、Event Streaming、LangGraph 和 DeepAgents 官方资料，并明确官方框架事件需要映射为项目自有 RunEvent，而不是泄漏给前端。

需要诚实保留的环境说明：课程目标为 Python 3.12，本次机器实际使用 Python 3.13.13 完成验证。代码只使用 3.12 可用语法与标准库，但尚未在独立 3.12 解释器上执行；第 8 章建立 `uv` 锁定环境后补正式兼容验证。

## 4. Demo 鉴定

结论：通过。

Demo 足够小且可运行：

- 无第三方依赖、无 API Key、无网络请求。
- 确定性模型替身被明确标注，不冒充 LLM。
- 场景 A 逐条 stream 8 个事件，在两个 ACTION_REQUIRED 后收束。
- 场景 B invoke 聚合 10 个事件，包含两次工具调用、RULE_COMPLETED 和证据化回答。
- 最终回答仍可追溯到 `obs-ch02-label`、解析工具和设备资料源。

实际命令：

~~~powershell
python examples/ch02_minimal_agent_stream.py
python -m unittest discover -s tests -v
~~~

结果：Demo 正常退出；28/28 项全量测试通过，其中第二章 12 项。

第二章测试覆盖：

1. 证据不足产生 Action，不运行规则。
2. 证据完整调用规则并回答。
3. 请求/结果 call_id 对应。
4. sequence 严格递增。
5. invoke 与 stream 语义一致。
6. session/belief target 不一致。
7. 未注册工具失败关闭。
8. ToolCall target 不一致。
9. 最大模型步数。
10. ModelDecision 分支形状不变量。
11. 模型适配器异常转 TASK_FAILED。
12. 工具异常转 TASK_FAILED。

## 5. 前后桥接鉴定

结论：通过。

### 与第 1 章

- 复用 `TaskSession`、`BeliefState`、`Action` 和 `RunEvent`，没有另造平行领域模型。
- 复用证据缺口、Action 规划、USB-C 规则和证据化回答。
- target 隔离和证据来源链在 Agent 外循环中继续生效。
- RunEvent 只增加 `sequence` 和事件枚举，没有改名。

### 与第 3 章

- 同步 `ModelAdapter` 与 ToolHandler 清楚暴露了待异步化边界。
- `max_model_steps` 为后续总超时、单工具超时和取消治理提供位置。
- 当前事件迭代器可以演进为 async iterator，而不改变事件含义。
- 第 3 章可以用模拟慢感知服务解释“外部服务不是子 Agent”。

### 与第 4～14 章

- ToolCall、ToolResult 和 RunEvent 将成为 Pydantic/Protobuf 契约输入。
- `thread_id == session_id` 为 LangGraph 状态和检查点提供稳定键。
- call_id 和 sequence 为日志、重试、WebSocket 与幂等性保留了关联语义。
- 框架内部事件到 RunEvent 的适配边界已经定义。

## 6. 最终门禁

| 门禁 | 判定 | 保留风险 |
|---|---|---|
| 逻辑 | 通过 | 工具状态前置条件尚未进入 registry |
| 技术栈 | 通过 | Python 3.12 目标环境尚未实际锁定验证 |
| 小 Demo | 通过 | 确定性模型替身，不代表真实模型质量 |
| 前后桥接 | 通过 | 第 3 章异步化时必须保持事件语义一致 |
| 自动化测试 | 通过，第二章 12/12，全量 28/28 | 未覆盖网络、并发、取消和持久化 |

是否允许进入用户评审：是。

是否自动生成第 3 章：否。按照逐章交付约束，应等待用户确认或提出修改。
