# 第 11 章质量鉴定报告：视觉证据子 Agent

## 1. 审计范围与先行结论

本报告审计第 11 章《视觉证据子 Agent：从关键帧到可审计 Evidence》的教学设计、正式
Python 实现、最小 Demo 与测试。它接收第 10 章真实 gRPC 链路输出的 Pydantic
`Observation`，目标是把一张合格的 USB-C 充电器背面标签关键帧转成候选 `Evidence`。

审计前先提出四个容易被忽略的风险：

1. 把 C++ 的 `local_features` 误写成 OCR 文本，会让“画面质量”越权变成“标签含义”。
2. 把 OCR 置信度直接写成“充电器兼容概率”，会混淆文字读取可靠性与最终业务结论。
3. 面对多个瓦数时简单取最大值，可能把多口充电器、输入功率或相互矛盾的标签混为一谈。
4. 用脚本化 OCR Demo 却宣称“已经完成 OCR”，会给学习者错误的完成感。

实现已经针对四项分别设防：只读取 `Observation.image_path`；以 `EvidenceGap` 表示不能
可靠提取的字段；只在完整输出档位中选最大值，额定瓦数与档位矛盾时拒绝选择；Demo 明确
标注为可替换 OCR 端口的教学替身。因此结论不是无条件“全部通过”，而是：**在单目标、
背面标签、教学 MVP 的边界内条件通过，可以进入第 12 章；不得表述为通用商品视觉识别或
真实 OCR 准确率验收。**

---

## 2. 逻辑鉴定

### 2.1 推导顺序

正文顺序为：第 10 章输入边界 -> 为什么需要视觉子 Agent -> OCR 与 Evidence 的差别 ->
可替换识别端口 -> 文字区域契约 -> 质量和识别置信度 -> 三类受限字段归一化 -> 缺口与
冲突 -> Demo/测试 -> 第 12、13 章桥接。

读者先得到“图片已经由 C++ 筛过”的前提，才学习 Python 怎样解释关键帧；随后才介绍
正则归一化和 USB-C 标签格式。没有要求读者提前理解第 12 章的笔记本规格库或第 13 章的
主 Agent 路由。

结论：通过。

### 2.2 职责边界

| 层 | 本章负责 | 本章明确不负责 |
|---|---|---|
| C++ runtime / 第 10 章 | 采集、质量门槛、关键帧、JPEG 工件、gRPC | OCR、业务字段、兼容判断 |
| `TextRecognizer` | 从一张图片返回原文、相对文字框、OCR 置信度 | 建立 Evidence、写 BeliefState |
| `VisionEvidenceAgent` | 验证 Observation、归一化三类字段、保留来源、生成 gap | 调摄像头、调 gRPC、写库、查规格、算兼容 |
| 第 12 章 | 资料证据、USB-C/PD 规则、冲突复核策略 | 重做 OCR |
| 第 13 章 | 根据 gap 规划下一步并汇总证据 | 把模型输出绕过证据账本 |

`VisionEvidenceAgent` 被称为“子 Agent”，是因为它有受限目标、专用工具端口、可解释输出
和失败回传，不是因为它每次都必须调用 LLM。当前 MVP 的字段归一化采用确定性代码，反而
更适合让初学者看清边界。

结论：通过。

### 2.3 状态与失败语义

- `Observation.status != accepted`：不调用 OCR；每个请求字段得到
  `observation_not_accepted` gap。
- 质量不足、缺图、找不到工件、识别后端可预期失败、没有文字：不生成 Evidence，只给可
  行动 gap。
- OCR 文字存在但字段所用区域置信度低：不以 `probable` 偷渡，给
  `recognition_confidence_too_low` gap。
- 65W 与 20V x 2A = 40W 同时出现：给 `conflicting_power_candidates` gap，不选择较大值。
- 没看到 PD/PPS/QC 标记：给 `protocol_not_found`，它表示“当前未知”，绝不等于“不支持
  PD”。

关键修正：最初审阅中发现“只请求 `laptop_model` 这种不支持字段时仍会调用 OCR”会浪费资源
并模糊边界。实现现已在 OCR 调用前返回 `unsupported_visual_feature`，对应测试也验证调用
次数为零。

结论：通过。

---

## 3. 技术栈鉴定

| 技术 | 是什么 | 为什么在本章使用 | 实现位置 | 当前限制 |
|---|---|---|---|---|
| Python 3.12 `Protocol` | 结构化的接口约定 | 把 OCR/VLM 后端从证据逻辑中拔出 | `TextRecognizer` | 不提供实际 OCR 模型 |
| Pydantic v2 | 输入输出运行时校验 | 防止错目标、错 Observation、退化文字框进入账本 | `TextRegion`、`RecognitionDocument`、`VisionExtraction` | 不验证图片像素真实内容 |
| Python `re` | 受限文本模式匹配 | 演示“原文 -> 可解释字段”而不引入模型黑箱 | 功率与协议归一化函数 | 不支持任意商品/多语言/复杂表格 |
| `hashlib.sha256` | 稳定摘要 | 让同一观察、字段和值得到短小可复现的 Evidence ID | `_build_evidence` | 不是工件内容哈希；第 6 章已有 artifact 哈希 |
| pytest | 确定性行为测试 | 验证成功、冲突、未知、低置信、拒绝和边界字段 | `test_ch11_vision_evidence_agent.py` | 不衡量真实 OCR 准确率 |
| 真实 OCR/VLM（后续替换） | 图片到文字/结构候选 | 企业项目需要经过评测和部署审批的后端 | 只需实现 `TextRecognizer` | 本章不安装、不选型、不联网 |

正文说明了每项技术“是什么、为什么、在哪儿实现、当前没解决什么”。尤其没有把正则或
脚本化输出宣传成企业级 OCR；真实后端属于可替换适配器，其上线应另做数据集、隐私、成本、
延迟、故障和版本评估。

结论：通过。

---

## 4. Demo 鉴定

### 4.1 可运行性

Demo 不依赖网络、GPU、摄像头、模型密钥或新安装包。它创建一张最小 PPM 工件，脚本化
识别器返回固定文字区域，之后运行的是正式 `VisionEvidenceAgent`，而非在 Demo 中复制
业务逻辑。

实际命令：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked python examples/ch11_vision_evidence_agent.py
```

实际关键输出：

```text
CH11_DEMO_OK fields=charger_label,charger_max_power_w,charger_protocol
```

JSON 中可核对：

- `charger_max_power_w.value = 65.0`；来源同时保留 `65W Max` 和 `20V=3.25A` 区域。
- `charger_protocol.value = ["usb_pd"]`，扩展能力 `pps` 放入元数据，不把扩展误作另一个
  基础协议。
- `source_id = observation-ch11-demo`，`derived_from` 是具体 region ID。
- `source_metadata` 有原 OCR 文字、相对框、OCR 置信度、关键帧质量和归一化方法。
- `gaps = []`，说明该教学样本的三项字段均可提取。

### 4.2 自动测试

实际命令：

```powershell
uv run --locked pytest -q python/tests/test_ch11_vision_evidence_agent.py
```

实际结果：

```text
6 passed
```

覆盖项包括：完整 65W PD/PPS 标签、功率候选矛盾、未知协议、低 OCR 置信度、被 C++ 拒绝
的 Observation 不触发 OCR、完全不支持的字段不触发 OCR。

### 4.3 静态质量

实际执行：Ruff 格式/检查与严格 mypy。结果分别为 `4 files already formatted`、`All checks
passed!`、`Success: no issues found in 3 source files`。

结论：Demo 通过；但它验证的是证据控制流和契约，不是图片识别精度。

---

## 5. 章节桥接鉴定

### 5.1 与前十章的桥接

- 第 4 章：复用正式 Pydantic `Observation`、`Evidence`、`EvidenceStatus`；`Evidence` 只
  新增有默认值的 `source_metadata`，没有改名或破坏旧构造方式。
- 第 5 章：视觉层不自行产生中断；它输出 gap，由工作流在正确位置暂停。
- 第 6 章：图片仍由 artifact storage 管理；Evidence 只存路径所代表观察的引用和小型
  JSON 元数据，不保存图片二进制。
- 第 7 章：OCR 调用属于可计数、可限流的外部能力；当前端口让治理中间件以后可以包裹它。
- 第 8 章：使用正式 `src/realsight` 包和锁定 Python 3.12 环境。
- 第 9、10 章：只消费 accepted `Observation.image_path`、`quality`、`view_type`、
  `target_id` 和源帧追踪字段；不回传视频，不修改 C++ runtime。

### 5.2 到后续章节的桥接

- 第 12 章接收 `charger_max_power_w` 和 `charger_protocol` Evidence，以及可能的 gap；再
  添加笔记本规格资料证据，用确定性规则判断条件是否满足。
- 第 13 章把 `EvidenceGap` 映射为 `request_view`、`ask_user` 或资料检索动作，并将多个
  证据写入 `BeliefState`，处理冲突和恢复。
- 第 14 章将识别进度与证据结果包成 `RunEvent`，通过 API/WebSocket 呈现；它不应把 OCR
  原文或整张标签图默认广播给所有客户端。

结论：前后桥接清晰。

---

## 6. 尚未通过的生产验收

以下项目不随“课程条件通过”被掩盖：

1. 没有真实充电器标签数据集、人工标注、CER/WER/字段准确率或置信度校准。
2. 没有真实 OCR、VLM、多语言、旋转文字、表格布局、手写体和低光图像测试。
3. 没有多口充电器端口归属解析；本章只能处理单个受限标签场景。
4. 没有 OCR 服务的超时、重试、并发、成本、模型版本或敏感文字脱敏策略。
5. `Evidence` 候选尚未写入持久账本或 `BeliefState`；这是第 12、13 章刻意保留的职责。
6. 未将 `source_metadata` 限制到生产审计的大小、保留期和访问控制策略。

这些限制已经写入正文和代码头部注释。因此最终判定是：**第 11 章教学实现通过，生产级
视觉证据服务未完成。**
