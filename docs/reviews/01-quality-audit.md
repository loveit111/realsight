# 第 1 章质量鉴定报告（修订版）

鉴定对象：

- 正文：../chapters/01-realsight-active-perception.md
- 共享领域模型：../../examples/realsight_domain.py
- Demo：../../examples/ch01_evidence_loop.py
- 测试：../../tests/test_ch01_evidence_loop.py
- 原始依据：D:/Desktop/留校/章节1.docx
- 外部审计：用户提供的 pasted-text.txt

鉴定日期：2026-08-01

## 1. 先给结论

修订后的第 1 章通过“本章逻辑、技术栈定位、小 Demo、章节桥接”四项门禁，可以定稿并进入第 2 章。但通过范围必须写清：本章只验证证据循环的**逻辑切片**，没有验证 LangGraph 中断、检查点和跨进程恢复。恢复能力仍是第 5～6 章的待交付项。

原质量报告将四项都写为无保留“通过”，确实过于乐观。主要问题不是章节方向错误，而是审核没有用反例攻击代码，也没有区分“正文描述了目标机制”和“Demo 已实现该机制”。

## 2. 对外部审计的批判性反思

### 2.1 完全接受并已修正

1. **跨目标污染**：原 `add_label_evidence()` 未检查 Observation 与 Belief State 的 `target_id`。现已在任何状态修改前拒绝不匹配输入，并加入“拒绝后状态不变”测试。
2. **Belief State 不互斥**：原实现可能让同一字段同时出现在 confirmed/unknown，且显式 conflict 被误当作 confirmed。现已定义状态转移表、运行时不变量检查和 5 项转移测试。
3. **架构绕过 LangGraph**：正文图已改为 `C++ Observation -> Observation 接收节点 -> 视觉证据节点 -> Evidence Ledger -> 验证节点`。
4. **标识语义不清**：MVP 明确并强制 `thread_id == session_id`。多线程映射留到确有需求时再增加。
5. **USB-C 结论命名过宽**：删除宽泛的 `compatible` 总结字段，改为 `meets_mvp_charging_requirements_v1`；规则至少检查所需电压档位及该档位功率。
6. **证据来源链不完整**：现在保存原始 OCR、解析后的 `power_profiles`、计算得到的最大功率，并通过 `derived_from` 连接；最终回答输出 Evidence ID、Observation/工具/资料来源。
7. **Demo 能力被夸大**：正文和代码 docstring 都把它限定为逻辑切片，真实中断恢复明确后移。
8. **技术栈顺序过早**：第 2 章改为手写最小 Tool Calling 循环；第 4～6 章学习 LangGraph；第 13 章再评估 DeepAgents。
9. **选型边界不足**：新增模型、OCR、资料源、图像引用和 DeepAgents 的“暂定/暂不选定”表。
10. **缺少可执行验收表**：正文加入场景、预期和当前验证状态，恢复场景诚实标记为待实现。

### 2.2 部分接受

外部审计建议全文压缩 15%～20% 并把完整 Demo 移到五个概念之后。重复论证确实需要控制，因此修订时删改了部分宽泛表述，并在第 6.7 节加入首次运行入口；但没有机械压缩到固定比例，也没有把完整拆解从第 11 节整体搬走。原因是本课程要求完整章节深度，架构、边界和验收仍需要集中说明。采用“概念后先运行，后文再拆解”的折中更适合学习节奏。

### 2.3 不盲目采纳的部分

“恢复后必须保留 target_id”是正确验收要求，但不能为了让第一章报告好看而在纯 Python 单进程脚本里伪造一个名为 resume 的函数。该项继续保留为第 5～6 章的真实集成测试，不计入本章通过数量。

## 3. 逻辑鉴定

结论：通过（限第 1 章职责范围）。

- 任务主线仍为“问题 -> 证据计划 -> 缺口 -> Action -> Observation -> Evidence -> Belief State -> 确定性规则 -> 证据化回答”。
- Observation 写入前校验目标身份，避免不同商品证据混合。
- confirmed、probable、unknown、conflict 对同一字段互斥；冲突不会被一条新证据自动解除。
- “证据缺失”和“已有反证”继续使用不同决策语义。
- USB-C 规则不再把任意最大功率等同于目标电压档位可用。
- C++ 是实时服务，Agent 是低频决策者，规则引擎是确定性组件，三者职责无冲突。

未通过但不属于本章实现范围：真实摄像头、OCR、LangGraph checkpoint/interrupt、进程重启恢复、C++/Python 通信。

## 4. 技术栈鉴定

结论：通过。

正文已说明每项技术“是什么、为什么使用、在哪章实现”，并修正了学习顺序：

| 技术层 | 本章定位 | 实现章节 |
|---|---|---|
| Python 3.12/dataclasses/unittest | 无依赖领域模型与测试 | 第 1～3 章 |
| 最小 Tool Calling/事件流 | Agent 基础执行协议 | 第 2 章 |
| asyncio、超时、取消 | 异步边界 | 第 3 章 |
| Pydantic/Protobuf | 正式契约 | 第 4 章 |
| LangGraph | 状态、检查点、中断恢复 | 第 4～6、13 章 |
| C++20/OpenCV/gRPC | 实时感知与跨语言事件 | 第 9～10 章 |
| DeepAgents | 是否需要高级 harness | 第 13 章评估 |
| FastAPI/WebSocket | 外部任务和实时进度 | 第 14 章 |

模型提供商、OCR 实现和远程 artifact 存储没有被假装“已经选定”，替换边界已写入正文。

## 5. Demo 鉴定

结论：通过（逻辑切片），中断恢复不适用。

实际运行：

~~~powershell
python examples/ch01_evidence_loop.py
python -m unittest discover -s tests -v
~~~

结果：Demo 正常退出；16/16 项测试通过。

关键覆盖：

1. 初始缺口生成 back_label 与 ask_user 动作。
2. 原始 OCR、档位、最大功率形成可遍历派生链。
3. target 不匹配被拒绝且不污染状态。
4. 协议未知返回 insufficient_evidence。
5. 65W/45W 且 20V 档匹配时满足 MVP。
6. 30W/45W 时不满足 MVP。
7. 最大功率足够但缺少 20V 档时不满足 MVP。
8. 端口不匹配、协议不匹配和证据冲突。
9. 关键 Belief State 状态转移。
10. `thread_id == session_id` 约束。

Demo 没有 LangGraph、checkpoint、序列化重载、真实等待、模型调用或事件流循环，因此报告不再称其为“可恢复 Demo”。

## 6. 章节桥接鉴定

结论：通过。

- 向前：第一章保留原始 Word 文档的项目目标、四阶段演进、职责边界、五个领域概念、主动感知、USB-C 案例和能力边界。
- 向后到第 2 章：共享 `TaskSession`、`Action`、`RunEvent` 和 Evidence 逻辑；第 2 章只新增最小 Agent 循环与事件轨迹。
- 向后到第 4～6 章：`thread_id == session_id` 和 Action/Belief State 成为状态图与检查点输入。
- 向后到第 10 章：Observation 的 target 校验成为 gRPC 接收边界的不变量。
- 向后到第 12～13 章：证据派生链与 MVP 规则可直接进入资料检索和主工作流。

## 7. 最终门禁

| 门禁 | 判定 | 保留风险 |
|---|---|---|
| 逻辑 | 通过 | 冲突解除策略尚未实现，后续需显式 revalidation |
| 技术栈 | 通过 | 模型/OCR/DeepAgents 仍是有意保留的决策点 |
| 小 Demo | 通过 | 仅逻辑切片，不代表中断恢复 |
| 前后桥接 | 通过 | 第 2 章必须复用共享类型，不能另造平行事件模型 |
| 自动化测试 | 通过，16/16 | Python 3.12 目标环境将在第 8 章锁定；本次宿主解释器为 3.13 |

允许进入第 2 章：是。用户已明确要求继续生成第 2 章。
