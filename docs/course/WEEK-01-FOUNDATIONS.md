# 第一周：基础、契约与系统边界

这一周不追求写很多业务代码，而是建立一套不会混层的心智模型。周末你应该能回答：
“一条用户问题怎样变成可验证结论，每一层拿什么数据、承担什么责任、失败后留下什么？”

## Day 1：环境、命令行、构建与 Git——先证明项目真的能运行

### 今天结束时你必须会

- 解释目录、进程、端口、环境变量和退出码，不再把它们统称为“环境问题”。
- 区分 Python 解释执行、C++ 编译链接、依赖安装和程序运行。
- 知道 `uv`、CMake、CTest、pytest、Ruff、mypy、Git 分别解决什么。
- 建立一份可重复的基线：以后任何失败都能与今天比较。

### 1. 五个最基础的运行概念

**目录**是文件的组织位置。PowerShell 的“当前目录”决定相对路径从哪里开始解析。比如
`config/realsight.example.toml` 只有在仓库根目录运行时才指向预期文件；代码中的
`Path(__file__)` 则以源码位置为锚，不受当前目录影响。

**进程**是一个正在运行的程序实例。C++ gRPC server 和 Python FastAPI 是两个进程；
即使源码在同一仓库，它们也不能直接访问彼此的内存，所以需要 gRPC 和文件工件交换。

**端口**可以理解为一台机器上某个网络服务的门牌号。`127.0.0.1:50051` 是本机 C++
gRPC，`127.0.0.1:8000` 是本机 HTTP API。端口被占用是运行期资源冲突，不是编译错误。

**环境变量**是进程启动时读取的外部配置。例如 `OPENAI_API_KEY` 不应写进 TOML 或 Git；
子进程通常继承父进程环境，但已经运行的进程不会自动读到你后来修改的变量。

**退出码**是进程结束时交给操作系统的整数。通常 0 表示成功，非 0 表示失败。自动化
系统依赖退出码，不会像人一样“看日志感觉应该成功”。

### 2. 工具栈分别干什么

| 工具 | 作用 | 它不证明什么 |
|---|---|---|
| `uv` | 创建 Python 环境、按 `uv.lock` 安装并运行命令 | 不编译你自己的 C++ 逻辑 |
| Python | 加载 `.py` 并执行 | import 成功不代表 C++ 库存在 |
| CMake | 根据平台和依赖生成构建规则 | 配置成功不代表测试通过 |
| 编译器/链接器 | 把 C++ 源码变为可执行文件并连接 OpenCV/gRPC | 构建成功不代表业务正确 |
| CTest/pytest | 运行 C++/Python 行为测试 | 测试绿不代表真实摄像头准确率 |
| Ruff | 格式和静态代码问题 | 不执行业务流程 |
| mypy | 检查 Python 类型关系 | 类型正确不代表数值规则正确 |
| Git | 保存可比较的源码快照 | commit 不会自动备份到 GitHub |

`pyproject.toml` 表示“项目直接声明需要什么”；`uv.lock` 保存完整解析结果和精确版本。
前者像菜单，后者像已确认的采购清单。`--locked` 的目的，是发现声明与锁文件不一致时
直接失败，而不是在你没注意时换一套依赖。

### 3. 为什么 RealSight 同时用 Python 和 C++

C++ 靠近摄像头和高频帧：帧很大、到达快、需要明确所有权、背压和停止。Python 靠近
工作流：类型契约、Agent 规划、状态恢复、HTTP 和测试迭代更方便。选择两种语言会增加
构建和协议成本，但把“实时像素路径”和“低频业务决策”分开，是本项目最重要的边界。

陷阱是把“C++ 更快、Python 更慢”当成全部理由。真正的选择还包括生态、故障隔离、
资源生命周期、团队能力和数据跨边界的代价。

### 4. 今日代码阅读路径

按顺序只回答“它配置了什么”，不要钻进业务代码：

1. `pyproject.toml`：Python 版本、依赖、命令入口、测试/静态检查配置。
2. `config/realsight.example.toml`：运行时 provider 和治理默认值。
3. `CMakePresets.json`：Windows 构建目录和生成器。
4. `cpp/CMakeLists.txt`：runtime、proto、transport、apps、tests 怎样分层。
5. `.github/workflows/ci.yml`：远端会重复哪些质量门禁。
6. `.gitignore`：哪些是源码，哪些只是本机产物。

### 5. 实验：建立基线

先在笔记里写“每条命令成功时我预计看到什么”，再运行：

```powershell
git status --short
uv sync --locked --all-groups
uv run --locked realsight-doctor --config config/realsight.example.toml
uv run --locked pytest -m "not integration"
uv run --locked ruff format --check python/src python/tests scripts
uv run --locked ruff check python/src/realsight python/tests scripts
uv run --locked mypy python/src/realsight python/tests
uv run --locked cmake --preset windows-gcc-debug
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
uv run --locked python scripts/run_ch10_demo.py
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
uv run --locked python examples/ch13_main_agent_loop.py
uv run --locked python examples/ch14_api_replay.py
```

不要只记录“成功”。记录关键标志：doctor 的 `READY=true`、CTest 的 `6/6`、gRPC 的
`CH10_DEMO_OK/CH10_CANCEL_OK`、主循环的两个 interrupt、API 的事件重放。

### 6. 故障注入

在没有启动 gRPC server 时运行：

```powershell
uv run --locked python examples/ch10_observation_stream.py `
  --address 127.0.0.1:59999 --timeout-ms 500
```

观察它与下面三种失败的区别：

- `ModuleNotFoundError`：Python 依赖或解释器问题。
- CMake `Could NOT find ...`：原生开发包或查找路径问题。
- `connection/deadline`：程序已运行，但另一进程或端口不可用。

你今天不是要修所有失败，而是训练“先分类再排查”。

### 7. 常见陷阱

- 在错误目录运行，然后把相对路径失败误认为文件不存在。
- 看到 `.venv` 就认为 C++ OpenCV 头文件也已安装。
- 修改依赖却忘记更新锁文件，自己的机器能跑，CI 不能。
- 为了“解决端口冲突”随意结束不属于本任务的进程。
- 只保存截图，不保存命令、退出码、版本和 commit。

### 8. 今日产物和答辩

产物：复制 [每日记录模板](DAY-NOTES-TEMPLATE.md)，写一张“源码→构建→进程→端口”的图。

答辩题：

1. Python 测试全过，为什么 C++ 仍可能完全不能构建？
2. `uv.lock` 和 Git commit 有什么不同？
3. 端口 50051 被占用属于哪一阶段的问题？
4. 为什么 `runtime-data` 不应该提交？

合格答案必须指向具体工件或生命周期，不能只说“为了规范”。

---

## Day 2：Python 最小语言模型——读懂项目对象和接口

### 今天结束时你必须会

- 读懂函数参数/返回值、类、Enum、dataclass、异常和类型标注。
- 解释 `list/tuple/dict/set` 在状态模型中的不同意图。
- 理解 `Protocol` 是“调用者需要的能力”，不是某个具体实现。
- 独立写一个最小 Evidence 合并练习和五个测试。

### 1. Python 在本项目里不是“写脚本”

Python 代码承担三类工作：

1. **数据建模**：Pydantic 类规定合法状态。
2. **纯逻辑**：Evidence 合并、规格匹配、规则计算。
3. **副作用编排**：调用模型、gRPC、SQLite、HTTP，但把连接留在依赖对象中。

理解代码时先判断当前函数属于哪类。纯逻辑函数应容易单测；副作用函数需要 timeout、
cancel、资源关闭和 fake adapter。

### 2. 四种容器为什么不随便换

- `list`：可变、有顺序，适合函数内部逐步收集候选。
- `tuple`：有顺序、不可变意图，适合 checkpoint 中的动作和事件快照。
- `dict`：按 key 找值，Belief 用字段名找活跃 Evidence，ledger 用 ID 找原始记录。
- `set/frozenset`：关心成员而非顺序，适合 missing field、capability 和 observed view。

项目里把公共状态多写成 tuple/frozenset，不是“这样更高级”，而是减少对象创建后被某个
调用者原地修改的风险。注意：不可变容器并不自动让容器里的所有对象都不可变；Pydantic
模型还用 `frozen=True` 补上这一层。

### 3. 函数、类、Enum、dataclass 和异常

**函数**把输入映射为输出。读函数先看签名，再看返回，而不是先读每一行实现。

**类**把数据和相关行为放在一起。Pydantic 类偏向“合法数据”，TaskService 偏向“用例
行为”，不要因为都叫 class 就认为职责相同。

**Enum**限制值域。`SessionStatus` 比任意字符串安全，因为拼错 `compeleted` 无法悄悄
进入系统。

**dataclass**适合依赖容器或轻量结果，例如 `MainAgentDependencies`；它没有自动替你完成
全部业务校验。

**异常**表示当前操作无法按约定完成，例如文件不存在或后端崩溃。`not_found`、
`insufficient_evidence` 等预期业务结果不应都抛异常，否则调用者无法区分“系统坏了”和
“现实里确实没有足够信息”。

### 4. 类型标注和 Protocol

`def observe(request: ObservationRequest) -> Observation` 告诉读者和 mypy：输入输出应该是
什么。Python 运行时不会因为写了标注就自动校验所有值，所以边界仍需要 Pydantic。

`Protocol` 表达结构化接口：只要对象有所需方法，就能被调用者使用。项目中
`TextRecognizer` 可以由脚本替身或 PaddleOCR 实现；工作流只依赖 `recognize()`，不依赖
第三方库细节。这叫依赖倒置：高层业务规定需要的能力，低层适配器来满足。

### 5. 今日代码阅读路径

1. `python/src/realsight/contracts/models.py`：先找 Enum，再看 `Evidence`。
2. `python/src/realsight/vision/evidence_agent.py`：找 `TextRecognizer(Protocol)`。
3. `python/src/realsight/workflow/replay.py`：比较 Replay、gRPC 和 unavailable 三种实现。
4. `python/src/realsight/workflow/main_agent.py`：只看 `MainAgentDependencies`，不要读图。

给每个类写四列：数据、方法、副作用、生命周期。你会发现 Protocol 本身不拥有连接；
具体 provider 才拥有。

### 6. 实作：自己写一个最小证据模型

在个人学习目录中实现，不复制正式 `Evidence`：

```python
from dataclasses import dataclass
from enum import StrEnum

class Status(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"

@dataclass(frozen=True)
class MiniEvidence:
    evidence_id: str
    target_id: str
    field: str
    value: object
    status: Status
```

然后写 `summarize(items, required_fields)`，返回 confirmed、unknown、conflict。你必须自己
决定：同字段同值怎样处理、同字段不同值怎样处理、不同 target 是否拒绝。先写规则，再
写实现。

至少五个 pytest：正常确认、缺字段、重复 ID、同字段冲突、错误 target。测试名必须描述
行为，例如 `test_different_values_for_one_field_become_conflict`。

### 7. 故障注入与陷阱

- 把默认参数写成 `items=[]`，连续调用会共享同一个 list；这叫可变默认参数陷阱。
- 用 `if value:` 判断功率是否存在，会把合法的 0 和缺失混为一谈；应明确 `is None`。
- Python 中 `bool` 是 `int` 的子类，边界校验不能只靠 `isinstance(x, int)`。
- 捕获 `except Exception: pass` 会把真实故障伪装成空结果。
- 把 Protocol 当作基类层级，写大量无意义继承；结构接口不要求继承。

### 8. 答辩

1. 为什么 Belief 的 ledger 用 dict，而 events 用 tuple？
2. fake recognizer 没有继承 TextRecognizer，为什么仍可使用？
3. “没有识别到文字”应该是异常还是业务缺口？什么时候分别使用？
4. 类型标注为什么不能替代 Pydantic？

满分回答必须给出项目中的类名和一个失败例子。

---

## Day 3：Pydantic 与 Protobuf——让边界上的数据不能自相矛盾

### 今天结束时你必须会

- 解释 schema、validation、serialization 和 compatibility。
- 看懂字段约束、field validator、model validator、判别联合和严格模式。
- 说明 Pydantic 与 Protobuf 为什么同时存在，不能互相替代。
- 新增字段前先完成影响分析，而不是直接改 `.proto`。

### 1. 什么是契约

契约不是“给字段写个类型”这么简单，而是参与者共同遵守的可验证约定。比如一条合法
Observation 不只要求 `status` 是枚举，还要求：accepted 不能带失败原因，rejected/failed
必须带原因，时间必须有时区，质量分必须在 0～1，target/request ID 必须对应当前任务。

没有契约时，错误会在远离源头的位置爆炸：规则层拿到字符串功率才发现 OCR 格式错；
有契约时，非法值在 HTTP、Proto 适配或 Pydantic 模型入口立即被拒绝。

### 2. Pydantic 在本项目中的作用

`StrictContract` 统一设置：

- `extra="forbid"`：拼错字段不能被静默忽略。
- `frozen=True`：checkpoint 快照创建后不允许原地改。
- `strict=True`：尽量不把字符串 `"65"` 偷偷转成数字 65。

`Field(ge=..., le=...)` 约束单字段；`field_validator` 检查一个字段的集合规则；
`model_validator` 检查字段之间的关系。例如 Action 的 `action_type` 必须与
`payload.kind` 一致，避免外层写 ask_user、内层却携带 request_view。

判别联合先读取 `payload.kind` 决定使用哪个 payload 模型。它比“一个 payload 放十几个
全是 optional 的字段”更安全，因为每种动作只能拥有自己的参数形状。

### 3. Protobuf 在本项目中的作用

Pydantic 是 Python 进程内/JSON 边界的业务契约；Protobuf 是 C++ 和 Python 之间的二进制
传输契约。`.proto` 生成两端代码，减少手写字节解析，但生成类仍不能自动理解全部业务
规则，所以 Python `grpc_client.py` 会再次映射到 Pydantic。

字段编号是线上的身份，不是显示顺序。发布后把 `9` 从 `image_path` 改给别的含义，旧端
会把新数据解释成旧字段。新增字段用新编号；删除字段应保留/废弃编号，不复用。

Proto `optional double` 能区分“没有计算”和“计算结果为 0”。普通 proto3 标量的默认 0
若没有 presence，会让这两种现实含义混在一起。

### 4. 为什么 Evidence、Belief 和 Action 不进 Proto

C++ 只负责感知，不拥有业务证据和 Agent 状态。若为了“统一模型”把所有 Python 业务类
都塞进 Proto，会扩大跨语言耦合：改一个规则状态也要重编 C++，并诱导 C++ 开始判断
USB-C。当前 Proto 只包含 C++ 真正需要的 Observation 请求/事件/取消。

### 5. 今日代码阅读路径

1. `StrictContract` 和几个类型别名：Identifier、UnitFloat、AwareDatetime。
2. `ObservationRequest → Observation → Evidence → BeliefState`。
3. Action payload 判别联合与 `Action` 跨字段 validator。
4. `RealSightGraphState` 的引用完整性 validator。
5. `contracts/realsight.proto`，逐字段对比 Observation 的 Python 版本。
6. `perception/grpc_client.py` 的显式转换，不读生成文件内部实现。

### 6. 实验：故意破坏契约

在一个学习脚本里分别构造：

- `confidence=1.2`。
- `captured_at=datetime.now()`（没有时区）。
- accepted Observation 加 `failure_reason`。
- Action 外层 `ASK_USER`、内层 `RequestViewPayload`。
- Belief 的 confirmed 与 unknown 同时含同一字段。
- Evidence 的 target 指向笔记本，却写入充电器 Belief。

每次先写“我预计哪个 validator 报什么含义”，再运行。不要只断言“会抛 ValueError”；
测试应匹配关键错误语义，证明失败发生在你认为的边界。

运行正式契约测试：

```powershell
uv run --locked pytest tests/test_ch04_state_and_contracts.py -q
uv run --locked pytest python/tests/test_ch10_grpc_adapter.py -q
```

### 7. 字段变更影响分析练习

假设给 Observation 增加可选 `camera_id`，不改代码，先列清单：

1. Python Pydantic 字段和验证。
2. Proto 使用全新编号 14。
3. 重新生成 C++/Python stub。
4. C++ service 填值。
5. Python adapter 检查 presence 并映射。
6. checkpoint 旧数据是否仍能读取。
7. API 是否需要暴露。
8. 测试成功、有值缺失、旧消息兼容。

能先做影响分析，是 L3 工程能力；直接改到编译报错再四处补，是被工具牵着走。

### 8. 常见陷阱和答辩

陷阱：把 strict 当绝对安全；把 JSON Schema 当业务逻辑；手改 generated 文件；复用 Proto
字段号；将 `None`、0、空字符串视为同一件事；用一个跨字段 validator 做完所有逻辑。

答辩：

1. 为什么 accepted Observation 的 failure_reason 必须为空？
2. 为什么 `target_ratio=None` 与 `0.0` 不同？
3. 为什么 Python 收到生成的 Proto 类后还要 Pydantic？
4. 为什么 Action 使用判别联合？
5. 为什么运行时 channel 不能进入 checkpoint？

---

## Day 4：Agent、工具、工作流与证据——谁有权决定什么

### 今天结束时你必须会

- 不把“调用了大模型”当作 Agent 的完整定义。
- 区分 Planner、Action、Tool/Service、Workflow 和确定性规则。
- 解释 OCR 输出为什么不是事实，模型输出为什么不是硬件 verdict。
- 画出四层决策权限表。

### 1. 用白话区分五个概念

**Planner**读取当前状态，选择下一步意图；例如“缺功率，申请背面标签”。

**Action**是结构化意图数据，不是已经执行的事实。REQUEST_VIEW 表示计划观察，不表示
摄像头已经返回图片。

**Tool/Service**拥有具体能力或副作用，例如 gRPC provider 控制一次观察，规格目录查本地
资料，规则函数计算 verdict。

**Workflow**规定顺序、路由、暂停、恢复和状态更新。它像交通规则，不亲自充当摄像头或
OCR。

**Agent**在本项目中是“基于状态选择受限动作并通过工作流获得新证据的循环”，不是一个
能自由调用任何代码的聊天机器人。

### 2. Evidence 为什么是核心

OCR 说“65W”只是识别候选；它可能来自反光、错误文字框或另一个端口。Evidence 在候选
外增加 target、field、source、confidence、status、derived_from 和 metadata，回答“关于
哪个对象的哪个字段、从哪里来、可靠到什么程度、还能否复核”。

Belief 是当前最合理的认知索引，ledger 是完整历史。不能只保存最终值，否则出现冲突时
你无法解释旧值来自哪张图。

### 3. 为什么模型不能直接决定兼容性

模型擅长在上下文中选路和理解自然语言，但存在随机性、提示注入、知识过期和生成不存在
事实的风险。USB-C verdict 有清晰规则顺序，且涉及安全边界，因此使用确定性函数：相同
Evidence 必须得到相同结果，并列出 used evidence、missing/conflicting fields 和未知边界。

模型可以提出“下一步查型号”，但不能把“看起来像 65W”升级为 confirmed，也不能绕过
规则说“肯定能充”。这不是否定模型，而是把模型放在可测试、可替换的位置。

### 4. 今日权限表

| 层 | 可以决定 | 不能决定 |
|---|---|---|
| C++ 感知 | 帧质量、候选关键帧、Observation 状态 | 标签语义、兼容性 |
| OCR/视觉 Agent | 文本区域、字段候选、EvidenceGap | 笔记本规格、最终 verdict |
| Planner/模型 | 当前允许 Action 中选择一个 | 发明 Evidence、执行任意代码 |
| 规则 | 基于 confirmed Evidence 输出条件 verdict | 声称真实 PD 协商成功 |

### 5. 今日代码阅读路径

1. `workflow/planner.py::available_action_types`：状态如何限制动作。
2. `DeterministicPlanner.plan`：固定优先级是怎样的。
3. `OpenAIPlanner.plan`：模型工具调用怎样再次变成 Action。
4. `compatibility/rules.py::evaluate`：最终结论为何完全不调用模型。
5. `main_agent.py::_render_final_answer`：答案怎样绑定规则结果。

阅读时给每个函数标注“选择/执行/记录/计算”，不能都标成“Agent 逻辑”。

### 6. 实验

运行 Planner 的假模型测试：

```powershell
uv run --locked pytest python/tests/test_ch13_main_agent_loop.py -q
```

然后在个人测试中构造三个假 Responses：

1. 当前只允许 request_view，却返回 generate_answer。
2. 返回正确工具名，但缺少 required 参数。
3. 返回一段“充电器兼容”的普通文字，没有 function call。

预期：三者都不能成为 Action 或 verdict。解释拒绝发生在工具白名单、参数契约还是响应
形状。

### 7. 常见陷阱

- 把每个类都称为 Agent，导致责任边界失去意义。
- 认为“模型只调用工具”就自动安全；工具参数和当前允许集合仍需验证。
- 把 OCR confidence 当作“设备兼容概率”。
- 为了展示 AI，让模型重写确定性规则已经给出的结论。
- 只保存最后一句自然语言答案，不保存 used evidence 和未知边界。

### 8. 答辩

1. Action 为什么不是 Event？一个描述未来意图，一个描述已经发生的过程。
2. deterministic planner 没用模型，为什么仍值得保留 Planner 接口？
3. 如果模型越权选择 generate_answer，哪一层应拒绝？
4. 规则输出 conditions_met 后为什么还不能说“实测充电成功”？

要求用一个完整反例回答：若模型直接输出兼容，会失去哪条证据链、怎样造成错误正结论。

---

## Day 5：异步、线程、超时、取消、恢复与存储

### 今天结束时你必须会

- 区分同步调用、异步事件循环、线程和独立进程。
- 区分 deadline、cancel、retry 和 idempotency。
- 解释 LangGraph interrupt 为什么必须配 checkpoint。
- 分清 checkpoint、Evidence ledger 和图片 artifact。

### 1. 四种执行关系

**同步**：调用者等待函数结束。当前 LangGraph 图和 Python gRPC adapter 主要是同步接口。

**异步**：单线程事件循环在等待 I/O 时切换处理其他任务。FastAPI endpoint 是 async；它用
`asyncio.to_thread` 把同步图移出事件循环，避免一个任务阻塞所有 HTTP/WebSocket。

**线程**：同一进程共享内存，可并行/并发做采集和消费，但需要锁、队列和停止协议。C++
感知使用生产者线程。

**进程**：内存隔离。C++ server 与 Python API 崩溃可以相互隔离，但需要 gRPC/文件通信。

不要把 `async` 理解为“自动开新线程”，也不要把线程安全等同于异步安全。

### 2. deadline、cancel、retry、idempotency

**deadline**回答“最晚等到什么时候”，防止无限阻塞。gRPC deadline 到期不代表底层摄像头
驱动瞬间可被强行终止；它规定调用者等待边界。

**cancel**回答“我现在明确不要了”。Cancel RPC 把 request ID 送到 C++，由 stop token
协作停止。

**retry**是再次尝试，不应默认存在。摄像头暂时失败可能可重试，非法 target ID 不可重试。

**idempotency**是重复请求不会重复产生不可接受副作用。interrupt 恢复前检查 ID，避免旧
网页重复提交消费新的暂停；节点把 I/O 放在 interrupt 之后或依赖层，减少图重放副作用。

### 3. interrupt 不等于普通 return

普通函数 return 后调用栈结束，局部状态消失。LangGraph `interrupt()` 会把可序列化状态
写入 checkpointer，返回一份暂停信息；将来用相同 thread ID 和正确 interrupt ID 提交
`Command(resume=...)`，图从受控位置继续。

三个 ID：

- `thread_id`：哪一个持久任务/checkpoint 流。
- `interrupt_id`：这个任务当前哪一次暂停。
- `request_id`：外部观察请求与 Observation 是否对应。

只校验其中一个不够：正确任务里的旧 interrupt、正确 interrupt 下的另一张 Observation
都可能污染状态。

### 4. 三种存储不能混

| 存储 | 保存什么 | 为什么分开 |
|---|---|---|
| checkpoint | 工作流控制状态、pending action、用量、事件 | 为暂停恢复服务，必须可序列化 |
| Evidence ledger | 事实来源和认知历史 | 为审计、冲突和规则解释服务 |
| artifact | 图片/视频等大二进制 | 大、生命周期不同，不适合塞进状态 JSON |

SQLite checkpointer 让单进程重启后可读取状态，但不自动提供分布式锁、共享事件总线或多实例
调度。“用了数据库”不等于“生产级分布式恢复”。

### 5. 今日代码阅读路径

1. `workflow/models.py` 的两种 Pause/Resume 模型。
2. `main_agent.py::resume_main_agent`：先验证再 Command。
3. `main_agent.py` 的 wait/apply 成对节点。
4. `application/task_service.py::with_sqlite`。
5. `application/api.py` 中 `asyncio.to_thread`。
6. 第 5、6 章 Demo，观察 checkpoint 与 artifact 分离。

### 6. 实验

```powershell
uv run --locked python examples/ch05_active_observation_interrupts.py
uv run --locked python examples/ch06_persistent_storage.py
uv run --locked pytest tests/test_ch05_active_observation_interrupts.py -q
uv run --locked pytest tests/test_ch06_persistent_storage.py -q
```

在正式主图测试中执行四种错误：错误 thread、错误 interrupt、错误 Observation request ID、
取消后恢复。每次确认两件事：错误被拒绝；原本正确的暂停是否仍可恢复。

额外画一条时间线：创建任务→写 checkpoint→返回 interrupt→进程空闲→客户端恢复→验证
三个 ID→执行 OCR→写新 checkpoint。

### 7. 常见陷阱

- 把 gRPC channel、SQLite connection、摄像头或锁放进 checkpoint。
- 先发 `Command(resume)`，进入图后才发现 ID 错，此时原 interrupt 可能已被消费。
- 对所有异常自动 retry，导致摄像头/模型调用风暴。
- 认为 Cancel RPC 成功就保证驱动已立即停止。
- 在 async endpoint 直接运行长同步图，堵住事件循环。
- 把图片 Base64 塞进每个 RunEvent，造成 checkpoint 急剧膨胀。

### 8. 答辩

1. deadline 与 Cancel RPC 为什么不能互相替代？
2. 为什么错误 resume 必须在 `Command(resume)` 前拒绝？
3. SQLite checkpoint 能做什么，不能做什么？
4. 为什么 checkpoint 只保存 provider 的 ID/结果，不保存 provider 对象？
5. `asyncio.to_thread` 解决什么，又没有解决什么？

---

## Day 6：第一周整合答辩——建立整套心智模型

### 上午：从空白纸重建项目

不看资料，用 30 分钟画：

```text
用户问题
→ TaskSession / 两个 RealityObject
→ Planner 选择 Action
→ ObservationRequest
→ C++ Frame 流与关键帧
→ Observation
→ OCR RecognitionDocument
→ Evidence / EvidenceGap
→ BeliefState
→ laptop specification Evidence
→ deterministic rules
→ RunEvent / final answer
```

图中必须标：进程边界、gRPC、artifact、checkpoint、外部副作用、可能暂停的位置。画完再对照
[项目总览](../PROJECT-OVERVIEW.md) 修正，使用另一种颜色记录你漏掉的部分。

### 中午：八个核心类型口试

每个类型用四句话：它是什么、谁创建、谁消费、绝不能混成什么。

1. TaskSession
2. RealityObject
3. ObservationRequest
4. Observation
5. Evidence
6. BeliefState
7. Action
8. RunEvent

例如 Observation 的合格解释必须包含“对一次请求的感知结果”“由 C++/provider 创建”
“进入视觉 Evidence 层”“不等于已确认业务事实”。

### 下午：综合故障定位

给自己准备五张卡片，随机抽取并回答首先检查哪一层：

- `ModuleNotFoundError: paddleocr`
- gRPC deadline exceeded
- Observation request ID mismatch
- OCR 文本存在但 power_not_found
- verdict 是 insufficient_evidence

不能把五种都回答成“看日志”。要说入口文件、关键状态/字段和下一条最小验证命令。

### 第一周 20 题

1. 解释器、编译器和链接器分别做什么？
2. 进程与线程的内存关系有什么不同？
3. `uv.lock` 为什么需要进 Git？
4. 类型标注和运行时校验的边界是什么？
5. Protocol 如何支持 fake/real adapter？
6. strict model 为什么拒绝多余字段？
7. Proto optional 解决什么语义？
8. Proto 字段号为什么不可复用？
9. Action 和 RunEvent 的时间方向有什么不同？
10. Observation 和 Evidence 为什么分开？
11. Belief 与 ledger 为什么都要存在？
12. Planner、Tool 和 Workflow 各负责什么？
13. 模型为什么不能决定 USB-C verdict？
14. deadline 和 cancel 有什么不同？
15. 什么样的错误值得 retry？
16. 幂等性在恢复场景中防什么？
17. 三种 ID 分别标识什么？
18. checkpoint 为什么不能保存连接？
19. SQLite 为什么不等于分布式系统？
20. 当前项目最容易被夸大的三项能力是什么？

### 评分与补考

按 [评分标准](ASSESSMENT-RUBRIC.md) 计分。至少 80 分并且以下三题不得错，才能进入第二周：

- Observation 与 Evidence 的区别。
- 模型与规则的权限边界。
- thread/interrupt/request ID 的区别。

若未通过，不是重看所有文档：定位错题对应的 Day，重新做一次故障实验，第二天先补考。
