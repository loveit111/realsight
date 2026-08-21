# 第 12 章：资料检索与规则引擎 - 从候选证据到有条件的 USB-C 结论

## 1. 本章目标、前提与产物

第 11 章已经能从充电器背面标签的关键帧中得到可追溯的候选 Evidence，例如：

```text
charger_max_power_w = 65.0
charger_protocol = ["usb_pd"]
来源 = observation ID + OCR 原文区域 + 置信度
```

但用户的问题是“这个充电器能给**我的电脑**充电吗”。仅有充电器一侧事实还不够，系统还
需要知道笔记本的最低/推荐 USB-C 充电功率和所需协议。本章把这些资料也做成 Evidence，
再用不用大模型的确定性规则比较双方条件。

本章结束时，你将能运行这条链路：

```text
第 11 章 charger Evidence
  + 用户确认的 laptop_model Evidence
  -> 本地规格目录精确检索
  -> laptop 资料 Evidence
  -> USB-C 功率/协议规则
  -> conditions_met / limited_power / insufficient_power / ...
  -> 带证据 ID 和未知边界的结果
```

本章交付：

- `python/src/realsight/compatibility/` 正式资料与规则包。
- 本地 JSON 规格目录 `test-data/ch12/laptop-specifications.json`。
- Demo `examples/ch12_specifications_and_rules.py`。
- 九项测试 `python/tests/test_ch12_specifications_and_rules.py`。
- 独立审计报告 `docs/reviews/12-quality-audit.md`。

学习前提：你理解第 11 章的 `Evidence`、`EvidenceGap`、`confidence` 与 `status`；知道
`confirmed` 代表当前来源可用，不代表所有真实世界条件已经验证。

## 2. 不要把“标签读到 65W”直接翻译成“肯定能充”

设想背标写着：

```text
Output: 5V=3A 9V=3A 15V=3A 20V=3.25A
65W Max USB PD 3.0 PPS
```

第 11 章可以形成 65W 和 USB PD 候选。可它仍无法知道：

- 用户的笔记本是哪一台；同一系列可能有不同充电要求。
- 笔记本是否要求 USB PD，还是存在专用供电模式。
- 45W 是否能在轻负载下工作，但不够推荐功率。
- 用户的线缆、端口、系统状态和充电器多口共享功率是否满足。

因此本章不输出“可以充电”这种过度承诺，而是输出六类**静态条件结论**：

| verdict | 含义 |
|---|---|
| `conditions_met` | 已知协议和推荐功率条件满足；仍未实测硬件 |
| `limited_power` | 协议和最低功率满足，但低于推荐功率 |
| `insufficient_power` | 最大输出低于资料中的最低功率 |
| `protocol_mismatch` | 充电器已知协议不包含笔记本所需协议 |
| `insufficient_evidence` | 有关键字段缺失或只有 probable/unknown 证据 |
| `evidence_conflict` | 同一关键字段有相互矛盾的 confirmed 证据 |

这张表不是安全认证。它只是让系统在已知事实范围内诚实地说“满足到哪一步、还不知道什么”。

## 3. 两个现实对象与一个兼容性任务

项目 MVP 只有一个摄像头、一个视觉目标：充电器。笔记本型号由用户人工给出，不代表摄像头
正在追踪笔记本。于是兼容性任务实际上关联两个对象：

```text
charger_target_id = 画面中的充电器
laptop_target_id  = 用户指定的笔记本上下文
```

规则函数显式接收两组 Evidence：

```python
rules.evaluate(
    charger_target_id="charger-001",
    charger_evidence=charger_evidence,
    laptop_target_id="laptop-001",
    laptop_evidence=laptop_evidence,
)
```

这样做有两个好处：

1. 不会把笔记本规格写成充电器的属性，保住 `Evidence.target_id` 的含义。
2. 第 13 章能够保留单摄像头的 `BeliefState`，同时在工作流里显式持有一个兼容性上下文。

本章不改写 `RealSightGraphState`，因为还没有主 Agent 决定何时检索、如何恢复。先把纯数据
与纯规则做对，下一章再把它们放进有 checkpoint 的流程。

## 4. 资料检索不是“让模型随便搜一下”

### 4.1 本章为什么用本地目录

真实厂商资料检索非常重要，却不适合直接作为教学 Demo 前提：网页会更新、地区版本不同、
页面可能要求登录，搜索结果也会变化；更重要的是，来源、发布日期、适用子型号和人工复核
都必须可追踪。

因此本章用版本化 JSON 目录模拟“经过人工审核的内部资料库”。它的作用不是代替真实检索，
而是练习正确的数据边界：

```text
用户型号
  -> 精确匹配目录
  -> 规格记录及 source_document_id/source_revision/source_checked_at
  -> 三条资料 Evidence
  -> 规则层
```

目录中的 `ExampleBook 13` 和 `ExampleWork 15` 是**虚构教学型号**，不对应任何真实厂商。
不要把它们复制到项目演示结论里。

### 4.2 规格记录包含什么

一条 `LaptopSpecification` 包含：

```json
{
  "model_id": "examplebook-13",
  "display_name": "ExampleBook 13",
  "aliases": ["ExampleBook 13", "EB13"],
  "minimum_power_w": 45.0,
  "recommended_power_w": 65.0,
  "required_protocols": ["usb_pd"],
  "source_document_id": "examplebook-13-teaching-manual",
  "source_revision": "teaching-fixture-2026-08",
  "source_checked_at": "2026-08-20T00:00:00Z"
}
```

`minimum_power_w` 与 `recommended_power_w` 不能混为一谈：

- 小于最低值，系统不会推断仍可安全工作。
- 介于最低与推荐之间，可能可以低负载充电，也可能在高负载下降电；所以结果是
  `limited_power`，而不是成功承诺。
- 不低于推荐值，只说明这两条静态条件满足，线缆和端口仍需验证。

### 4.3 为什么只做保守精确匹配

目录只忽略大小写和连续空白，例如 `"  examplebook   13 "` 可以匹配 `ExampleBook 13`。
它不会用编辑距离将 `ExampleBook 14` 猜成 `ExampleBook 13`，也不会在两个记录共享同一别名
时按 JSON 文件顺序选第一台。

结果类型如下：

| status | 行为 |
|---|---|
| `found` | 返回规格和三条资料 Evidence |
| `not_found` | 无 Evidence，主 Agent 应请求更完整型号或可靠资料 |
| `ambiguous` | 返回候选 `model_id`，主 Agent 应询问用户区分 |
| `invalid_query` | 用户 Evidence 不是 confirmed 的非空字符串，不能检索 |

“查不到”不是异常，更不是自动选择最像的型号；它是下一步行动的理由。

## 5. 资料怎样变成 Evidence

用户输入本身是一条独立 Evidence：

```text
field = laptop_model
value = "ExampleBook 13"
source_type = user_input
source_id = user-001
target_id = laptop-001
```

`LocalSpecificationCatalog.lookup()` 成功后生成三条来源不同的 Evidence：

| field | value | source_type |
|---|---|---|
| `laptop_minimum_power_w` | `45.0` | `local_spec_catalog` |
| `laptop_recommended_power_w` | `65.0` | `local_spec_catalog` |
| `laptop_required_protocol` | `["usb_pd"]` | `local_spec_catalog` |

每条资料 Evidence 都有：

- `source_id = source_document_id`，指向人工记录的资料来源。
- `derived_from = [laptop_model Evidence ID]`，说明检索从哪条用户输入开始。
- `source_metadata.catalog_id/model_id/source_revision/source_checked_at`，便于之后发现资料已过期。
- 由 `source_document_id + model_id + field + value` 派生的稳定 Evidence ID；同一目录重复检索
  不会制造语义相同却 ID 不同的事实。

注意：这里的 SHA-256 只稳定生成 Evidence ID，不是对原始说明书 PDF 的法务级内容签名。真实
项目应把资料快照作为 artifact 保存，再保存内容哈希、许可与审核记录。

## 6. 规则层输入：只使用 confirmed Evidence

第 11 章可能输出 `confirmed`、`probable`、`unknown` 或 conflict。规则层的原则是：

```text
confirmed: 可以参与静态比较
probable: 仍是证据缺口，不能升级
unknown: 仍是证据缺口
conflict: 阻断比较，先解决矛盾
```

这看起来保守，但它防止了一个常见错误：OCR 在反光文字上仅有 0.75 置信度，而规则却因为
“总分不错”把 65W 当做确定输入，最后输出一条肯定回答。规则层不应擅自提高来源等级。

对于同字段多条 `confirmed` Evidence：

- 数值完全相同：可共同支持。
- 功率数值不同：`evidence_conflict`。
- 协议列表顺序不同但集合相同，例如 `["usb_pd", "quick_charge"]` 与
  `["quick_charge", "usb_pd"]`：不是冲突。
- 收到显式 `EvidenceStatus.CONFLICT`：直接是冲突。

## 7. 确定性 USB-C 条件规则

规则的执行顺序非常重要：

```text
1. 检查 confirmed 值是否互相冲突
2. 检查五个关键字段是否齐全
3. 检查笔记本所需协议是否是充电器协议集合的子集
4. 比较 charger_max_power_w 与 laptop_minimum_power_w
5. 比较 charger_max_power_w 与 laptop_recommended_power_w
6. 输出结论、所用 Evidence ID、条件与未知边界
```

五个关键字段是：

```text
charger_max_power_w
charger_protocol
laptop_minimum_power_w
laptop_recommended_power_w
laptop_required_protocol
```

### 7.1 为什么协议优先于功率

一个 100W 的非 USB PD 充电器，并不能仅凭瓦数满足一个要求 USB PD 的笔记本。功率数字无法
替代协议协商能力。所以规则会先返回 `protocol_mismatch`，再谈瓦数。

### 7.2 最低功率与推荐功率

以 ExampleBook 13 的 45W 最低、65W 推荐为例：

| 充电器已知最大功率 | 协议 | 结果 |
|---|---|---|
| 30W | USB PD | `insufficient_power` |
| 45W | USB PD | `limited_power` |
| 65W | USB PD | `conditions_met` |
| 100W | Quick Charge | `protocol_mismatch` |

`limited_power` 的描述是“可能在轻负载下充电或充电变慢”，不是肯定会发生。电池电量、系统
负载、固件策略和端口状态都没有进入当前规则。

### 7.3 `conditions_met` 仍要保留什么未知

所有规则结果都带三条固定边界：

1. 未验证线缆的电流、功率和 e-marker 能力。
2. 未验证笔记本实际端口角色、系统状态和 USB PD 协商结果。
3. 未验证充电器多口共享功率、温度保护和实际输出状态。

因此面向用户的最终话术在第 13 章应类似：“根据已读到的标签和资料，已知协议与推荐功率
条件满足；仍建议使用合格 USB-C 线缆并在目标端口实测。”不能只显示绿勾。

## 8. 代码阅读顺序

代码基础还不强时，建议这样阅读：

1. `compatibility/models.py`：先看 `LaptopSpecification`、lookup 状态、六类 verdict。
2. `test-data/ch12/laptop-specifications.json`：理解目录怎样映射为模型。
3. `catalog.py` 的 `lookup()`：看 confirmed 型号为什么才允许检索。
4. `catalog.py` 的 `_build_term_index()`：理解同一记录去重与不同记录歧义的区别。
5. `catalog.py` 的 `_specification_evidence()`：看一条规格为何拆成三个字段。
6. `rules.py` 的 `_resolve_field()`：理解 probable/unknown/conflict 的语义。
7. `rules.py` 的 `evaluate()`：按固定顺序读六种结论分支。
8. Demo 与测试：从预期行为反推每条契约。

每个新增 Python 文件顶部都说明整体逻辑、技术栈、调用流程和边界；函数内部的中文注释重点
解释“为何不能这样做”，而不是逐字翻译 Python 语法。

## 9. 运行最小 Demo

进入项目根目录后运行：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked python examples/ch12_specifications_and_rules.py
```

本章实际最后一行输出：

```text
CH12_DEMO_OK verdict=conditions_met evidence=5 boundaries=3
```

向上查看 JSON，可以确认：

- `model_evidence` 的来源是 `user_input`。
- `lookup.status` 是 `found`，并生成三个 `local_spec_catalog` Evidence。
- 兼容结果的 `used_evidence_ids` 有 5 条：2 条充电器视觉 Evidence + 3 条资料 Evidence。
- `verdict` 是 `conditions_met`，但 `unknown_boundaries` 仍有 3 条。

Demo 中的充电器 Evidence 是第 11 章输出的简化等价物。这样本章聚焦资料和规则，不重复 OCR；
真正端到端编排将在第 13 章完成。

## 10. 自动测试与静态检查

执行：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked pytest -q python/tests/test_ch12_specifications_and_rules.py
uv run --locked ruff format --check python/src/realsight/compatibility python/tests/test_ch12_specifications_and_rules.py examples/ch12_specifications_and_rules.py
uv run --locked ruff check python/src/realsight/compatibility python/tests/test_ch12_specifications_and_rules.py examples/ch12_specifications_and_rules.py
uv run --locked mypy python/src/realsight/compatibility python/tests/test_ch12_specifications_and_rules.py
```

本章实际结果：九项 pytest 测试通过；Ruff 格式和检查通过；严格 mypy 显示
`Success: no issues found in 5 source files`。

测试不是装饰。特别关注两个测试：

- “共享 alias 歧义”：确保 JSON 文件中两台不同笔记本共用别名时，不按文件顺序猜一台。
- “协议列表顺序”：确保同一协议集合的不同书写顺序不会误报为冲突。

它们体现了资料系统常见的两个坑：错误匹配和错误冲突。

## 11. 常见误解与排查

### 11.1 为什么找不到型号也不做模糊匹配

型号相似不等于规格相同。错误地检索到另一台笔记本，比诚实地请求用户拍摄型号或补充说明书
更危险。本 MVP 宁可返回 `not_found`，由主 Agent 在第 13 章提出 `ask_user`。

### 11.2 为什么资料 Evidence 的置信度不是 1.0

Pydantic 能保证 JSON 结构合法，不能保证人工抄录、资料版本或适用子型号绝对正确。规格
记录保留 `confidence` 和 `source_checked_at`，提醒你资料也是会过期的外部知识。课程夹具的
0.95 是示意值，不是测量出来的真值。

### 11.3 为什么 `probable` 没有参与规则

因为让规则自行升级证据状态，会使来源等级失去意义。第 13 章可决定请求补拍、请用户确认
标签、或用第二份资料交叉验证；只有明确接纳后，才把它作为 confirmed 输入。

### 11.4 为什么有 65W 和 USB PD 还不能承诺实测成功

当前系统没有验证线缆、端口角色、系统策略、充电器多口分配和 PD negotiation。规则能说明
“静态标签与资料条件已经满足”，不能替代插上设备后的行为测量。

## 12. 课后练习

1. 在临时 JSON 目录中增加一台需要 100W 推荐功率的虚构笔记本，运行 65W 充电器并解释
   为什么结果应为 `limited_power` 而不是 `insufficient_power`。
2. 为 `laptop_required_protocol` 加入两个协议，构造充电器只支持一个的情形，解释集合子集
   判断为何比字符串相等更合适。
3. 设计真实资料目录的审核字段：文档 URL/文件哈希、页码、审核人、审核时间、适用地区、
   失效日期。哪些适合 `source_metadata`，哪些应放单独数据库表？
4. 写一个“资料过期”设计：若 `source_checked_at` 超过一年，返回不执行规则的缺口还是
   `probable` Evidence？比较两种选择。
5. 设计线缆 Evidence：字段、来源、如何拍摄/读取、何时进入规则。说明为什么 e-marker
   不能靠充电器背标推断。
6. 不改代码，只画出第 13 章遇到 `not_found`、`ambiguous`、`limited_power` 和
   `evidence_conflict` 时的 Action 路由图。

## 13. 本章能力边界

本章已经完成：

- 用户型号到本地资料的严格、可审计检索。
- 最低/推荐功率与所需协议的来源 Evidence。
- 冲突优先、缺失优先、协议优先的确定性规则。
- 条件满足、受限功率、功率不足、协议不匹配、证据不足、证据冲突六类结果。
- 每个结果的 Evidence ID、条件与硬件未知边界。
- 无网络 Demo 和九项测试。

本章没有完成：

- 真实厂商说明书、网页搜索、PDF 解析、RAG 或企业知识库连接。
- 真实型号的资料正确性认证和持续更新。
- 多口充电器端口映射、线缆 e-marker、USB4/Thunderbolt、显示输出。
- 真实 USB PD 报文协商、充电电流测量或笔记本状态诊断。
- 将结果写入 `BeliefState`、checkpoint、治理命令或 API。

这些是接下来的工程任务，不是可以由一个更长 prompt 自动补齐的细节。

## 14. 到第 13 章的桥接

第 12 章留下的不是一段文字，而是三个可路由的结构化结果：

```text
SpecificationLookupResult
  found / not_found / ambiguous / invalid_query

UsbCCompatibilityResult
  verdict + used_evidence_ids + missing_fields + conflicting_fields + unknown_boundaries

VisionExtraction
  evidence + EvidenceGap
```

第 13 章《主 Agent 与完整循环》将首次把它们编排进 LangGraph：

```text
视觉 Evidence 不足       -> request_view
没有笔记本型号           -> ask_user
型号已确认但目录未知/歧义 -> retrieve_knowledge 后请求澄清
功率/协议证据齐全         -> run_rules
规则得到可解释结果        -> generate_answer
```

它还会把中断、恢复、治理和 checkpoint 接进同一循环，确保每次恢复都能知道“已经看过什么、
已查过哪份资料、规则为什么没有运行”。第 12 章只负责让这些步骤拥有可靠输入与可审计输出。
