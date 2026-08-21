# 第 12 章质量鉴定报告：资料检索与 USB-C 规则引擎

## 1. 审计范围与先行结论

本报告审计第 12 章《资料检索与 USB-C 规则引擎》的课程设计、正式 Python 实现、教学
资料目录、Demo 与测试。输入包括第 11 章产生的充电器视觉 Evidence、用户明确提供的
笔记本型号 Evidence，以及本地人工核验规格目录；输出是带来源 ID、未知边界和结论类型的
`UsbCCompatibilityResult`。

本章不使用互联网搜索作为 Demo 的隐含依赖。网络页面、供应商资料、认证、版权、模型排序
和网络失败会让初学者无法复现结果，也会让“检索正确”没有清晰含义。因此本章采用一份
明确标注为虚构教学夹具的 JSON 规格目录，重点学习**资料如何成为 Evidence**，而不是假装
完成了全球设备资料检索。

审计后结论：**在单充电器、用户提供笔记本型号、本地已审核规格、只检查功率与 USB PD
协议条件的 MVP 边界内条件通过，可以进入第 13 章；不得将 `conditions_met` 宣称为真实
设备已经充电成功，也不得将教学规格夹具当作厂商规格。**

---

## 2. 逻辑鉴定

### 2.1 推导顺序

正文采用：视觉 Evidence 的缺口 -> 用户型号事实 -> 本地资料目录 -> 资料 Evidence ->
字段归并 -> 冲突/缺失优先 -> 协议判断 -> 最低/推荐功率 -> 带未知边界的结论 -> Demo/测试
-> 第 13 章编排。读者不需要先理解 LangGraph 主 Agent 即可验证核心纯函数。

结论：通过。

### 2.2 关键职责边界

| 组件 | 做什么 | 不做什么 |
|---|---|---|
| 第 11 章视觉层 | 读取标签、输出功率/协议候选与 OCR 来源 | 查笔记本规格、做兼容结论 |
| 用户输入 | 确认 `laptop_model` | 伪装成厂商资料 |
| `LocalSpecificationCatalog` | 严格精确匹配本地 JSON、输出三条资料 Evidence | 模糊猜测型号、联网爬取、决定兼容 |
| `UsbCCompatibilityRules` | 比较 confirmed 的功率和协议 | 读取文件、调用模型、控制硬件 |
| 第 13 章主 Agent | 根据 lookup/gap/result 决定重拍、询问、检索或回答 | 重写确定性规则 |

本章刻意让充电器和笔记本是两个 `target_id`。摄像头只观察充电器，笔记本型号是任务上下文，
资料 Evidence 不能伪装成充电器本身的属性。第 12 章纯函数显式接收两组 Evidence；第 13 章
将引入兼容性任务上下文来组织两组来源，而不是把笔记本规格硬塞入 charger 的 `BeliefState`。

结论：通过，且消除了单目标摄像头与双对象兼容判断之间的概念混淆。

### 2.3 判断顺序与失败语义

规则的固定顺序是：

```text
冲突 -> 缺少 confirmed Evidence -> 协议 -> 最低功率 -> 推荐功率 -> 条件满足
```

- 两条 confirmed 的同字段值不同，或收到显式 `conflict`：`evidence_conflict`。
- `probable`/`unknown` 不是 confirmed 输入：`insufficient_evidence`，不会偷偷升级。
- 充电器协议不包含笔记本要求：`protocol_mismatch`，它优先于瓦数判断。
- 小于最低功率：`insufficient_power`。
- 不低于最低、低于推荐：`limited_power`，说明可能慢充或轻负载才可用。
- 不低于推荐且协议满足：`conditions_met`，仍保留线缆、端口、PD 协商、多口共享功率三项未知。

这个顺序避免了“65W 很大，所以肯定能用”这类直觉短路。

结论：通过。

### 2.4 实施中发现并修复的真实问题

规格目录索引最初会将同一条记录的 `model_id`、`display_name` 和 alias 都加入同一规范化词条。
例如 `examplebook-13` 的显示名与 alias 都是 `ExampleBook 13` 时，一条记录被重复计数，
检索错误返回 `ambiguous`。

修正：索引按 `model_id` 去重；只有**两条不同记录**共享同一别名时，才返回歧义候选。九项
测试中的精确匹配与歧义匹配共同覆盖这一点。

结论：逻辑问题已修复，不遗留到正文。

---

## 3. 技术栈鉴定

| 技术 | 是什么 | 为什么使用 | 实现位置 | 当前限制 |
|---|---|---|---|---|
| Python 3.12 | `StrEnum`、`dataclass`、`json`、`pathlib` | 让规则纯函数、目录可读且跨平台 | compatibility 包 | 不提供分布式检索 |
| Pydantic v2 | 严格字段和跨字段校验 | 目录、检索结果、结论不能含模糊字典 | `models.py` | 不验证厂商资料真伪 |
| JSON 规格目录 | 版本化结构化资料 | Demo 可复现、资料来源可人工审核 | `test-data/ch12` | 只是虚构教学夹具 |
| SHA-256 | 短稳定摘要 | 同一资料字段可生成稳定 Evidence ID | `catalog.py` | 不是 PDF/JPEG 内容签名 |
| 确定性规则 | 固定比较和分支顺序 | 兼容逻辑能测试、审计、复现 | `rules.py` | 不执行真实 PD 协商 |
| pytest/Ruff/mypy | 行为、风格、静态类型门禁 | 验证失败分支与代码可维护性 | tests/CI 命令 | 不等于硬件验收 |

技术栈同时说明了“为何使用”和“没解决什么”。没有暗中安装搜索 SDK 或使用未记录的模型。

结论：通过。

---

## 4. Demo 鉴定

### 4.1 可运行性

Demo 只依赖项目已经锁定的 Python 3.12/Pydantic 环境和仓库内 JSON 文件，不需要网络、API
Key、GPU、摄像头或真实厂商资料。

实际命令：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked python examples/ch12_specifications_and_rules.py
```

实际关键输出：

```text
CH12_DEMO_OK verdict=conditions_met evidence=5 boundaries=3
```

该 Demo 验证：视觉层的 65W/USB PD 两条 Evidence，加用户型号和目录生成的三条资料 Evidence，
得到五个可追溯输入；结果同时留下三项真实硬件未知边界。

### 4.2 自动测试

实际命令：

```powershell
uv run --locked pytest -q python/tests/test_ch12_specifications_and_rules.py
```

实际结果：

```text
9 passed
```

测试覆盖精确匹配、未知型号、条件满足、功率受限、协议优先失败、confirmed 冲突、probable
不升级、协议列表顺序不构成假冲突、共享 alias 的真实歧义。

### 4.3 静态质量

第 12 章自身 Ruff 格式/规则检查均通过，严格 mypy 输出 `Success: no issues found in 5
source files`。

结论：Demo 通过；它验证的是资料证据链与规则分支，不是互联网搜索质量或真实硬件行为。

---

## 5. 桥接鉴定

### 5.1 前向桥接

- 第 4 章：继续使用 `Evidence`、`EvidenceStatus` 和 `source_metadata`，没有改名稳定类型。
- 第 6 章：资料目录是受版本控制的小 JSON；将来真实 PDF/网页快照应进入 artifact storage，
  Evidence 只保存来源 ID、版本和引用。
- 第 7 章：未来 `spec.retrieve` 与 `rules.usb_c` 都应经过能力、预算、超时和审计治理；本章
  的本地纯函数不伪装成已受远程治理的服务。
- 第 8 章：代码进入正式 `src/realsight/compatibility` 包，不依赖 examples 的临时导入。
- 第 10、11 章：只消费已确认的充电器 Evidence；不读取原始视频或重复 OCR。

### 5.2 后向桥接

第 13 章接收三个结构化外部结果：

1. `SpecificationLookupResult`：found/not_found/ambiguous/invalid_query。
2. `UsbCCompatibilityResult`：结论、使用 Evidence、缺失字段、冲突字段和未知边界。
3. 第 11 章 `EvidenceGap`：需要更清晰背标或缺协议/功率。

主 Agent 决定是否产生 `ask_user`、`retrieve_knowledge`、`request_view`、`run_rules` 或最终
回答。第 14 章再把这些过程转换为 `RunEvent` 和 WebSocket 进度，不能把教学资料或内部规则
细节无差别暴露给普通客户端。

结论：通过。

---

## 6. 未通过的生产验收

1. 没有接入真实厂商说明书、知识库或企业检索系统。
2. 没有资料快照、版权许可、来源审核人、过期策略和变更通知流程。
3. 没有型号模糊匹配；这是安全选择，不是缺陷被隐藏。
4. 没有线缆 e-marker、端口方向、USB PD 实际协商、多口共享功率、温度和电池状态证据。
5. 没有真实充电仪表测试、设备型号矩阵或供电安全认证。
6. 没有将规则输入/输出接入第 13 章 checkpoint 与第 14 章 API。

最终判定：**第 12 章课程实现通过；生产级设备规格检索和真实 USB-C 兼容认证未完成。**
