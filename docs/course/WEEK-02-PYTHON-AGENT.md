# 第二周：Python Agent 完整闭环

这一周只研究低频决策链，不碰 C++ 像素实现。你的目标不是背 LangGraph API，而是能拿一段
OCR 文本，从 `Observation` 开始，手工推导出 `Evidence → Belief → Action → verdict`，再到
代码里指出每一步由谁完成、为什么不能越权。

## Day 7：Observation 到 Evidence——把“机器看见”变成“可审计事实候选”

### 今天结束时你必须会

- 区分图片质量分、OCR 文字置信度和 Evidence 置信度。
- 解释 `TextRegion`、`RecognitionDocument`、`Evidence`、`EvidenceGap` 的关系。
- 沿真实代码讲清功率、输出档位和协议的解析过程。
- 为一种新的识别失败写测试，并判断它是异常还是业务缺口。

### 1. OCR 到底做了什么

OCR 通常包含两个步骤：文本检测找到图片中的文字区域，文本识别把区域像素转成字符。
RealSight 把第三方 OCR 的不同输出统一成：

```text
TextRegion = 文字 + OCR confidence + 归一化多边形 + region_id
RecognitionDocument = 多个 TextRegion + provider metadata
```

OCR 并不知道 `65W` 是充电器最大功率还是宣传文字，也不知道 `20V⎓3.25A` 应算出 65W。
这些是 `VisionEvidenceAgent` 的字段提取职责。这个分层让 OCR 后端可以替换，而功率规则仍然
可独立测试。

### 2. 三种置信度绝不能混

| 名称 | 回答的问题 | 产生者 | 不能被解释成 |
|---|---|---|---|
| Observation quality | 这帧是否清晰、曝光合理、反光可控 | C++ 质量评估 | 文字正确概率 |
| OCR confidence | OCR 对某个文字框的字符识别有多确信 | OCR provider | 瓦数含义正确概率 |
| Evidence confidence | 当前字段候选结合来源后的保守可信度 | VisionEvidenceAgent | USB-C 兼容概率 |

代码构造 Evidence 时采用相关文字区域的最低置信度，并结合阈值决定 `confirmed` 或
`probable`。取最低值是一种保守聚合：`20V` 很清楚但 `3.25A` 模糊，乘积不能继承较高的
那个分数。它不是统计校准后的真实概率，不能声称“0.91 就有 91% 正确率”。

### 3. `VisionEvidenceAgent.extract()` 的真实顺序

按下面顺序阅读 `python/src/realsight/vision/evidence_agent.py`：

1. 先检查 Observation 是否 `accepted`、是否有图片 artifact、请求的 feature 是否支持。
2. 调用 `TextRecognizer.recognize(image_path)`，后端异常统一转成 `RecognitionFailure`。
3. 空文字或低 OCR 置信度形成 gap，不伪造 Evidence。
4. `charger_label` 保存可回看的原始拼接文本。
5. `charger_max_power_w` 优先从完整电压/电流档位计算最大值，也读取明确瓦数。
6. 直接瓦数与完整档位计算结果矛盾时，返回 `conflicting_power_candidates`。
7. `charger_protocol` 从 USB PD/PPS 等标记形成稳定协议集合；未发现不是“不支持”。
8. 所有 Evidence 保存 `derived_from` 和 metadata，使结果能追到 Observation 和文字框。

这里的关键业务语义是：**未发现协议文字属于未知，不属于否定**。标签没拍全、OCR 漏字和
设备确实不支持协议，在单张图片上可能有同样输出，因此系统只能请求补证据。

### 4. 为什么同时保存 Evidence 和 EvidenceGap

Evidence 表示“有一个带来源的字段候选”；gap 表示“这次尝试为什么没有得到所需字段，
以及建议怎样补”。如果只返回空 tuple，Planner 无法区分：图片被拒、OCR 崩溃、功率冲突、
协议没找到还是调用者请求了不支持的 feature。

gap 不是异常的垃圾桶。判断方法：

- 外部后端不可用、图片无法读取、返回数组长度自相矛盾：适配器/系统异常。
- 图片质量不够、文字为空、字段未找到、候选冲突：现实信息不足，形成结构化 gap。
- 程序员把笔记本 Observation 传给充电器提取器：契约/调用错误，立即拒绝。

### 5. 今日阅读与跟踪表

依次阅读：

1. `TextRegion` 的坐标和 confidence validator。
2. `RecognitionDocument` 的 region ID 唯一性。
3. `VisionExtraction` 为什么能同时含 Evidence 与 gaps。
4. `extract()` 的早退分支。
5. `_extract_max_power()`、`_extract_protocols()`、`_build_evidence()`。
6. `python/tests/test_ch11_vision_evidence_agent.py`，把每个测试对应到一个分支。

为每个分支写五列：输入、是否调用 OCR、Evidence、gap、是否值得重拍。

### 6. 实验：先手算，再运行

准备 fake recognizer，分别返回：

1. `OUTPUT: 20V 3.25A USB PD`：应得到 65W 和 `usb_pd`。
2. `65W`，confidence 低于最低阈值：不能成为 confirmed。
3. `65W` 与 `20V 2.25A`：直接值 65W、档位 45W 冲突，应产生 gap。
4. `20V 3.25A / 9V 3A`：最大输出取 65W，不把瓦数相加。
5. `USB-C` 但没有 `PD/PPS`：不能推断支持 USB PD。
6. 空 regions：保留可行动缺口。

```powershell
uv run --locked pytest python/tests/test_ch11_vision_evidence_agent.py -q
uv run --locked pytest python/tests/test_portfolio_readiness.py `
  -k "low_ocr_confidence or paddle_adapter" -q
```

新增一个测试：OCR 返回重复 `region_id` 或请求未知 feature。先预测由 Pydantic 还是
VisionEvidenceAgent 拒绝，再写断言。

### 7. 常见逻辑陷阱

- 用整张图的平均 OCR confidence 覆盖关键低置信度数字。
- 看到多个瓦数就取最大值；其中可能是单口、总功率或宣传值。
- 把电压和电流跨不同文字区域随意配对，形成不存在的档位。
- 把 `USB-C` 接口外形当作 `USB PD` 协议证据。
- OCR 返回空结果时抛 500，导致用户无法知道需要重拍。
- 只在自然语言日志保存原文，没有 region ID 和 Observation 来源。

### 8. 今日答辩

1. 一张质量分 0.95 的图，为什么仍可能没有任何 confirmed Evidence？
2. OCR confidence 0.99，为什么字段仍可能冲突？
3. “没识别到 PD”与“不支持 PD”有什么根本区别？
4. 65W 与 20V×3.25A 一致时，metadata 应保存哪些推导信息？
5. 什么错误应该变成 `RecognitionFailure`，什么应该变成 `EvidenceGap`？

合格标准：给一段标签文字，五分钟内手算所有候选、status、gap 和来源链。

---

## Day 8：规格检索与确定性规则——让结论可重复、可解释

### 今天结束时你必须会

- 解释本地规格目录为什么只做保守精确匹配。
- 说清充电器 Evidence 与笔记本 specification Evidence 为什么属于两个 target。
- 不看代码写出规则优先级，并手算 30W、45W、65W、100W。
- 区分业务 verdict 与 `RuleInputError`。

### 1. 为什么需要第二类证据

充电器标签回答“这个充电器声称提供什么”；兼容判断还需要“指定笔记本要求什么”。用户
输入 `ExampleBook 13` 只是一条型号 Evidence，不能直接等于最低功率、推荐功率和协议。
规格目录命中后再产生三条 `local_spec_catalog` Evidence：

```text
laptop_minimum_power_w
laptop_recommended_power_w
laptop_required_protocol
```

这三条的 `target_id` 是笔记本，不是充电器；规则调用者显式分两组传入，避免现实对象串线。

### 2. 目录为什么不用“智能模糊匹配”

`LocalSpecificationCatalog` 只统一大小写和连续空白，然后做精确型号/别名索引。原因是相似
型号可能有不同年份、屏幕、CPU 和供电要求。模糊匹配“看起来更方便”，却可能静默选错
记录，直接制造错误正结论。

查询有四种结构化状态：`found`、`not_found`、`ambiguous`、`invalid_query`。歧义时返回候选
而不取 JSON 中第一条；未命中也不让模型凭常识补规格。当前目录是教学夹具，不是厂商知识库。

### 3. 规则为何按固定顺序

`UsbCCompatibilityRules.evaluate()` 的顺序是：

```text
target/类型合法性
→ confirmed 值是否冲突
→ 必需字段是否缺失
→ 协议集合是否满足
→ 功率是否低于最低值
→ 功率是否低于推荐值
→ 已知条件满足
```

冲突优先于缺失，因为同字段出现两个 confirmed 值时，不能把它悄悄归为普通缺项；协议
优先于功率，因为 100W 但协议不符仍不能由功率掩盖。只有 confirmed Evidence 能进规则，
probable 保留为缺口。

### 4. 五类主要 verdict

假设教学目录要求最低 45W、推荐 65W、协议 `usb_pd`：

| 充电器证据 | verdict | 原因 |
|---|---|---|
| 30W + USB PD | `insufficient_power` | 低于最低 45W |
| 45W + USB PD | `limited_power` | 满足最低，低于推荐 |
| 65W + USB PD | `conditions_met` | 已知协议与推荐功率满足 |
| 100W + USB PD | `conditions_met` | 功率充足，但不是“已实测成功” |
| 100W + 非 USB PD | `protocol_mismatch` | 协议先于功率比较 |

此外还有 `evidence_conflict` 与 `insufficient_evidence`。即使 `conditions_met`，结果仍携带线缆、
实际端口/系统状态、PD 协商和多口共享功率等未知边界。

### 5. 相同值、不同值和规范化

同字段多条 confirmed Evidence 不一定冲突。两条都是 65W，可共同支持一个值；协议列表
`["usb_pd", "pps"]` 与 `["pps", "usb_pd"]` 语义相同，规则按集合规范化比较。真正不同的
confirmed 值或显式 conflict 才阻断规则。

这里还有 Python 陷阱：`True` 是 `int` 的子类，所以功率转换明确拒绝 bool；字符串
`"65"` 也不被静默当成数字。输入形状错误是 `RuleInputError`，表示上游程序违反契约，
不是设备“不兼容”。

### 6. 今日代码阅读与实验

阅读：

1. `compatibility/catalog.py::lookup()` 与 `_normalize_term()`。
2. 测试数据 `test-data/ch12/laptop-specifications.json`。
3. `compatibility/rules.py::evaluate()`。
4. `_resolve_field()`、`_canonical_value()`、`_power_value()`。
5. `UsbCCompatibilityResult` 的跨字段校验。

```powershell
uv run --locked pytest python/tests/test_ch12_specifications_and_rules.py -q
```

修改个人测试夹具完成七个案例：上表五个、probable 65W、两条不同 confirmed 功率。每次
断言 verdict、used evidence IDs、missing/conflicting fields 和 unknown boundaries，不能只
断言 summary 包含某个词。

再建立一个歧义别名目录：两台设备共享同一 alias，证明查询返回两个 candidate 而不是第一条。

### 7. 常见陷阱

- 用户说了型号，就把型号对应规格当作 confirmed，跳过资料来源。
- 用编辑距离自动选择“最像”的笔记本。
- 规则只比较功率，忽略协议。
- probable 值在传给规则前被无声升级为 confirmed。
- 同字段重复 Evidence 一律判冲突，或反过来只保留最后一条。
- 把 `conditions_met` 翻译成“保证可以快充”。

### 8. 今日答辩

1. 为什么 catalog not_found 是业务结果，JSON 文件损坏却应是启动错误？
2. 为什么 100W 不必然优于 65W，也不证明能够协商？
3. 两条协议列表只是顺序不同，为什么不应冲突？
4. probable 65W 为什么不能进入规则？下一步应该是什么？
5. 如果规格目录数据本身错了，当前项目能否自动发现？

---

## Day 9：Planner 与受限工具——模型只能在护栏内选路

### 今天结束时你必须会

- 根据任意 `MainAgentState` 列出允许 Action 集合。
- 比较 `DeterministicPlanner` 与 `OpenAIPlanner` 的相同契约和不同风险。
- 解释工具 schema、函数调用、参数验证和状态白名单的四道边界。
- 用假模型响应测试越权、坏 JSON、多调用和无调用。

### 1. Planner 不执行动作

Planner 的签名是 `state → Action`。它不打开摄像头、不做 OCR、不查文件、不运行兼容规则。
Action 只描述下一步，由 LangGraph 路由到拥有相应能力的节点。这样可以独立替换 Planner，
而不会让模型拿到任意 Python 或 shell 执行权。

### 2. 状态到允许动作的真实表

阅读 `available_action_types()`，自己补全下面的表：

| 当前状态 | 允许动作 |
|---|---|
| 缺 charger confirmed 规则字段 | `REQUEST_VIEW` |
| 缺笔记本型号，或查询未得到规格 | `ASK_USER` |
| 型号已确认但尚未查询 | `RETRIEVE_KNOWLEDGE` |
| 充电器字段与规格 Evidence 都齐 | `RUN_RULES` |
| 已有兼容结果 | `GENERATE_ANSWER` |
| completed/cancelled/failed | 无 |

某些状态可能同时允许观察和询问用户。离线 Planner 用固定优先级保证可重复；模型 Planner
可以在当前白名单中选择，但不能调用表外工具。

### 3. OpenAI Planner 的四层约束

1. **允许工具集合**：根据当前 state 动态计算，而不是总把五种工具都暴露。
2. **JSON Schema**：每个函数参数有必填、类型和 `additionalProperties=false`。
3. **响应形状**：必须恰好返回一个 function call，普通自然语言不算 Action。
4. **二次转换**：`tool_call_to_action()` 再检查工具名、当前 allowed、target 和字段范围，构造
   严格 Pydantic Action。

模型选择了名为 `run_rules` 的函数不等于规则已经运行；它只是产生 RunRulesPayload。真正
规则节点仍然从 checkpoint 读取 Evidence 并调用纯规则。

### 4. 为什么保留确定性 Planner

它让离线测试、无 API Key 演示和故障定位可重复，也明确表达项目策略。当 OpenAI 模式行为
异常时，可以切回 deterministic：如果仍失败，问题多半在工作流/证据；如果只在模型模式
失败，检查响应或治理成本。这是有价值的对照组。

### 5. 今日阅读路径

1. `_VISION_FIELDS`、`_RULE_FIELDS` 和 `_TOOL_NAMES`。
2. `available_action_types()` 的每个条件。
3. `DeterministicPlanner.plan()` 的优先级。
4. `tool_call_to_action()` 的每种 payload 构造。
5. `_tool_schema()` 与 `_state_summary()`。
6. `OpenAIPlanner.plan()` 对 function call 数量、JSON 和工具名的检查。

为每个 Action 写：前置状态、payload、目标对象、实际执行节点、可能副作用。

### 6. 实验

先用正式测试观察一个合法假调用：

```powershell
uv run --locked pytest python/tests/test_ch13_main_agent_loop.py `
  -k "openai_planner" -q
```

再写四个 fake response：

- 缺视觉 Evidence 时返回 `generate_answer`。
- 恰好允许 `request_view`，但 JSON 参数多出 `shell_command`。
- 返回两个 function calls。
- 只返回“我认为兼容”的自然语言。

四者都必须失败，且不能改变输入 state。再构造合法 `ask_user`，检查生成 Action 的 target 是
笔记本而不是充电器。

### 7. 逻辑陷阱

- 只依赖 prompt 说“不要越权”，没有程序白名单。
- 所有工具一直可见，让模型在证据不足时生成答案。
- 将模型的自然语言理由当作 Evidence。
- 工具调用成功解析后不再验证 target/field。
- 模型重试没有预算，坏响应形成无限成本循环。
- 为了“更智能”删除 deterministic baseline，失去可重复对照。

### 8. 今日答辩

1. 模型能选择 `run_rules`，为什么仍不是模型决定 verdict？
2. 动态工具白名单比只写系统提示强在哪里？
3. 为什么要求恰好一个 function call？
4. 模型返回错误 target ID 时哪一层拒绝？
5. deterministic planner 的工程价值是什么？

---

## Day 10：LangGraph 主循环——状态、路由、中断与重放

### 今天结束时你必须会

- 不看源码画出正式图的所有节点和边。
- 解释“prepare → wait → apply”三段式为何安全。
- 能跟踪一次两次中断的 session status、pending action、iteration 和事件变化。
- 安全增加一个 RunEvent，并验证序号和恢复行为。

### 1. 图不是流程图装饰，而是可执行状态机

`StateGraph(MainAgentState)` 的节点接收已校验 state，返回字段更新；条件边读取 route，选择
下一个节点。checkpoint 在节点边界保存状态，因此节点应返回新快照，不能依赖不可序列化
局部对象或隐藏全局变量。

正式成功路径是：

```text
plan
├─ REQUEST_VIEW → prepare_observation → wait_for_observation
│                                      ⇣ resume
│                 apply_observation → plan
├─ ASK_USER → prepare_laptop_model → wait_for_laptop_model
│                                    ⇣ resume
│              apply_laptop_model → plan
├─ RETRIEVE_KNOWLEDGE → retrieve_specification → plan
├─ RUN_RULES → run_rules → plan
└─ GENERATE_ANSWER → generate_answer → END
```

这比简写的 `plan→interrupt→resume` 多，是因为“等待状态先落盘”和“恢复数据后再产生副作用”
必须显式分开。

### 2. prepare、wait、apply 各自负责什么

**prepare**：保存 pending request/action、把 session 改为等待态、写 ACTION_REQUIRED event。

**wait**：只调用 `interrupt()`。恢复时这个节点可能重新执行，所以绝不能在这里调用 OCR、
gRPC 或扣不可重复外部费用。

**apply**：验证 resume payload、request/target/view，再调用视觉 Agent 或写用户 Evidence，
清理 pending 字段、增加 iteration 和事件。

这是一种重放安全设计。注意它不自动让所有外部调用幂等：若 apply 中途完成 OCR 后进程
在 checkpoint 前崩溃，仍需依靠稳定 ID、外部幂等或更严格事务处理。课程 MVP 要主动说明
这个边界。

### 3. state、event 和 route 的区别

- state 是当前事实快照，如 Belief、pending request、governance usage。
- event 是已经发生的、面向观察者的有序记录，不是另一个事实来源。
- route 是当前节点给图路由用的临时控制字段，不是业务结论。

RunEvent 的 `sequence` 应单调增加。WebSocket 以后靠它补发；如果两个事件重复序号或节点
忘记保留旧 events，客户端会漏消息。

### 4. serializer allowlist 为什么存在

LangGraph checkpointer 需要序列化 Pydantic 模型、Enum、datetime 等。正式代码显式允许
必要类型，不把任意对象 pickle 进数据库。运行时 dependencies 通过闭包存在：Planner、
OCR pipeline、gRPC channel、规则实例和 SQLite connection 都不属于状态。

### 5. 今日逐节点阅读方法

打开 `workflow/main_agent.py::build_main_agent_graph()`，对每个 node 填表：

| 节点 | 必须已有 | 新增/清理字段 | 副作用 | 下一站 |
|---|---|---|---|---|
| plan | 非终态 state | action、route、event、usage | 可选模型调用 | 条件路由 |
| wait | prepared pause | resumed_payload | interrupt | 暂停/apply |
| apply observation | request + resume | belief、gap、iteration | OCR | plan |
| retrieve | model Evidence | lookup/spec Evidence | 本地文件已加载目录查询 | plan |
| rules | 两类 Evidence | compatibility result | 无外部 I/O | plan |
| answer | rule result | final answer、completed | 模板渲染 | END |

然后读图组装的 `add_node`、`add_edge`、`add_conditional_edges`，确认表和真实边一致。

### 6. 实验：两次 interrupt 的状态时间线

```powershell
uv run --locked python examples/ch13_main_agent_loop.py
uv run --locked pytest python/tests/test_ch13_main_agent_loop.py -q
```

在 Day Notes 逐轮记录：session status、iteration、pending action、pending request、confirmed
fields、lookup status、verdict、最后一个 event sequence。

新增一种 `RunEvent` 或给现有事件 metadata 增加不敏感字段，补测试证明：

1. 事件只产生一次。
2. sequence 连续递增。
3. SQLite 序列化能读取。
4. API 安全摘要没有泄露图片或密钥。

再制造低置信度 OCR，观察图为什么重新回到 REQUEST_VIEW；把治理预算设小，证明循环以 failed
终态结束而不是无限重拍。

### 7. 常见陷阱

- 在 wait 节点执行 OCR 或调用摄像头，恢复重放导致重复副作用。
- 直接修改 `state.events.append(...)`，破坏冻结快照和更新语义。
- 路由只看自然语言 reason，不看 ActionType。
- 错误 resume 进入图后才验证，消费掉当前 interrupt。
- 把 dependencies 塞进 MainAgentState。
- 节点异常后仍生成 final answer，掩盖失败。

### 8. 今日答辩

1. 为什么 REQUEST_VIEW 需要三个节点，不能一个节点完成？
2. event 与 state 哪个是事实来源？WebSocket 为何使用 event？
3. 运行时依赖为什么放闭包而不是 checkpoint？
4. 低质量观察为何会再次 plan？什么机制防无限循环？
5. `Command(resume)` 前必须验证哪些标识？

---

## Day 11：TaskService、FastAPI 与 WebSocket——把图变成可调用产品

### 今天结束时你必须会

- 区分 HTTP DTO、应用服务和领域状态。
- 解释 `asyncio.to_thread`、HTTP 201/404/409 和 WebSocket 关闭码。
- 手算 `after_sequence` 重连时会补发哪些事件。
- 写一个 API 错误路径测试，证明错误没有破坏任务。

### 1. 三层为什么分开

**FastAPI DTO**处理不可信 JSON、字段形状和 HTTP 语义；**TaskService**协调创建、查询、恢复、
取消和自动 Observation；**LangGraph/领域模型**维护业务不变量。浏览器请求体不能直接当作
内部 `Command(resume)`，所以 `ResumeTaskRequest.workflow_payload()` 做显式转换，领域层还会
再次验证 request/target。

如果 HTTP 路由直接操作 graph，每个 endpoint 都会重复 thread ID、interrupt 和错误映射；
CLI 或未来队列消费者也无法复用同一个用例层。

### 2. 同步图与 async API

FastAPI 的事件循环适合等待网络。当前 LangGraph、SQLite 和 Python gRPC provider 是同步
调用；在 async endpoint 直接执行会阻塞其他请求，所以使用 `asyncio.to_thread` 放到工作
线程。它只避免阻塞事件循环，不会让 SQLite 自动支持多进程事务，也不会解决 provider
线程安全和无限调用问题。

### 3. HTTP 状态语义

- `POST /tasks` 成功创建返回 201。
- 未知 session 是 404。
- 已取消任务恢复、错误 interrupt、当前状态不接受该操作是 409。
- Pydantic 请求形状错误由 FastAPI 返回 422。

不要把所有异常都变成 500，也不要把内部 Python 栈和绝对路径返回给客户端。

### 4. WebSocket 事件重放

客户端传 `after_sequence=N`，服务读取 checkpoint 中所有 `event.sequence > N` 的事件，再
轮询后续事件。终态 completed/cancelled/failed 发送完剩余事件后用 1000 正常关闭；未知任务
用 4404。这个方案适合单进程教学 MVP，多个 API 实例需要共享事件系统，不能靠各自内存轮询。

例：checkpoint 有 1～8，客户端确认收到 5 后断开，重连 `after_sequence=5`，应收到 6、7、8；
若错误传 8，就不会重发 6～8，客户端自己负责持久化最后确认序号。

### 5. TaskService 的自动观察

配置 provider 时，`_drive_automatic_observations()` 只在 WAITING_OBSERVATION 下调用它；遇到
WAITING_USER 就返回给客户端。每轮从 graph 取真实 interrupt ID、调用 observe，再恢复。
provider 不存在时任务诚实地停在等待观察，不会伪造图片。

`close()` 负责关闭 provider/channel 和 SQLite。应用生命周期若忘记调用，会留下文件锁或
网络资源；正式 `serve.py` 应负责装配与退出清理。

### 6. 今日阅读与实验

阅读：

1. `application/task_service.py` 所有公开方法。
2. `CreateTaskRequest` 与 `ResumeTaskRequest` validators。
3. `_state_response()` 为什么只返回摘要。
4. `_http_error()` 的 404/409 映射。
5. WebSocket 循环和终态集合。

```powershell
uv run --locked python examples/ch14_api_replay.py
uv run --locked pytest python/tests/test_ch14_api.py -q
```

新增一个错误路径测试，任选其一：

- charger/laptop target ID 相同得到 422。
- 错误 interrupt 得到 409，之后正确 interrupt 仍可恢复。
- 取消后 resume 得到 409。
- 未知 session WebSocket 以 4404 关闭。
- `after_sequence` 只补发更大的事件。

测试必须同时断言 HTTP/WebSocket 语义和领域状态未被错误请求污染。

### 7. 常见陷阱

- API 直接返回完整 checkpoint，把 metadata、路径或未来密钥暴露出去。
- 认为 async endpoint 里的同步调用会自动异步。
- 用 200 返回所有失败，客户端无法判断重试策略。
- WebSocket 只推新事件、不支持重放，断网必然丢消息。
- 服务关闭时只关 SQLite，不关 gRPC channel。
- 在多个进程部署后仍把 checkpoint 轮询当作共享事件总线。

### 8. 今日答辩

1. API DTO 与领域模型字段相似，为什么仍要分开？
2. `asyncio.to_thread` 解决和不解决什么？
3. 错误 interrupt 为什么是 409 而不是 404？
4. 客户端从 sequence 5 重连应收到什么？谁保存 5？
5. TaskService 为什么不自己解析 OCR？

---

## Day 12：脱离源码重写最小闭环——证明理解可以迁移

### 今日任务

不打开 `main_agent.py`，在个人学习目录写一个 150～250 行的最小闭环。可以不用 LangGraph，
但必须保留边界：

```text
缺充电器字段 → RequestView
收到 Observation → fake OCR → Evidence/gap → Belief
缺型号 → AskUser
型号 Evidence → catalog lookup
规则输入齐 → deterministic verdict
verdict → 模板答案
```

### 1. 先写协议，不要先写 while

先定义最小类型：MiniObservation、MiniEvidence、MiniBelief、五种 MiniAction、MiniState。
写下不变量：

- 一个 state 中 charger/laptop target 不同。
- probable 不进规则。
- conflicting field 必须阻断。
- RequestView 不等于 Observation。
- final answer 只能从 rule result 生成。
- 每轮最多一个 pending Action。

然后写纯函数：`plan(state)`、`apply_observation`、`apply_model`、`lookup`、`evaluate`、`render`。
最外层 loop 只负责调用这些函数，不在同一个 `if` 中混 OCR、数据库和规则。

### 2. 为什么暂时不用 LangGraph

这项练习要验证你理解的是领域边界，而不是会复制框架 API。没有框架后，若你仍能保持
Action/Observation/Evidence 分离，说明心智模型成立。完成后再对比正式图，写出 LangGraph
额外解决的 checkpoint、interrupt、路由可视化和恢复问题。

### 3. 必须先写的十个测试

1. 初始状态请求观察。
2. accepted Observation 才进入 OCR。
3. 错 request ID 被拒，原 pending request 保留。
4. 低置信度成为 probable/gap，不进规则。
5. 两个 confirmed 不同值成为 conflict。
6. 缺型号询问用户。
7. 目录未命中不猜规格。
8. 协议不符优先于功率。
9. 45W 得到 limited、65W 得到 conditions_met。
10. final answer 包含未知边界和 used evidence IDs。

测试完成前不要加 HTTP、SQLite 或漂亮 CLI。最小闭环的价值在可验证逻辑，不在界面。

### 4. 与正式实现对照

完成后才打开正式文件，逐项回答：

- 正式 Pydantic 比你的 dataclass 多了哪些运行时约束？
- 正式 Planner 的动态白名单避免了哪个分支错误？
- 正式 prepare/wait/apply 如何支持跨进程等待？
- 正式 Evidence metadata 多保存了哪些审计信息？
- 正式 governance 在哪里阻止无限循环？
- TaskService 和 API 分层解决了什么复用问题？

只记录差异，不把正式代码整段复制回练习。

### 5. 第二周综合答辩

用一段 `OUTPUT 20V 2.25A; USB PD`、笔记本最低 45W/推荐 65W，脱稿完成：

1. OCR region 到 45W Evidence 的推导。
2. Evidence status 和 metadata。
3. Belief confirmed/ledger 更新。
4. Planner 从 RequestView 走到 AskUser、Retrieve、RunRules、GenerateAnswer。
5. 最终 `limited_power` 及未知边界。
6. 对应 RunEvent 序列和两个 interrupt。

再回答 15 题：

1. OCR 检测和字段提取的边界是什么？
2. 三种 confidence 分别是什么？
3. EvidenceGap 为什么比空结果有用？
4. 多个瓦数为何不能总取最大？
5. 目录为何拒绝模糊匹配？
6. 规则的固定优先级是什么？
7. probable 为什么是 missing rule input？
8. Planner 为何不执行 Action？
9. 动态工具白名单如何计算？
10. OpenAI 普通文字为何不能成为 Action？
11. prepare/wait/apply 为什么拆开？
12. event sequence 服务什么恢复语义？
13. API 的 404、409、422 各表示什么？
14. WebSocket 怎样断线补发？
15. 最小闭环与正式 LangGraph 的核心差异是什么？

### 6. Week 2 Gate

按 [评分标准](ASSESSMENT-RUBRIC.md) 至少 80 分，并完成：

- 10 分钟逐节点画主图。
- 从原始标签手算 Evidence、Belief、Action、verdict。
- 一个 API 错误路径测试。
- 60 分钟内给最小闭环增加一个受限 Action 与两项测试。

如果不能解释为什么 probable 不进规则、为什么错误 resume 不消费 interrupt、为什么答案由
规则模板生成，不进入 C++ 周；这些边界是后面跨语言调试的坐标系。
