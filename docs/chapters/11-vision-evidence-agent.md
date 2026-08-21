# 第 11 章：视觉证据子 Agent - 从关键帧到可审计 Evidence

## 1. 本章目标、前提与产物

第 10 章已经打通了真正的 C++/Python gRPC 链路：C++ 不断处理视频或摄像头帧，筛掉模糊、
过暗或反光严重的画面，只把一张带质量分数、来源帧序号和图片工件路径的 `Observation`
交给 Python。它解决的是“哪一帧值得看”。

本章解决紧接着的问题：**这张关键帧中写了什么，以及怎样把它变成以后可以复核的证据？**

完成本章后，你应能解释并运行以下链路：

```text
accepted Observation
    -> image_path 指向的关键帧工件
    -> OCR/VLM 识别器返回文字区域
    -> 视觉证据子 Agent 校验、归一化、计算置信度
    -> Evidence 或 EvidenceGap
    -> 第 12 章资料检索和规则引擎 / 第 13 章主 Agent
```

本章的受限任务是：从**一个单目标 USB-C 充电器背面标签**中提取三类候选字段。

| 请求字段 | 本章输出 | 示例 |
|---|---|---|
| `charger_label` | 标签全文，保留换行和原 OCR 区域 | `Example USB-C Charger ...` |
| `charger_max_power_w` | 最大输出功率的数值证据 | `65.0` |
| `charger_protocol` | 支持协议的有序集合 | `["usb_pd"]`，元数据含 `pps` |

本章交付物：

- 正式视觉模块 `python/src/realsight/vision/evidence_agent.py`。
- 向后兼容的 `Evidence.source_metadata` 字段。
- 最小 Demo `examples/ch11_vision_evidence_agent.py`。
- 六项确定性测试 `python/tests/test_ch11_vision_evidence_agent.py`。
- 独立质量鉴定报告 `docs/reviews/11-quality-audit.md`。

学习前提是：你理解 `Observation` 不是 `Evidence`，知道第 10 章为什么把连续视频留在 C++，
并能读懂 Pydantic 模型的字段和验证器。不会 OCR、深度学习或 C++ 都可以继续本章。

## 2. 从“看见标签”到“能说明依据”

假设用户问：“这个 65W USB-C 充电器能给我的笔记本充电吗？”

看到一张充电器背面的照片，并不能直接回答。至少还缺三层事实：

1. 图片是否足够清楚，真的是该任务所指的目标和视角？
2. 标签上的原文、文字位置和 OCR 置信度分别是什么？
3. 原文能否规范地支持“最大功率 65W”“支持 USB PD”这类字段？

第 10 章处理第一层，但不会凭空理解文字；第 11 章处理第二、三层；第 12 章才会引入
笔记本规格与 PD 规则，判断“能否”。把三层混成一次大模型调用，演示时很快，出现错误时却
无法说清究竟是镜头、识别、资料还是规则出了问题。

一个很重要的用词区分：

- OCR 说“我把 `65W Max` 读成了这些字符，置信度 0.94”。
- 本章 Agent 说“这些字符与 `20V=3.25A` 相互印证，可形成 `charger_max_power_w=65.0`
  的候选 Evidence”。
- 第 12 章规则说“65W 和 USB PD 是否满足某台笔记本的输入要求”。
- 第 13 章主 Agent 说“证据是否充分；若不充分，下一步请用户做什么”。

它们都很有价值，但不是同一种结论。

## 3. 为什么它可以叫“视觉证据子 Agent”

本项目不把任何函数都叫 Agent。一个有资格称为子 Agent 的组件，至少应具有：明确目标、
受限输入、专用工具、结构化输出、失败语义和可替换实现。

视觉证据子 Agent 满足这些条件：

- 目标：从一张已筛选的背面标签图生成可追溯候选证据。
- 输入：`Observation`、请求字段和一个图片工件路径。
- 专用工具：`TextRecognizer`，可接本地 OCR、远程 OCR 或多模态模型适配器。
- 输出：`VisionExtraction(document, evidence, gaps)`。
- 失败：不清晰、缺图、识别失败、未知文本、低置信、候选矛盾均变成 `EvidenceGap`。
- 边界：不控制摄像头、不启动 gRPC、不保存数据库、不决定 USB-C 兼容性。

它当前不需要 LLM。标签功率的乘法和协议标记的归一化，使用确定性代码更适合 MVP：同样
输入会得到同样输出，便于你追踪错误。未来接入 VLM 时，VLM 只替换“图片到文字/结构候选”
的适配器；不能替代来源记录、字段校验和规则层。

## 4. 输入契约：只接受第 10 章的合格 Observation

本章不会读取 gRPC stream，也不会读取 `local_features` 来猜测标签内容。它的入口已经是
第 10 章 Python adapter 输出的 Pydantic `Observation`。至少关心：

```text
observation_id       这次观察的不可混淆 ID
target_id            这条证据属于哪个现实目标
view_type            本课程 USB-C 标签必须是 back_label
status               必须是 accepted
image_path           已落盘关键帧的位置
quality.overall_score C++ 对图像可用性的分数
source_frame_sequence/source_position_ms 追到连续帧流的位置
```

进入 OCR 前，`VisionEvidenceAgent.extract()` 做五道廉价检查：

1. 请求字段是否属于本章支持的三项；若全不支持，直接返回 gap，不调用 OCR。
2. `status` 是否为 `accepted`；`rejected` 图像不可被“模型可能看懂”覆盖。
3. `view_type` 是否为 `back_label`；本 MVP 不用正面随意猜背标参数。
4. `overall_score` 是否至少为默认 0.65；这只是教学启发式，不是行业通用阈值。
5. `image_path` 是否存在；不能将上一次图的 OCR 文本借给这次 Observation。

这五步与“模型能力强不强”无关，却决定系统是否会生成无法追溯的幻觉证据。

## 5. 识别端口：把 OCR/VLM 放在正确位置

本章的接口故意很小：

```python
class TextRecognizer(Protocol):
    def recognize(
        self, image_path: Path, *, observation_id: str
    ) -> RecognitionDocument: ...
```

它不是要求所有识别后端长得一样，而是要求它们把输出收敛成同一份 `RecognitionDocument`：

```text
RecognitionDocument
  observation_id
  regions[]
    region_id
    text
    ocr_confidence
    left, top, right, bottom  # 0 到 1 的相对坐标
```

相对坐标而不是像素坐标的好处是：无论原图是 1280x720 还是裁剪图，账本都能描述“这段文字
来自标签的哪里”。`TextRegion` 验证 `right > left` 且 `bottom > top`，所以不会把一个零面积
框写成证据来源。

### 5.1 三种实现层次

| 实现 | 适合什么阶段 | 输入/输出 | 不能证明什么 |
|---|---|---|---|
| 脚本化识别器 | 本章 Demo 与单元测试 | 固定文字区域 | 不能证明图片识别正确 |
| 本地 OCR 适配器 | 后续个人实验 | 图片路径 -> OCR 框/文本/置信度 | 需自行做模型、语言、驱动与性能评测 |
| 多模态模型适配器 | 受控的复杂标签试验 | 图片路径 -> 严格 JSON 候选 | 不能跳过来源、价格、隐私和稳定性治理 |

选择何种真实后端不是本章的成绩。企业工程里，模型选择要看你的标签语言、离线要求、显存、
延迟、成本、数据能否外发、版本管理和可复现实验。先把接口和证据语义做对，后面替换后端
才不会毁掉整个工作流。

## 6. Evidence 为什么要增加 source_metadata

第 4 章的 `Evidence` 已经有 `source_type`、`source_id`、`confidence` 和 `derived_from`。
第 11 章在不改名、不破坏旧构造方式的前提下增加了默认空字典 `source_metadata`。

一条功率证据的核心结构如下：

```json
{
  "field": "charger_max_power_w",
  "value": 65.0,
  "source_type": "vision_ocr",
  "source_id": "observation-ch11-demo",
  "confidence": 0.92,
  "derived_from": ["region-ch11-output", "region-ch11-protocol"],
  "source_metadata": {
    "normalizer": "matching_explicit_wattage_and_output_profile",
    "observation_quality": 0.92,
    "regions": ["省略：原文、相对框和 OCR 置信度"],
    "direct_wattage_candidates": [65.0],
    "output_profile_wattage_candidates": [15.0, 27.0, 45.0, 65.0]
  }
}
```

注意三个层次：

- 图片二进制仍属于第 6 章 artifact storage，Evidence 不保存图片内容。
- `source_id` 是 Observation ID，表明证据来自哪一次观看。
- `derived_from` 是文字区域 ID，`source_metadata` 再保存原文和位置，方便人回看“为什么是
  65W”。

这使后续规则、审计日志或前端可以显示依据，而不是只显示一个神秘的 65。

## 7. 置信度：不是兼容性概率

第 10 章的 `overall_score` 说的是“关键帧作为视觉输入有多可用”；OCR 的
`ocr_confidence` 说的是“识别器对这些字符有多确定”。它们没有一个等于“这款充电器能不能
给这台电脑充电”。

本章的候选 Evidence 采用保守组合：

```text
evidence_confidence
  = min(observation.quality.overall_score, 所有支撑文字区域的 ocr_confidence)
```

因此清晰度 0.92、相关文字框 OCR 置信度 0.94 的功率 Evidence 得到 0.92。一个环节弱，
证据不会假装更强。默认规则为：

- 小于 0.70：产生 `recognition_confidence_too_low` gap。
- 0.70 到小于 0.85：候选 Evidence 标记 `probable`。
- 不小于 0.85：候选 Evidence 标记 `confirmed`。

这仍是课程阈值，必须用真实标签数据校准。`confirmed` 的含义是“在当前视觉层中有足够
可复核的证据”，不是“最终兼容结论已确认”，更不是人身安全保证。

## 8. 三个字段如何归一化

### 8.1 `charger_label`：保存原文，而非猜品牌型号

本章将识别文档各区域的文字按原顺序换行拼接，作为 `charger_label`。这看上去朴素，却是
诚实的做法：在没有产品目录和型号规范器前，不应该把 `Example USB-C Charger` 猜成某个
具体厂商/型号。第 12 章如果需要资料检索，会在可见原文基础上提出明确查询。

### 8.2 `charger_max_power_w`：区分输出档位、额定瓦数与矛盾

标签常写：

```text
Output: 5V=3A  9V=3A  15V=3A  20V=3.25A
65W Max
```

本章会计算每个输出档位的功率：15W、27W、45W、65W，并将输出档位的最大值作为候选。
因为多个档位是同一个充电器的工作范围，不应把 15W、27W、45W 视为内部冲突。

如果同时读到 `65W Max`：

- 它与最大输出档位同为 65W：两段文字共同支持一条 65W Evidence。
- 它与最大输出档位不同，例如 `65W Max` 和 `20V=2A`（40W）：返回
  `conflicting_power_candidates`，不自动取大值，也不自动取小值。
- 只看到一个明确瓦数：用 `single_explicit_wattage` 生成候选。
- 看到多个瓦数却没有完整输出档位：返回 `ambiguous_power_candidates`。

这里尚未解决多口充电器、总功率/单口功率、输入额定功率和图片表格布局；把这些当作已经
解决，会比承认范围更危险。

### 8.3 `charger_protocol`：协议是集合，不一定是一个字符串

一个充电器可以同时兼容 USB PD 和 Quick Charge。于是字段名字保持已有的
`charger_protocol`，其值却是稳定排序的 JSON 列表，例如：

```json
["usb_pd", "quick_charge"]
```

PPS 是 USB PD 的可选扩展，本章把基础协议保留为 `usb_pd`，把 `pps` 放进
`source_metadata.extensions`。这是一个有意识的建模选择：第 12 章规则可先问“是否有
USB PD”，需要时再阅读扩展能力，不会把 `PPS` 错当成与 PD 平级的基础协议。

没有读到协议标记时，输出 gap，而不是 `charger_protocol=[]`。空列表容易被误解成“已经
确认一个也不支持”，而当前事实只是“这张图没有提供足够证据”。

## 9. EvidenceGap 是正常业务输出

在真实视觉系统里，“不知道”常常比“猜错”更有价值。`EvidenceGap` 有：

```text
observation_id / target_id / field
code             程序可路由的失败类型
message          给日志与用户界面的解释
suggested_view   目前固定为 back_label
```

第 11 章的 gap 不直接产生 `Action`，因为视觉子 Agent 不拥有全局任务计划。第 13 章主
Agent 会结合已有 `BeliefState`、观察预算和用户上下文决定：

- `request_view`：请翻转充电器、靠近标签、避开反光。
- `ask_user`：请提供笔记本型号或直接确认文字。
- `retrieve_knowledge`：根据已得到的型号/规格查询资料。
- `run_rules`：只有必要 Evidence 已齐全且无未解决冲突时才执行。

这就是“主动感知”：缺口不被吞掉，而是成为下一次行动的理由。

## 10. 阅读正式代码

建议按这个顺序阅读，而不是从最长的归一化函数开始：

1. `TextRegion`：先理解为什么文字、置信度和相对框必须一起存在。
2. `RecognitionDocument`：理解它为什么只能属于一个 Observation。
3. `EvidenceGap` 与 `VisionExtraction`：理解成功和未知为何能在一个结果中并存。
4. `TextRecognizer`：看清真实 OCR/VLM 将来要替换的唯一接口。
5. `VisionEvidenceAgent.extract()`：看输入门槛和失败短路。
6. `_extract_max_power()`：手算一次电压乘电流，再理解它为何不随便选最大瓦数。
7. `_build_evidence()`：看原文、区域和置信度如何进入 `source_metadata`。
8. 最后读 Demo 和测试，用预期行为反推实现。

所有本章新增 Python 文件顶部都写了“整体逻辑、技术栈、调用流程和边界”。你阅读每个
函数时优先问“这个函数保证了什么不变量”，不要只翻译每一行语法。

## 11. 运行最小 Demo

在项目根目录执行：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked python examples/ch11_vision_evidence_agent.py
```

`UV_CACHE_DIR` 这一行只把本次的依赖缓存放进项目目录；当用户级缓存没有写权限时，它不
影响代码或锁文件。完整 JSON 很长，先寻找最后一行：

```text
CH11_DEMO_OK fields=charger_label,charger_max_power_w,charger_protocol
```

再向上检查三个证据：

1. 功率 `value` 是否为 `65.0`。
2. 协议 `value` 是否为 `["usb_pd"]`，并在元数据看到 `pps`。
3. 每个 Evidence 是否带 `source_id=observation-ch11-demo` 和非空 `source_metadata`。

Demo 使用的是 `ScriptedTextRecognizer`，因此不要用它测试一张自己的充电器照片。它的作用
是让你先理解正式控制流。接入本地 OCR 或模型适配器时，直接实现 `TextRecognizer`，并用
真实但已脱敏的标注样本新增测试。

## 12. 自动测试与静态检查

执行：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv run --locked pytest -q python/tests/test_ch11_vision_evidence_agent.py
uv run --locked ruff format --check python/src/realsight/vision python/tests/test_ch11_vision_evidence_agent.py examples/ch11_vision_evidence_agent.py
uv run --locked ruff check python/src/realsight/vision python/tests/test_ch11_vision_evidence_agent.py examples/ch11_vision_evidence_agent.py
uv run --locked mypy python/src/realsight/vision python/tests/test_ch11_vision_evidence_agent.py
```

本章实际结果为：六项 pytest 全部通过；Ruff 格式和规则检查通过；严格 mypy 报告
`Success: no issues found in 3 source files`。

六个测试分别说明：

| 测试 | 说明 |
|---|---|
| 完整标签 | 65W、输出档位、PD/PPS、来源元数据能共同进入 Evidence |
| 功率矛盾 | 65W 与 40W 不一致时不猜测 |
| 协议未知 | 未见协议不等于不支持 PD |
| 低 OCR 置信度 | 清晰图也不能抵消糟糕的字符识别 |
| rejected Observation | C++ 已拒绝时 OCR 调用数为 0 |
| 不支持字段 | `laptop_model` 不会被视觉层假装处理，也不会调用 OCR |

## 13. 常见误解与排查

### 13.1 为什么 `image_path` 存在还会出现 `no_readable_text`

文件存在只说明 artifact storage 没丢工件，不说明标签在图中可读。可能是标签太小、模糊、
反光、遮挡、角度太斜，或 OCR 后端不支持相应语言。先区分路径错误、质量不足与识别失败，
再决定重新观察还是更换识别器。

### 13.2 为什么质量 0.95 仍可能没有 Evidence

0.95 是 C++ 对图像整体条件的判断；若 OCR 对 `65W` 字符只有 0.50，二者取最小后仍不够。
图像清楚却文字识别差，可能是字体、语言、裁剪或后端问题。两种指标不可互相替代。

### 13.3 为什么不直接以模型回答“能充”

因为图片只能提供充电器一侧的有限事实。笔记本输入要求、线缆能力、端口角色和实际协商
还没有进入系统。第 12 章会把“看见的标签”与“设备资料”和“确定性规则”分开，才能给出
带条件和未知边界的结论。

### 13.4 为什么 Evidence 不直接写入 BeliefState

一条 OCR 证据只是候选。持久化与 checkpoint 对账在第 6 章已有边界；冲突、计划、恢复和
接纳顺序由第 13 章统一管理。让子 Agent 直接改全局状态，会让重试、重复提交和冲突解决
变得不可解释。

## 14. 课后练习

1. 为 `TextRecognizer` 写一个“只读取同目录 JSON 标注文件”的开发替身。它必须确认
   `observation_id` 一致，不能仅按文件名匹配。
2. 增加一条测试：标签含 `USB PD 3.0` 与 `QC 4+`，预期协议值的顺序稳定，并解释为什么
   顺序稳定比用无序 `set` 更有利于日志和测试。
3. 只写设计，不改协议：多口 100W 充电器应怎样表示总功率、每个端口和共享功率？说明
   目前 `charger_max_power_w` 为什么不够。
4. 让低置信度字段输出候选 Evidence 加 gap，而非本章的“只 gap”。比较两种设计对审计、
   人工复核和模型训练数据的影响。
5. 用 20 张你自行拍摄并脱敏的标签建立小表：图片条件、人工真值、OCR 原文、字段真值、
   是否应接受。不要急着调阈值，先找错误分布。
6. 设计 `source_metadata` 的大小上限、保留期和读取权限。解释为什么不能无限保存 OCR 原文。

## 15. 本章能力边界

本章已完成：

- 以真实 Pydantic Observation 为视觉入口。
- 可替换的 OCR/VLM 端口。
- 文字区域、OCR 置信度、关键帧质量和来源 Observation 的可追溯链。
- 标签全文、最大输出功率和协议集合的受限归一化。
- 未知、低置信、输入无效与候选矛盾的明确失败语义。
- 不联网的 Demo 和六项测试。

本章没有完成：

- 真实 OCR/VLM 部署或精度评估。
- 通用产品识别、真伪鉴定、品牌/型号猜测。
- 多口充电器端口归属、线缆 e-marker、USB4/Thunderbolt 兼容判断。
- 多语言、弯曲文字、复杂表格、手写体或低光/运动模糊的鲁棒识别。
- OCR 后端的生产级限流、超时、监控、鉴权、脱敏和成本治理。
- 写入 `BeliefState`、查询笔记本资料或做最终兼容结论。

这些限制不是缺点清单，而是模块边界。一个月 MVP 能把“从关键帧提取三项可复核候选证据”
做好，就已经比“声称什么都能识别”更接近可维护工程。

## 16. 到第 12 章的桥接

现在系统终于能从真实世界的关键帧得到这种输入：

```text
charger_max_power_w = 65.0      来源：背标 Observation + OCR 区域
charger_protocol = ["usb_pd"]   来源：背标 Observation + OCR 区域
charger_label = "..."           来源：背标全文
```

但仍然不能回答“能不能给我的电脑充电”。下一章《资料检索与规则引擎》会补上另外两类
来源：用户给出的笔记本型号/官方规格资料，以及确定性的 USB-C PD 功率和协议规则。它将
做三件本章刻意没有做的事：

1. 检索并规范化笔记本的最低/推荐充电要求。
2. 将视觉 Evidence、资料 Evidence 和冲突信息组合成规则输入。
3. 输出有条件、有未知边界、能指回每条证据的兼容性结果。

第 13 章再让主 Agent 根据 `EvidenceGap` 决定是否重拍、询问用户、检索资料或运行规则。
到那时，RealSight 才形成“看见 -> 读出 -> 查证 -> 判断 -> 主动补证”的完整闭环。
