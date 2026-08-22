# 第四周：真实 OCR、正式接线、治理、评测与作品集

前三周回答“代码为什么这样组织”；这一周回答“它在我的设备上究竟表现怎样”。现有项目已
提供 PaddleOCR adapter、正式 provider 装配、治理用量和评测契约，你要做的不是勾选功能，
而是读懂、故障注入、用真实样本验证，并把任何无法验证的能力写进限制。

## Day 19：PaddleOCR 适配器——第三方推理结果怎样进入自己的契约

### 今天结束时你必须会

- 解释 PaddleOCR pipeline、ONNX Runtime、模型版本和 adapter 各自职责。
- 沿代码讲清像素多边形到 0～1 `TextRegion` 的转换。
- 说明模型为何只初始化一次、为何使用延迟可选依赖。
- 为正常、空结果、低置信度、坐标退化、数组错位和后端异常写隔离测试。

### 1. 技术栈分别干什么

**PaddleOCR**提供文本检测与识别 pipeline；**PP-OCRv6**是所选 OCR 模型族；**ONNX Runtime**
执行导出的推理图；**Pillow**在此 adapter 中只读取图片宽高；**PaddleOcrTextRecognizer**
把第三方输出转换成项目 `RecognitionDocument`。

重要的是依赖方向：业务 `VisionEvidenceAgent` 依赖 `TextRecognizer` Protocol，不 import
PaddleOCR。只有配置选择 paddleocr 时，应用启动才构造真实 adapter。普通回放和 CI 不需要
下载模型，这也避免重型依赖故障拖垮所有单元测试。

### 2. 为什么 pipeline 只初始化一次

模型加载通常比单张推理昂贵，还占用大量内存。adapter 构造时建立 pipeline，后续每次
`recognize()` 复用；若每张图重新创建，端到端延迟和内存抖动会非常差。它也意味着要考虑
线程安全和资源关闭：当前单进程教学服务顺序使用，未来高并发需确认第三方 pipeline 是否能
并发调用或建立受控 worker pool，不能想当然共享。

### 3. 第三方输出为什么不能直接透传

PaddleOCR 3.x 结果提供 `rec_texts`、`rec_scores`、`rec_polys`。adapter 必须检查：

- 三个数组长度完全一致。
- text 是非空、长度受限字符串。
- confidence 是非 bool 的 0～1 数字。
- polygon 至少三点，每点恰好 x/y 数字。
- 图片宽高为正。
- 归一化、clamp 后矩形没有塌缩。

外部库即使“通常正确”，边界代码也不能假设其结果永不变形。透传会让错误在 Evidence 或
checkpoint 更晚爆炸，定位更难。

### 4. 坐标归一化

第三方多边形使用图片像素坐标。项目取全部点的 min/max 得到包围矩形，再除以 width/height：

```text
left   = clamp(min(x) / width)
right  = clamp(max(x) / width)
top    = clamp(min(y) / height)
bottom = clamp(max(y) / height)
```

0～1 坐标与分辨率无关，便于后续 UI 或证据 metadata 使用。代价是丢失旋转多边形的精细形状；
当前字段解析只需文字与近似位置，所以可接受。若以后做版面关系，需要保留完整 polygon。

### 5. 稳定 region ID 与 provider metadata

region ID 对 `observation_id + page/line index + text + bounds` 做 SHA-256 摘要。相同输入产生
相同 ID，有利于重放和审计；它不是安全签名，也不能证明图片未被篡改。

RecognitionDocument 保存 provider、PaddleOCR package version、OCR model、engine、推理耗时、
图片宽高。VisionEvidenceAgent 会把 provider metadata 继续写入 Evidence，使简历中的版本和
延迟可以追到真实记录。

### 6. 异常边界

推理时第三方抛错统一包为 `RecognitionFailure`，保留原异常作为 cause；初始化缺包则给出
明确 `uv sync --extra ocr` 提示。空 OCR 结果是合法 RecognitionDocument，不是后端崩溃，
由 VisionEvidenceAgent 转成字段 gap。

`close()` 使用能力检测调用 pipeline 未来可能公开的 close；当前通常无操作。不要假设 Python
垃圾回收会在服务退出前恰好释放所有 native 资源。

### 7. 今日阅读与测试

阅读 `vision/paddle_ocr.py`，给每个 private helper 写“接受/拒绝/输出”表。普通测试注入
FakePipeline 和假的 image_size_reader，不下载模型：

```powershell
uv run --locked pytest python/tests/test_portfolio_readiness.py `
  -k "paddle or ocr" -q
```

确认覆盖：正常多区域、无文本、低 confidence、三数组长度不同、后端抛错、坐标塌缩。再加
两个测试：超长文字、confidence 是 bool。断言错误语义，不依赖第三方真实类。

安装并运行真实模型属于 integration：

```powershell
uv sync --locked --all-groups --extra ocr
$env:REALSIGHT_OCR_TEST_IMAGE = "<absolute-path-to-a-consented-label-image>"
uv run --locked --extra ocr pytest -m integration `
  python/tests/test_ocr_integration.py -q
```

首次模型准备可能需要网络和时间。不要把个人图片路径、模型缓存或环境变量值提交 Git。

### 8. 常见陷阱与答辩

陷阱：每次 recognize 加载模型；普通 CI 下载模型；直接相信第三方数组对齐；坐标忘记除宽高；
clamp 后不检查塌缩；把空 OCR 当 500；只记录解析值不记录 raw text 和版本。

答辩：

1. 为什么 adapter 不解析 USB-C 功率？
2. 为什么真实 OCR 测试与单元测试分开？
3. 多边形变矩形损失了什么？为什么当前接受？
4. 稳定 region ID 能证明和不能证明什么？
5. 模型初始化失败与某张图片识别为空怎样区分？

---

## Day 20：OCR 与字段抽取评测——准确率的分母必须说清

### 今天结束时你必须会

- 区分文本检测、字符识别、字段抽取、Evidence status 和最终 verdict 五层指标。
- 设计不挑样本的小型受控评测，并建立错误分类。
- 用真实数据决定阈值，不从单张成功截图推断准确率。
- 解释为什么 confidence 阈值不是越高越安全的全部答案。

### 1. 五层成功不能压成一个“识别率”

```text
关键帧 accepted
→ 检测到文字框
→ 文字字符正确
→ 功率/协议字段正确
→ Evidence 状态正确
→ verdict 正确且不过度肯定
```

OCR 原文完全正确，字段 parser 仍可能把总功率与单口功率混淆；字段值正确但被错误升级为
confirmed，遇到另一条冲突证据时仍可能产生坏 verdict。作品集至少报告功率字段、协议字段、
端到端 verdict 和错误正结论，不能只写“PaddleOCR 识别率 95%”。

### 2. 建立小型校准集

在 Day 23 的 30 样本前，先采 8～12 张校准图：正常、轻反光、强反光、模糊、倾斜、过暗、
遮挡。采集前编号，不得跑完只保留成功图。每张由人独立记录 visible raw text、最大功率、协议；
真值不能从 OCR 复制。

保持设备、相机、距离、分辨率和代码 commit 可记录；研究一个变量时固定其余变量。校准集可
用于调阈值，但最终报告样本应另采，避免同一数据既调参又宣称泛化性能。

### 3. 错误分类比总准确率更能指导修改

给每次失败标第一处边界：

- perception reject：关键帧门槛问题。
- text detection miss：关键文字区域未检测。
- character substitution：如 `3.25A` 识别成 `3.2SA`。
- field parse miss：文字在但 regex/normalizer 未抽取。
- semantic ambiguity：多个瓦数或档位含义不唯一。
- confidence policy：正确值被降级，或错误值被升级。
- catalog/rule mismatch：视觉正确但规格/规则输出不符。

先修第一处错误，不能看到 verdict 错就直接修改最终规则。

### 4. 如何理解阈值取舍

提高 minimum OCR confidence 通常减少错误 confirmed，但增加 probable/gap 和重拍次数；降低
阈值提高覆盖率，却可能增加错误正证据。对于安全型判断，优先控制 false positive，而不是
追求最大一次完成率。但“全部拒绝”虽然 false positive 为 0，也没有产品价值，所以同时报告
成功率、重拍/失败和延迟。

阈值应由样本分布决定；confidence 未经过校准时，只能当排序/门槛信号，不能解释为真实概率。

### 5. 今日实验

对校准集逐张保存：C++ quality、OCR raw regions/confidence、提取 Evidence/gap、人工 truth。
做一张表比较至少两个 confidence threshold；不要同时调 C++ 清晰度阈值。

必须回答：

- 哪些错误由关键帧改善可解决？
- 哪些需要 OCR/预处理？
- 哪些是标签语义本来就不确定，必须请求更多证据？
- 阈值变化让 false confirmed 和 gap 各变化多少？

为发现的一个 parser 边界写回归测试，使用最小文本 fixture，而不是把整张真实图片塞进普通
单元测试。

### 6. 常见陷阱

- 把模型自带 benchmark 当自己的设备指标。
- 在最终 30 样本上反复调参再报告同一批准确率。
- 排除所有困难/失败图。
- 只报告平均 confidence，没有字段准确率。
- 看到低置信度就删结果，不分析是否系统性字符错误。
- 为了 false positive=0 把所有输入都判 unknown，却不报告成功率。

### 7. 今日答辩

1. OCR 原文正确但 verdict 错，排查顺序是什么？
2. 为什么置信度 0.9 不等于 90% 正确？
3. 调阈值集与最终评测集为什么应分开？
4. false positive 为 0 为什么仍可能是无用系统？
5. 哪些失败应该重拍，哪些应该改 parser？

---

## Day 21：正式服务器装配——配置选择真实 C++ 和 OCR

### 今天结束时你必须会

- 解释 dependency injection 和 composition root。
- 沿 `serve.py` 讲清配置到具体 provider/recognizer/service 的构造。
- 独立启动 C++ 摄像头服务与 Python API，完成真实任务到至少一个暂停/终态。
- 证明四种 provider 组合表现诚实且退出时资源关闭。

### 1. 什么是启动装配

业务类不应该自己读取 TOML 再决定创建 PaddleOCR 或 gRPC channel；否则测试很难替换依赖，
配置逻辑散在深层。`serve.py` 是 composition root：唯一负责读取配置、选择具体 adapter、注入
TaskService、启动 Uvicorn 和退出清理的地方。

依赖注入不是框架专属术语：把 `recognizer` 作为构造参数传给 VisionEvidenceAgent，就是把
“需要什么能力”与“本次运行用哪个实现”分开。

### 2. 五段真实装配链

```text
load_settings(TOML + allowlisted env overrides)
→ planner_from_settings(deterministic/openai)
→ _recognizer_from_settings(unavailable/paddleocr)
→ _observation_provider_from_settings(None/grpc client)
→ TaskService.with_sqlite(dependencies + governance)
→ create_app(service) → uvicorn
```

默认 unavailable 是诚实能力声明：未配置 C++ 时停在观察请求；未配置 OCR 时形成识别缺口。
绝不能静默切到 scripted recognizer 并让正式 API 看似成功。

### 3. 为什么 C++ 服务独立启动

C++ 进程拥有摄像头、队列和原生库。Python API 不隐式 `Popen` 它，避免 Web server reload
重复占用摄像头，也让两侧崩溃、日志和版本边界清晰。运维需要显式启动两进程；生产化可由
service manager/container 管理，但不是 FastAPI import 时偷偷启动。

### 4. 配置与密钥边界

TOML 只允许已知 top-level 和 provider 字段；环境变量也用白名单映射。相对 data_dir 相对
配置文件定位。OpenAI key 不进入 TOML、checkpoint、日志或 API，由 SDK 从环境读取。

配置校验只能防拼写/类型错误，不是权限系统。`allowed_capabilities` 仍需工作流运行时强制，
否则改了 TOML 值但业务完全不执行检查。

### 5. 资源生命周期

TaskService close 关闭 ObservationProvider，后者关闭 gRPC channel；service 还关闭 SQLite。
`serve.py finally` 再调用 recognizer 的可选 close。即使 Uvicorn 退出或抛错，也应执行清理。
当前没有验证强杀进程时的优雅清理，真实设备测试需观察残留文件锁/端口。

### 6. 四组合测试

先读并运行 factory 测试：

```powershell
uv run --locked pytest python/tests/test_portfolio_readiness.py `
  -k "runtime_factories or runtime_configuration" -q
```

建立本地配置副本，不提交：

| perception | vision | 预期 |
|---|---|---|
| unavailable | unavailable | 停在 WAITING_OBSERVATION |
| grpc | unavailable | 得到关键帧，OCR gap，受治理后失败/再请求 |
| unavailable | paddleocr | 没 Observation，OCR 不会凭空运行 |
| grpc | paddleocr | 真实闭环，可继续询问型号与规则 |

### 7. 真实启动顺序

终端 A：

```powershell
build\windows-gcc-debug\cpp\realsight-perception-grpc.exe `
  --camera 0 --listen 127.0.0.1:50051 `
  --artifacts runtime-data\camera-artifacts --max-frames 180
```

终端 B 使用本地 TOML（provider 改为 grpc/paddleocr）：

```powershell
uv run --locked --extra ocr realsight-api `
  --config <local-real-device-config.toml> --host 127.0.0.1 --port 8000
```

用 OpenAPI 或 HTTP 客户端创建任务、查看 interrupt、恢复型号、订阅 WebSocket。至少验证：
C++ 未启动、OCR 缺依赖、正常关键帧、进程退出后 channel/SQLite 可再次打开。

### 8. 常见陷阱与答辩

陷阱：应用 import 就启动摄像头；默认 fake 冒充真实；构造依赖散落多个 endpoint；provider
地址硬编码；API 退出不 close；绑定 `0.0.0.0` 暴露无认证教学 API。

答辩：

1. composition root 为什么应集中？
2. 默认 unavailable 为什么是优点？
3. C++ 为什么不由 FastAPI 隐式启动？
4. 配置校验为什么不能替代治理？
5. 哪些资源由谁关闭？

---

## Day 22：治理真正进入主循环——先检查、原子扣减、持久失败

### 今天结束时你必须会

- 区分 capability、budget、usage 和 policy ID。
- 指出每种预算在真实工作流哪里检查以及为什么在副作用前。
- 解释 `GovernanceUsage.consume()` 的不可变、原子语义。
- 用测试证明预算耗尽会形成 FAILED 终态且拒绝恢复。

### 1. 治理不是配置表

写了 `max_observations=4` 但执行前从不读取，它就只是注释。正式实现把策略上限和已用计数
一起放入 `GovernanceUsage`，随 checkpoint 持久化：跨 interrupt/resume 后不能重新从 0 计数。

capability 回答“允不允许做”；budget 回答“最多做多少”；usage 回答“已经用了多少”；
policy_id 回答“是哪套政策做出的决定”。

### 2. 五种上限的语义

- `max_iterations`：跨节点/恢复的工作轮数，防逻辑循环。
- `max_commands`：Planner 决策轮数。
- `max_observations`：REQUEST_VIEW 次数。
- `max_external_attempts`：感知、OCR、规格检索、模型等外部边界尝试总数。
- `max_cost_units`：抽象模型费用单位，不等于真实货币账单。

它们有重叠但用途不同。例如一次 RequestView 会消耗 observation 和 external attempt；恢复后
OCR 再消耗一个 external attempt。这样能分别限制摄像头打扰次数和所有外部资源总量。

### 3. 为什么必须在副作用前检查

plan node 先检查 iteration/command/model cost，再调用 Planner；REQUEST_VIEW 在进入 interrupt
前预留 perception capability、observation 和 external attempt；apply observation 在 OCR 前
再扣 external attempt；规格检索在 lookup 前扣。

若先调用模型/摄像头/OCR 再发现超限，预算已经无法阻止费用与副作用。测试应使用
`MustNotRunPlanner`/`MustNotRunRecognizer` 证明后端没有被调用，而不只断言最后 status failed。

### 4. 原子 consume

`consume(commands=1, external_attempts=1)` 先计算所有 next values，再逐项检查；任何一项超限
就抛 GovernanceViolation，原对象保持不变，不会只扣 command 没扣 external。成功则返回新的
冻结 GovernanceUsage，调用者必须把它写回 state。

这保证单次 state 更新的逻辑原子性，但不是数据库多进程事务。多个 API 实例同时修改同一
thread 仍需要更强的并发控制，当前项目没有宣称解决。

### 5. 违反策略为何是 FAILED 终态

`_governance_failure_update()` 清空 pending action/request，把 session 设 FAILED，写 TASK_FAILED
event 和安全 code。失败后 resume 被拒，避免客户端用旧 interrupt 绕过上限。治理失败不是
USB-C verdict，final answer 不能伪装成兼容结论。

### 6. 今日测试矩阵

```powershell
uv run --locked pytest python/tests/test_portfolio_readiness.py `
  -k "budget or capability or governance or automatic_observation" -q
```

逐个把上限设到最小，记录“哪次动作成功、哪次被拒、后端是否调用、usage 最终值、event code”：

1. 移除 `perception.observe`。
2. max_iterations=1，跨 resume 后再 plan。
3. max_commands=1，第二次规划前失败。
4. max_observations=1，第二次重拍前失败。
5. max_external_attempts=1，Observation 后 OCR 前失败。
6. OpenAI planner cost=2、max_cost_units=1，模型调用前失败。
7. 一次 consume 同时增加两项，其中一项超限，证明两项都未部分扣减。

### 7. 常见陷阱

- usage 只存在内存，恢复后归零。
- 副作用后才检查预算。
- capability 只控制 UI 是否显示按钮，后端仍可调用。
- 把 retry 当免费，不计 external attempts。
- 失败后保留 pending interrupt，允许再次恢复。
- 把抽象 cost unit 写成真实美元节省金额。

### 8. 今日答辩

1. max_iterations 与 max_commands 有何差别？
2. 一次观察为什么可能消耗两次 external attempt？
3. consume 的“原子”在这里准确指什么，不指什么？
4. 为什么预留 observation 在 interrupt 前？
5. 终态 FAILED 后为何必须拒绝 resume？

---

## Day 23：30 个真实样本与指标——让每个数字都可复算

### 今天结束时你必须会

- 按固定矩阵采集 30 个不挑选结果的样本。
- 独立填写严格 JSONL，区分人工 truth 与系统 prediction。
- 手算并用脚本复算接受率、字段准确率、false positive、p50/p95 和丢帧率。
- 对至少一个失败样本定位第一处错误边界。

### 1. 为什么固定矩阵

只拍“容易成功”的图，指标没有意义。项目规定 30 个样本：normal 5、tilted 4、mild_glare 4、
strong_glare 4、blurred 4、underexposed 3、partially_occluded 3、conflicting_print 3。每项尽量
只改变主要变量，失败样本不能删除后只留重拍成功版。

如果没有足够真实设备，可以完成数据契约和演练，但不能填写简历实测数字，也不能通过
Week 4 Gate。诚实的“尚未完成真实设备评测”优于示例数据冒充结果。

### 2. truth 必须独立于系统

人工在运行待测系统前记录最大功率、可见协议和按同一规格规则计算的期望 verdict。不能将
OCR 文本复制成真值，也不能看到系统答案后调整 truth。真实规格需记录来源、版本和核验日期；
若仍使用虚构教学目录，报告标题与限制必须明确。

冲突打印样本是受控测试工件，不要伪造真实厂商标签，也不要把它描述成真实设备缺陷。

### 3. 一行 JSONL 保存什么

`DeviceSample` 同时保存 artifact 引用/哈希、truth、perception signals/帧统计、OCR raw text/
版本、Evidence 字段/冲突、predicted outcome 和五段 latency。未知字段、重复 sample ID、非法
0～1 分数都拒绝，确保报告来源结构一致。

大体积视频可不公开，但应保存本地受控工件、相对路径和 SHA-256；公开图片必须确认无个人
信息、序列号或其他隐私。

### 4. 指标精确定义

- keyframe acceptance = accepted 样本 / 全部样本。高不总是好。
- power accuracy = 预测功率与真值差 ≤0.5W / 全部样本；无 outcome 算错，不从分母排除。
- protocol accuracy = casefold/strip 后精确相等 / 全部样本。
- verdict accuracy = verdict 完全相等 / 全部样本。
- false positive = prediction conditions_met 且 truth 不是 conditions_met 的数量。
- drop rate = 总 dropped / 总 produced，同时报告三个原始数。
- p50/p95 = 排序后固定线性插值，不是平均值，也不只统计成功样本。

安全底线是证据缺失/冲突时错误正结论为 0；但还需同时报告总体成功和准确率，防止“全拒绝”
伪装安全。

### 5. 采集与运行顺序

完整执行 [真实设备评测手册](../REAL-DEVICE-VALIDATION.md)。先复制示例 manifest 为未追踪本地
文件，逐行填写，最后运行：

```powershell
uv run --locked python scripts/evaluate_device_dataset.py `
  test-data/device-evaluation/manifest.jsonl `
  --json-output runtime-data/device-report.json `
  --markdown-output runtime-data/device-report.md
uv run --locked pytest python/tests/test_device_evaluation.py -q
```

生成 Markdown 后抽三行手算分母，核对脚本。报告要附 commit、配置、相机、OCR package/model、
样本数和日期；否则以后无法复现。

### 6. 失败复盘

至少选一个失败，按下列顺序定位：

```text
artifact 是否正确
→ C++ accepted/issues 是否合理
→ OCR raw text 第一处差异
→ field parser/Evidence status
→ Belief conflict/missing
→ catalog/rules
→ API 最终映射
```

写最小回归测试时固定第一处错误，不要修改后面规则掩盖上游错。若失败是启发式能力边界，
可以不修，但要解释系统怎样保守失败以及未来方案。

### 7. 常见陷阱与答辩

陷阱：用 example manifest 当成绩；从分母删除无 outcome 样本；只计成功请求延迟；先看结果再
改 truth；把 acceptance 当 accuracy；手改 Markdown 数字；失败样本不公开。

答辩：

1. 关键帧接受率 100% 为什么可能危险？
2. outcome=null 为什么仍在字段准确率分母？
3. p95 与平均值回答的问题有什么不同？
4. dropped/produced 为什么要同时报告原始总数？
5. false positive=0 之外还必须报告什么？

---

## Day 24：作品集、简历和 30 分钟终局答辩

### 今天结束时你必须交付

- 可复现公开仓库、架构图、回放/真实设备说明、CI 和限制。
- 真实指标表与至少一个失败样例；没有数据就明确标为待完成。
- 2～4 分钟演示视频和 30 秒/3 分钟/10 分钟三版讲稿。
- 一次 30 分钟连续答辩和一次 60 分钟现场小改动。

### 1. README 应按读者问题组织

1. 一句话：项目解决什么，条件结论而非真实电气协商。
2. 架构图：进程、数据类型、artifact/checkpoint、模型/规则边界。
3. 五分钟回放 quick start：无摄像头/OCR 也能验证工作流。
4. 真实设备 mode：C++ 和 API 两进程、可选 OCR 依赖、配置副本。
5. 测试/CI：具体命令和门禁。
6. 真实指标：n、分母、commit、失败样例。
7. 设计取舍：C++/Python、Observation/Evidence、模型/规则、背压。
8. 限制：OCR、教学规格、单进程 API、无认证/TLS、无真实 PD 协商。

架构图箭头必须标数据名，不能只画方框。演示从一个问题开始，展示 progress/interrupt、证据
与未知边界，不用大量时间滚动代码。

### 2. Git 历史表达自己的工作

至少让历史清楚区分：上游教学基线、环境复现、本地 OCR、正式 gRPC/OCR 接线、治理、真实
设备评测、文档/演示。不要重写来源把教学骨架伪装成自己从零设计，也不要把所有工作压成
`final` 一个 commit。

公开前检查 `.env`、API key、个人绝对路径、runtime-data、模型缓存、大视频、数据库和图片
隐私。Git 删除当前文件不代表历史中的密钥消失；发现密钥要轮换并清理历史。

### 3. 三版项目讲稿

**30 秒版**：

> 我复现并扩展了一个证据驱动主动视觉系统。C++20/OpenCV 在连续视频中通过有界队列和质量
> 门槛选择关键帧，gRPC 将 Observation 交给 Python；Python 用 OCR 形成可追溯 Evidence，
> LangGraph 管理中断恢复，最终由确定性 USB-C 规则而非大模型输出条件结论。项目还实现了
> 治理预算、API/WebSocket 和真实样本评测；它不等同于实际 USB PD 协商。

**3 分钟版**按“问题→架构→三项取舍→一次失败→指标/限制”。

**10 分钟版**再加入完整状态时间线、C++ 停止/背压、契约变化和你亲自完成的改造。不要背
库名清单；每个技术栈都回答“它在本项目具体解决哪个问题”。

### 4. 简历 bullet 的证据规则

只写你能现场打开源码、测试或报告证明的内容：

- “复现并扩展”，明确来源。
- C++ 帧率/丢帧率使用真实报告数字。
- gRPC 可写 streaming/deadline/Cancel，因为有联调和测试。
- OCR 写“接入/适配/评测”，不写训练模型。
- LangGraph 写可暂停恢复和证据工作流，不写生产级分布式 Agent。
- 错误正结论、成功率、p95 全部使用真实 n=30 结果。

任何方括号占位符未替换，就不进入最终简历。

### 5. 30 分钟答辩结构

| 时间 | 内容 | 必须证明 |
|---:|---|---|
| 0～3 分钟 | 问题与边界 | 条件判断，不是电气实测 |
| 3～8 分钟 | 全链路图 | 每个数据由谁产生/消费 |
| 8～13 分钟 | C++ runtime | move/clone、背压、stop |
| 13～18 分钟 | Evidence/Agent | 三类 confidence、受限 Action |
| 18～22 分钟 | LangGraph/API | 两次 interrupt、事件重放 |
| 22～25 分钟 | governance | 检查位置、持久用量、终态 |
| 25～28 分钟 | 真实指标 | 分母、失败样例、false positive |
| 28～30 分钟 | 限制与下一步 | 不夸大生产与空间感知能力 |

面试者可能在任何处追问“如果删除这一层会怎样”。用具体失败回答：例如删除 Evidence，规则
无法追溯 OCR；删除 bounded queue，慢消费导致积压；删除 interrupt ID 校验，旧表单污染新暂停。

### 6. 60 分钟现场修改模拟

随机抽一题：

1. 给 Observation 增加可选 camera ID，先做跨语言影响分析。
2. 新增一种 OCR provider fake 与 adapter contract test。
3. 增加一个 RunEvent 并验证 WebSocket 重放。
4. 给 quality evaluator 增加一个确定性 issue。
5. 新增一种 governance budget 并证明副作用前拒绝。

计时安排：10 分钟读入口/列不变量，10 分钟写失败测试，25 分钟最小实现，10 分钟回归，5 分钟
解释取舍和未完成项。若来不及，保留绿测试和清楚的下一步，不用大范围复制代码制造假完成。

### 7. 最终自动化验收

```powershell
uv run --locked pytest -m "not integration"
uv run --locked ruff format --check python/src python/tests scripts
uv run --locked ruff check python/src/realsight python/tests scripts
uv run --locked mypy python/src/realsight python/tests
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
uv run --locked python scripts/run_ch10_demo.py
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
uv run --locked python examples/ch13_main_agent_loop.py
uv run --locked python examples/ch14_api_replay.py
```

真实 OCR、真实摄像头和 30 样本报告单独记录；普通 CI 的绿色不能替代它们。

### 8. Week 4 / 简历 Gate

必须全部满足：

- 回放和真实摄像头两种演示都能独立运行。
- PaddleOCR 真实 integration 成功，fake tests 不依赖模型下载。
- 正式 API 通过配置接入 gRPC/OCR，默认 unavailable 不伪造能力。
- 五种预算与 capability 有正式测试。
- 30 个预先编号样本可从 JSONL 复算，错误正结论为 0。
- 能展示一个失败样例、第一处错误边界和回归测试。
- 能连续答辩 30 分钟，60 分钟完成一个小改动。
- 主动说明仍无认证/TLS、多实例事件总线、真实厂商知识库和 USB PD 电气协商。

若缺真实硬件或模型环境，可以达到“代码理解与回放合格”，但不要把项目作为简历核心实测
经历。补齐真实 Gate 后再使用 [总路线中的简历表述](../PORTFOLIO-ROADMAP.md#五简历表述)。

## 四周结束后的空间感知延伸

现在你掌握的是单目标关键帧与证据工作流，不是三维空间感知。后续按以下依赖顺序推进：

```text
线性代数/坐标变换
→ 相机模型、内外参与标定
→ 特征、光流、PnP、RANSAC、检测和跟踪
→ Eigen/Sophus/Ceres/ROS2
→ Visual Odometry、VIO、SLAM
→ ONNX Runtime/TensorRT/CUDA 边缘部署
→ RealSight track_id、位姿、跨帧状态和多摄像头 Observation
```

学习新阶段仍沿用本课方法：先定义数据与坐标系契约，再区分高频感知和低频决策，建立可回放
数据集与失败指标，最后才添加更复杂模型。
