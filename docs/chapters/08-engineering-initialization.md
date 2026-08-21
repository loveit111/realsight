# 第 8 章 工程初始化：把七章 Demo 变成可构建、可测试的 C++/Python 单仓项目

> 本章关键词：monorepo、Python 3.12、uv、pyproject.toml、uv.lock、src layout、editable install、C++20、CMake target、CMake Presets、CTest、配置分层、环境自检、Ruff、mypy、pytest、GitHub Actions、兼容迁移

前七章已经回答了一连串业务问题：如何用证据而不是印象判断；如何让 Agent 输出事件；如何取消异步任务；如何固化 Pydantic/Protobuf 契约；如何中断和恢复主动观察；如何跨进程保存检查点、证据和图片；如何用权限、幂等、预算、超时和审计约束执行。

这些能力都经过了测试，但代码仍然主要位于 `examples/`。测试为了找到模块，常常手工修改 `sys.path`；依赖散落在三份 `requirements-ch*.txt`；C++ 尚无构建目标；Python 的目标版本虽然一直写作 3.12，实际却运行在全局 3.13。

这是一种合理的教学阶段，却不是可以持续扩展的工程形态。

第八章不添加新的 Agent，也不连接摄像头。它解决另一个更基础的问题：

> 如何让同一份 RealSight 源码，在你的电脑和 CI 中以明确版本、明确入口、明确依赖方向被构建和测试，并在缺少环境条件时给出可行动的失败信息？

---

## 1. 本章交付前质量鉴定摘要

本章不是写完目录后直接判定通过。代码完成后进行了独立审计，结论如下：

| 鉴定项 | 结论 | 证据 |
|---|---|---|
| 逻辑闭环 | 通过，有明确边界 | 安装、配置、自检、Python 测试、C++ 构建、CTest 形成闭环 |
| 技术栈说明 | 通过 | 每项工具都说明是什么、为什么使用、负责什么、不负责什么 |
| 小 Demo | 通过 | Python 3.12 下构造稳定契约并输出 doctor 报告；C++ 输出 runtime JSON |
| 向前桥接 | 通过 | 保留前七章示例，正式迁入领域契约，并维持旧导入兼容 |
| 向后桥接 | 通过 | 第 9 章可直接向 `RealSight::runtime` 增加帧、队列、回放和摄像头模块 |

 

---

## 2. 本章目标、前提与产物

### 2.1 学习目标

完成本章后，你应当能够：

1. 解释“单仓库”与“单进程”“单可执行文件”不是同一个概念。
2. 画出 Python、C++、契约、配置、测试、文档之间的所有权边界。
3. 说明 `pyproject.toml`、`.python-version`、`.venv` 和 `uv.lock` 各自解决什么问题。
4. 解释版本范围与锁定版本为什么需要同时存在。
5. 使用 `src` 布局和 editable install，避免项目根目录偶然导入。
6. 理解运行依赖与开发依赖为什么分组。
7. 使用 TOML 和白名单环境变量建立可校验配置快照。
8. 解释配置、密钥、业务状态和治理策略为什么不能混成一类数据。
9. 使用 CMake target 表达 C++20 要求、包含目录和链接方向。
10. 使用 CMake Presets 复现 Windows GCC 配置、构建和测试命令。
11. 理解 CTest 测试与 Python pytest 测试的职责边界。
12. 使用 Ruff、mypy、pytest 建立三类不同质量门。
13. 阅读 GitHub Actions 中的最小权限、锁文件和 Action SHA。
14. 运行 `realsight-doctor`，定位 Python、包、配置和 C++ 工具链问题。
15. 在迁移代码时保护旧行为，不把目录重构伪装成业务改写。
16. 说明第九章为什么从 `RuntimeInfo` 进入帧生命周期，而不是从 gRPC 或 Agent 开始。

### 2.2 学习前提

你需要理解：

- 第 4 章 Pydantic 契约和 Protobuf 进程边界。
- 第 5 章 LangGraph 状态与运行时资源分离。
- 第 6 章检查点、Evidence Ledger 和 Artifact Store 的生命周期差异。
- 第 7 章治理中间件包围外部 operation 的执行顺序。
- 基本 Python 模块导入、C++ 头文件和编译概念。

不要求熟练使用包发布、CMake 安装导出、Docker、Kubernetes 或复杂 CI。第八章建立开发工程，不发布公共 PyPI 包，也不部署服务。

### 2.3 本章产物

- 根级 `pyproject.toml`、`.python-version` 和 `uv.lock`。
- `python/src/realsight/` 正式 Python 包。
- `application/config/contracts/governance/services/storage/workflow` 所有权边界。
- 第 4 章稳定领域模型迁入正式包，旧路径保留兼容转发。
- `config/realsight.example.toml` 与严格配置加载器。
- `realsight-doctor` CLI 与 JSON 报告。
- 第 8 章 Python Demo 和 16 项专项测试。
- 根级 CMake 工程、`RealSight::runtime` 库、可执行程序与 CTest。
- Ruff、mypy、pytest 和 GitHub Actions 质量门。
- 第八章正文与独立质量鉴定报告。

---

## 3. 工程初始化不等于“整理文件夹”

如果只把 `examples/ch07_governance_middleware.py` 拆成五个文件，工程仍可能有这些问题：

- 开发者 A 用 Python 3.12，开发者 B 用 3.13，行为不一致。
- 测试从项目根导入源码，但打包后漏文件。
- 新代码从 `realsight.contracts` 导入，旧代码从复制的 `contracts.models` 导入，得到两套类。
- C++ 开发者手写不同编译参数，某台机器使用 C++17 仍能偶然编译。
- 配置字段拼错后被静默忽略。
- README 命令与真实 CLI 不一致。
- CI 使用浮动依赖或浮动 Action，今天通过、下月产生不同结果。

因此，本章用四个可验证条件定义“初始化完成”：

~~~text
可安装：正式 Python 包通过项目元数据安装，不依赖 sys.path 技巧
可构建：C++ target 通过声明式 CMake 命令产生库和程序
可测试：Python/C++ 都有自动测试，旧章节回归继续通过
可诊断：环境缺失时输出具体失败项和修复提示
~~~

目录只是这些性质的外在表现。

---

## 4. 为什么使用 C++/Python 单仓库

### 4.1 单仓库的含义

本课程中的 monorepo 表示：C++ 感知运行时、Python Agent 服务、共享协议、配置、测试和文档位于一个版本控制单元。

它不表示：

- C++ 和 Python 必须在同一个进程。
- C++ 必须编译成 Python 扩展。
- 所有组件必须用一份构建工具。
- 所有数据必须写入一个数据库。
- 所有模块可以相互导入。

第 1 章已经确定，C++ 与 Python 是两个长期运行的进程，第 10 章通过 gRPC/Protobuf 通信。第八章仍坚持该边界。

### 4.2 为什么不做 Python C++ 扩展

Python 扩展适合函数级低延迟调用，但会把 ABI、解释器生命周期、崩溃隔离和部署方式绑定在一起。RealSight 的 C++ 运行时需要持续持有摄像头、管理帧队列，即使 Python Agent 重启也应能独立治理资源。

因此本项目采用：

~~~text
同一仓库
├── 独立 Python 包和进程
├── 独立 C++ CMake 工程和进程
└── 共享 Protobuf 协议
~~~

单仓便于协议原子变更和端到端测试；独立进程保留故障与生命周期边界。

---

## 5. 正式目录结构与所有权

~~~text
realsight/
├── .github/workflows/ci.yml
├── config/realsight.example.toml
├── contracts/
│   ├── realsight.proto
│   └── models.py                 # 旧章节兼容入口
├── cpp/
│   ├── apps/perception_main.cpp
│   ├── include/realsight/runtime/runtime_info.hpp
│   ├── src/runtime_info.cpp
│   └── tests/runtime_info_test.cpp
├── docs/chapters/                # 正式课程正文
├── docs/reviews/                 # 独立质量鉴定
├── examples/                     # 第 1～8 章教学示例
├── python/
│   ├── src/realsight/
│   │   ├── application/
│   │   ├── config/
│   │   ├── contracts/
│   │   ├── governance/
│   │   ├── services/
│   │   ├── storage/
│   │   └── workflow/
│   └── tests/
├── tests/                        # 第 1～7 章历史回归测试
├── CMakeLists.txt
├── CMakePresets.json
├── pyproject.toml
└── uv.lock
~~~

### 5.1 Python 模块职责

| 包 | 拥有什么 | 不应拥有什么 | 后续章节 |
|---|---|---|---|
| `contracts` | Pydantic 领域类型 | SQL、网络调用、模型客户端 | 全课程复用 |
| `config` | TOML/环境变量到设置快照 | 密钥明文、动态业务状态 | 第 14 章启动 |
| `application` | 启动、恢复、取消用例 | 摄像头循环、SQL 细节 | 第 13～14 章 |
| `workflow` | LangGraph 图、节点、路由 | C++ 句柄、数据库连接 | 第 13 章 |
| `governance` | 权限、预算、重试、审计 | 业务规划、视觉识别 | 第 13 章迁入 |
| `services` | gRPC、视觉、检索适配器 | Agent 状态与规则真相 | 第 10～12 章 |
| `storage` | checkpoint、ledger、artifact 适配 | Agent 决策 | 第 13 章迁入 |

### 5.2 为什么第八章不立刻拆完第 6～7 章大文件

“建立目标目录”与“安全迁移所有实现”是不同任务。第 6～7 章文件合计较长，内部还有测试夹具和教学阶段函数。如果本章一次拆完，很难区分失败来自包结构还是行为重构。

本章只迁移最稳定的领域契约，其他包先固定所有权。后续每个真实功能进入正式包时，带着自己的测试迁入。这种增量迁移降低回归定位成本。

---

## 6. Python 3.12：目标版本、实测版本与企业选择

### 6.1 为什么继续使用 3.12

截至 2026 年 8 月，Python 3.12 已进入 security 状态，官方计划支持至 2028 年 10 月；3.13 和 3.14 更新，但课程依赖已经围绕 3.12 设计并验证。[Python 版本状态](https://devguide.python.org/versions/)

选择企业版本不能只追求最新数字，还要看：

- 依赖是否支持。
- 团队与部署基础设施是否统一。
- 是否仍有官方安全支持。
- 升级是否有独立测试周期。

本项目声明：

~~~toml
requires-python = ">=3.12,<3.13"
~~~

这表示课程当前只支持 3.12，不是假装 3.13 一定不兼容，而是没有把未经完整验收的版本加入支持矩阵。

### 6.2 三层版本约束

| 文件/机制 | 作用 | 是否足够 |
|---|---|---|
| `.python-version` | 告诉 uv 默认选择 3.12 | 不能阻止其他工具绕过 |
| `requires-python` | 包元数据声明兼容范围 | 不选择具体补丁版本 |
| CI/doctor 实测 | 证明当前运行解释器确实是 3.12 | 依赖前两者提供意图 |

本机实际验证为 Python 3.12.13。此前全局命令是 3.13.13，说明仅靠“大家记得用 3.12”并不可靠。

---

## 7. uv、pyproject.toml 与 uv.lock

### 7.1 三者分别是什么

`pyproject.toml` 是 Python 项目的标准元数据和工具配置入口。它表达项目名称、Python 范围、直接依赖、命令入口及 Ruff/mypy/pytest 配置。

`uv` 是项目与环境管理工具，负责选择 Python、解析依赖、创建 `.venv`、安装项目和运行命令。

`uv.lock` 是 uv 管理的跨平台精确解析结果。官方说明，`pyproject.toml` 表达宽泛要求，锁文件记录精确版本，且应提交版本控制；`uv run`/`uv sync` 会检查两者是否一致。[uv 项目结构](https://docs.astral.sh/uv/concepts/projects/layout/)

### 7.2 为什么既有 `==` 又有 lock

课程依赖使用经过本章验证的直接版本：

~~~toml
dependencies = [
  "grpcio-tools==1.81.1",
  "langgraph==1.2.10",
  "langgraph-checkpoint-sqlite==3.1.0",
  "protobuf==6.33.6",
  "pydantic==2.13.4",
]
~~~

`==` 固定直接依赖，`uv.lock` 还固定其传递依赖与平台条件。例如只固定 LangGraph 仍不能说明 LangChain Core、序列化器等解析到哪一版。

企业库常使用兼容范围并测试最低/最高版本；内部应用更常锁定完整环境。本项目是一个需要可复现演示的应用型课程，因此偏向严格锁定。

### 7.3 `uv sync --locked` 的含义

官方文档区分 locking 与 syncing：lock 解析依赖，sync 将环境安装到锁文件状态；`--locked` 在元数据与锁文件不一致时失败，而不是偷偷更新。[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)

日常流程是：

~~~powershell
# 开发者和 CI：只接受已审核锁文件
uv sync --locked --all-groups

# 有意识升级依赖时：修改元数据并重新解析，然后审查 uv.lock
uv lock
~~~

不要手工编辑 `uv.lock`。

### 7.4 运行依赖与开发依赖

运行依赖是程序启动所需：Pydantic、LangGraph、Protobuf 等。开发组包含：

- `pytest`：执行行为测试。
- `ruff`：格式化和静态规则。
- `mypy`：类型检查。
- `cmake`：让 Windows CMake 版本进入同一锁定环境。

CMake 是开发工具，不应成为生产 Python 进程的运行依赖。部署时可以使用 `--no-dev` 排除开发组。

---

## 8. 为什么使用 `src` 布局

### 8.1 平铺布局的偶然成功

假设项目根有：

~~~text
realsight/
├── realsight/
└── tests/
~~~

从根目录运行 Python 时，当前目录自动进入模块搜索路径。即使项目从未安装，`import realsight` 也可能成功；打包后漏文件的问题要到另一台机器才暴露。

### 8.2 `src` 布局的保护

正式代码位于：

~~~text
python/src/realsight/
~~~

Python 不会因为当前目录是仓库根就直接找到它。必须由 uv 将项目以 editable 方式安装。uv 官方也指出，`src` 布局能把库代码与项目根隔离，避免普通 Python 调用从源码树偶然导入。[uv 创建项目](https://docs.astral.sh/uv/concepts/projects/init/)

本项目使用 uv 的原生纯 Python build backend：

~~~toml
[build-system]
requires = ["uv_build>=0.11.30,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-root = "python/src"
~~~

官方建议为 `uv_build` 设上界，因为它仍遵循 uv 的版本策略；该 backend 适合纯 Python 包，而 C++ 服务由独立 CMake 构建，不塞进 Python wheel。[uv build backend](https://docs.astral.sh/uv/configuration/build-backend/)

### 8.3 editable install 不是“直接导入当前目录”

editable install 会在环境中注册项目，使源码修改立即可见。它仍经过构建系统和项目元数据，而不是测试手工修改 `sys.path`。

本章专项测试断言：

- `importlib.metadata.version("realsight")` 可读取发行版本。
- 包路径包含 `python/src`。
- 测试文件不插入项目根到 `sys.path`。

---

## 9. 稳定契约迁移与兼容窗口

### 9.1 最危险的错误：复制两套类

如果同时保留两份完整定义：

~~~python
from contracts.models import TaskSession as OldTaskSession
from realsight.contracts import TaskSession as NewTaskSession

OldTaskSession is NewTaskSession  # False
~~~

即使字段完全相同，它们也是不同 Python 类。序列化器、`isinstance`、依赖注入和 checkpoint 恢复都可能产生难以理解的问题。

### 9.2 本章方案：唯一实现 + 兼容转发

唯一实现位于：

~~~text
python/src/realsight/contracts/models.py
~~~

旧 `contracts/models.py` 只做：

~~~python
from realsight.contracts.models import *
from realsight.contracts.models import __all__ as __all__
~~~

因此旧代码与新代码获得同一个类对象。专项测试另起 Python 进程，以仓库根为工作目录，验证 `old is new`。

### 9.3 兼容层的生命周期

兼容层不是永久架构。后续迁移步骤：

1. 新代码只从 `realsight.contracts` 导入。
2. 老示例继续通过兼容入口运行。
3. 当历史示例全部更新或冻结后，删除根 `contracts/models.py`。
4. `contracts/realsight.proto` 则是语言中立协议，仍可保留在仓库根。

---

## 10. 配置：TOML、环境变量与不可变快照

### 10.1 为什么不把配置写在 Demo 里

第 7 章策略由测试函数直接构造，便于验证，但真实进程需要在不改代码的情况下调整数据目录、日志等级和预算。

本章示例配置：

~~~toml
[runtime]
data_dir = "../runtime-data"
log_level = "INFO"

[governance]
policy_id = "local-teaching-v1"
allowed_capabilities = ["perception.observe", "rules.usb_c"]
max_commands = 8
max_observations = 4
max_external_attempts = 12
max_cost_units = 20
~~~

### 10.2 加载顺序

~~~text
TOML 文件
-> tomllib 结构化解析
-> 白名单 REALSIGHT_* 环境变量覆盖
-> Pydantic 字段与聚合校验
-> 相对 data_dir 锚定到配置文件目录
-> 不可变 AppSettings 快照
~~~

为什么相对路径以配置文件目录为准？如果用当前工作目录，同一命令从 IDE、项目根或服务管理器启动可能写入三个不同位置。

### 10.3 为什么环境变量不能任意映射

本章只允许明确列表：

~~~text
REALSIGHT_DATA_DIR
REALSIGHT_LOG_LEVEL
REALSIGHT_MAX_COMMANDS
REALSIGHT_MAX_OBSERVATIONS
REALSIGHT_MAX_EXTERNAL_ATTEMPTS
REALSIGHT_MAX_COST_UNITS
~~~

显式映射能控制类型转换和审计面。环境中其他变量不会自动进入设置，更不会被打印到 doctor 报告。

### 10.4 配置不是密钥系统

`realsight.example.toml` 可以提交版本控制，因为它不含凭据。真实 API Key、令牌和证书不应写入该文件。第十四章部署时应从平台 secret store 注入敏感值，并对日志做脱敏。

配置也不是业务状态：`laptop_model`、Evidence、interrupt 不应写入应用设置，它们属于任务会话和存储层。

---

## 11. 工程自检器：让失败可以行动

### 11.1 为什么需要 doctor

初学者看到：

~~~text
ModuleNotFoundError
command not found
requires-python mismatch
~~~

往往不知道应该重装包、切 Python、配置 PATH，还是修 TOML。`realsight-doctor` 把检查分成稳定结果：

~~~text
[PASS] python: Python 3.12.13
[PASS] python-package: realsight 0.1.0
[PASS] configuration: policy=local-teaching-v1, data_dir=...
[PASS] uv: uv 0.11.30 ...
[PASS] cmake: cmake version 4.4.2 ...
[PASS] cxx-compiler: g++.EXE ... 16.1.0
READY=true
~~~

### 11.2 doctor 检查什么

| 检查 | 证明什么 | 不能证明什么 |
|---|---|---|
| Python | 当前解释器是 3.12 | 所有依赖业务都正确 |
| package | 发行包已安装并有版本 | wheel 内容全部正确 |
| config | TOML 通过正式加载器 | 策略在运行时已被强制 |
| uv | 环境工具可执行 | 锁文件一定经过安全审核 |
| CMake | 构建前端可执行 | OpenCV 已安装 |
| compiler | GCC/Clang 可执行 | 摄像头驱动可用 |

doctor 的 `ready=true` 只表示第八章初始化条件满足，不等于 RealSight 功能完成。

### 11.3 为什么 doctor 不自动安装

自动安装会修改系统状态，可能破坏用户已有环境，还会掩盖工具来源。doctor 的职责是诊断和提示；依赖安装由 `uv sync`、MSYS2 包管理或管理员流程完成。

### 11.4 文本与 JSON 两种输出

人类使用文本：

~~~powershell
uv run --locked realsight-doctor
~~~

CI 或未来 API 可使用 JSON：

~~~powershell
uv run --locked realsight-doctor --json
~~~

结构化输出避免调用者重新解析彩色日志文本。

---

## 12. CMake：从源文件列表到目标关系

### 12.1 为什么不直接调用 g++

下面的命令能编译一个文件：

~~~powershell
g++ -std=c++20 main.cpp -o app.exe
~~~

但当第九章出现库、测试、OpenCV 头文件和平台差异后，手写命令会迅速失控。CMake 不是编译器；它读取目标声明，生成给 MinGW Makefiles、Ninja、Visual Studio 等后端使用的构建系统。

### 12.2 根项目与子项目

根 `CMakeLists.txt` 只做三件事：

~~~cmake
cmake_minimum_required(VERSION 3.25)
project(RealSight VERSION 0.1.0 LANGUAGES CXX)
include(CTest)
add_subdirectory(cpp)
~~~

实际目标定义在 `cpp/CMakeLists.txt`。

### 12.3 库目标、别名目标与可执行目标

~~~cmake
add_library(realsight_runtime STATIC src/runtime_info.cpp)
add_library(RealSight::runtime ALIAS realsight_runtime)

add_executable(realsight-perception apps/perception_main.cpp)
target_link_libraries(realsight-perception PRIVATE RealSight::runtime)
~~~

`RealSight::runtime` 命名空间别名让依赖看起来像一个明确组件。未来目标不应把 `runtime_info.cpp` 再次列入自己的源文件，而应链接库。

### 12.4 为什么使用 target-scoped C++20

~~~cmake
target_compile_features(realsight_runtime PUBLIC cxx_std_20)
~~~

CMake 官方教程建议通过 `target_compile_features` 表达目标所需最低特性，而不是全局覆盖 `CMAKE_CXX_STANDARD`。`PUBLIC` 表示 runtime 的消费者也必须满足 C++20。[CMake Target Commands](https://cmake.org/cmake/help/latest/guide/tutorial/In-Depth%20CMake%20Target%20Commands.html)

目标式写法会随依赖图传播要求：

~~~text
realsight_runtime --PUBLIC cxx_std_20--> realsight-perception
                 --PUBLIC include dir--> realsight-runtime-test
~~~

### 12.5 包含目录的 BUILD/INSTALL 区分

~~~cmake
target_include_directories(
  realsight_runtime
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)
~~~

构建源码树时使用绝对源码目录；未来安装库时，消费者看到安装前缀下的 `include`。本章尚未编写 install/export 规则，但接口没有把本机绝对路径写进未来安装元数据。

---

## 13. CMake Presets 与本地差异

### 13.1 Preset 解决什么

`CMakePresets.json` 保存团队共享的 configure/build/test 方式。CMake 官方区分：`CMakePresets.json` 可提交版本控制，`CMakeUserPresets.json` 用于开发者本机设置，不应提交。[CMake Presets](https://cmake.org/cmake/help/latest/manual/cmake-presets.7.html)

本章提供 Windows MSYS2 GCC preset：

~~~json
{
  "name": "windows-gcc-debug",
  "generator": "MinGW Makefiles",
  "binaryDir": "${sourceDir}/build/windows-gcc-debug",
  "cacheVariables": {
    "CMAKE_BUILD_TYPE": "Debug",
    "CMAKE_CXX_COMPILER": "g++"
  }
}
~~~

### 13.2 为什么 CMake 被锁进 dev 依赖，GCC 没有

CMake 的 Python wheel 能跨平台提供构建前端，因此由 uv 锁定为 4.4.2。GCC 属于原生系统工具链，由 MSYS2 安装；把完整 GCC 塞进 Python 项目并不合理。

本章本机调用：

~~~powershell
uv run --locked cmake --preset windows-gcc-debug
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
~~~

实测编译器为 MSYS2 UCRT64 GCC 16.1.0。

### 13.3 CI 为什么不使用 Windows preset

C++ CI 在 Ubuntu 使用标准命令：

~~~text
cmake -S . -B build/ci -DCMAKE_BUILD_TYPE=Debug
cmake --build build/ci --parallel 2
ctest --test-dir build/ci --output-on-failure
~~~

这提供第二个平台验证。Windows preset 验证首要开发环境；Linux CI 验证源代码和 CMake 目标没有无意绑定 Windows。

---

## 14. C++ 最小 Demo：为什么只有 RuntimeInfo

第八章的 C++ 程序输出：

~~~json
{"service_name":"realsight-perception-runtime","version":"0.1.0","cxx_standard":20,"planned_capabilities":["video_replay","camera_capture","quality_scoring"]}
~~~

它证明：

- 头文件可以从公开 include 目录消费。
- 静态库可以编译。
- 可执行目标能链接该库。
- 程序由 C++20 编译。
- CTest 能链接相同库并运行断言。

它不证明：

- 摄像头已经打开。
- 视频帧已采集。
- OpenCV 已接入。
- gRPC 已生成 C++ stub。
- 质量评分已经实现。

为什么不现在加入摄像头？因为如果同时调试 CMake、OpenCV、驱动、队列和业务逻辑，失败来源太多。第九章将在已验证构建链上逐层加入帧运行时。

---

## 15. Python 第八章 Demo 逐步讲解

Demo 文件：`examples/ch08_engineering_bootstrap.py`。

### 15.1 启动方式本身就是测试

~~~powershell
uv run --locked python examples/ch08_engineering_bootstrap.py
~~~

文件没有修改 `sys.path`。如果项目未安装、锁文件过期或 Python 版本不符，命令会在明确边界失败。

### 15.2 第一步：加载配置快照

~~~python
settings = load_settings(config_path, environ={})
~~~

Demo 显式传入空环境映射，使输出不受你的终端环境变量影响。生产启动可省略该参数，让真实 `os.environ` 覆盖。

### 15.3 第二步：构造稳定会话

~~~python
session = TaskSession(
    session_id="session-ch08-001",
    thread_id="session-ch08-001",
    intent="判断 USB-C 充电兼容性",
    target_id="charger-ch08-001",
)
~~~

这不是创建新模型，而是使用第 4 章迁入正式包的同一契约。`thread_id == session_id` 的课程 MVP 约束仍存在。

### 15.4 第三步：创建下一章的观察请求

~~~python
request = ObservationRequest(
    request_id="request-ch08-001",
    session_id=session.session_id,
    target_id=session.target_id,
    view_type=ViewType.BACK_LABEL,
    required_features=("charger_power", "charger_protocol"),
    instruction="请拍摄充电器背面标签",
    reason="需要额定功率与协议证据",
    timeout_ms=5_000,
)
~~~

第八章不会执行请求，但第九章的 C++ 回放/摄像头运行时已有明确输入语义；第十章才通过 Protobuf/gRPC 真正发送。

### 15.5 第四步：运行 doctor

~~~python
report = run_doctor(config_path, strict_python=True)
~~~

最终 JSON 同时包含 session、request、policy 和 doctor。它展示“业务契约”和“工程环境”是两类数据，虽然可以在一次 Demo 输出中观察，却不应存进同一个领域对象。

---

## 16. 质量门：格式、规则、类型、行为不是一回事

### 16.1 Ruff format

~~~powershell
uv run --locked ruff format --check python/src python/tests examples/ch08_engineering_bootstrap.py
~~~

它验证代码排版一致，不证明逻辑正确。

### 16.2 Ruff check

~~~powershell
uv run --locked ruff check python/src python/tests examples/ch08_engineering_bootstrap.py
~~~

它检查未使用导入、现代语法、部分易错模式和导入顺序。Ruff 有自己的版本策略，规则范围可能随 minor 版本变化，因此本项目锁定工具版本并在升级时审查变化。[Ruff Versioning](https://docs.astral.sh/ruff/versioning/)

### 16.3 mypy

~~~powershell
uv run --locked mypy
~~~

mypy 做静态类型一致性检查。本章对新正式包使用 strict 模式，但暂时排除迁入的 `contracts/models.py`，因为该文件含大量 Pydantic `Annotated` 约束，应该单独制定插件和迁移策略，不能为了“全绿”删除运行时校验。

这是一项明确技术债，不是宣称全仓严格类型完成。

### 16.4 pytest

~~~powershell
uv run --locked pytest
~~~

pytest 运行第八章新测试，也能发现根 `tests/` 中使用 `unittest.TestCase` 的前七章测试。因此不需要为了换测试运行器重写 120 个既有测试。

### 16.5 CTest

CTest 运行 C++ 可执行测试，不负责 Python。当前 1 个测试验证 runtime 元数据；第九章会增加帧队列和回放测试。

---

## 17. 第八章专项测试在保护什么

16 项测试按风险分组：

### 17.1 环境与安装

- 当前解释器真实为 Python 3.12。
- `realsight` 发行元数据存在。
- 包文件来自 `python/src`。

### 17.2 配置

- 相对数据目录以配置文件为基准。
- 日志等级规范化。
- 白名单环境变量按类型覆盖。
- 无关环境变量不进入设置。
- 非法整数给出变量名。
- 未知 TOML 字段被拒绝。
- capability 格式被校验。

### 17.3 契约迁移

- 正式包能构造下一章 ObservationRequest。
- 旧入口与新入口共享类身份。

### 17.4 doctor 与 CLI

- Python 3.12 严格检查通过。
- 缺工具返回失败和修复提示。
- 工具版本可被结构化读取。
- 全 PASS 时 `ready=true`。
- CLI 失败返回退出码 1。
- JSON 输出可被解析。

### 17.5 CMake 边界

- C++20 通过 target 属性声明。
- 没有全局 `CMAKE_CXX_STANDARD`。
- 本章没有真实 `find_package(OpenCV...)` 或 `find_package(gRPC...)`。

测试不要只验证“文件存在”。工程文件存在但语义错误，比没有文件更难发现。

---

## 18. CI：在另一台机器重新证明

### 18.1 Python job

Windows job 执行：

~~~text
安装固定 uv
-> uv sync --locked --all-groups
-> Ruff format check
-> Ruff lint
-> mypy
-> pytest
~~~

它让 Python 3.12 和 Windows 首要环境成为持续验证对象。

### 18.2 C++ job

Ubuntu job执行：

~~~text
CMake configure
-> C++20 build
-> CTest
~~~

这不是声称 Linux 摄像头已经支持，只是验证基础 C++ 源码跨平台。

### 18.3 最小权限与 Action SHA

工作流声明：

~~~yaml
permissions:
  contents: read
~~~

没有发布需求，就不给写权限。GitHub 安全文档指出，完整 commit SHA 是使用不可变 Action 版本的唯一方式，因此 checkout 与 setup-uv 均钉到完整 SHA，并在注释保留人类可读版本。[GitHub Actions 安全使用](https://docs.github.com/en/actions/reference/security/secure-use)

### 18.4 当前 CI 边界

本地工作区不在 Git 仓库中，因此本章只能静态检查 workflow，不能声称 GitHub-hosted runner 已实际运行。真正推送到 GitHub 后，CI 状态才是远程证据。

---

## 19. 实际运行与关键输出

### 19.1 同步锁定环境

~~~powershell
$env:UV_CACHE_DIR = Join-Path $PWD ".uv-cache"
uv sync --locked --all-groups
~~~

关键结果：

~~~text
Using CPython 3.12.13
Built realsight 0.1.0
Installed locked runtime and development dependencies
~~~

### 19.2 运行 doctor

~~~powershell
uv run --locked realsight-doctor
~~~

关键结果：6 项 PASS，`READY=true`。

### 19.3 运行 Python Demo

~~~powershell
uv run --locked python examples/ch08_engineering_bootstrap.py
~~~

关键字段：

~~~text
session.status = running
observation_request.view_type = back_label
policy.allowed_capabilities = [perception.observe, rules.usb_c]
doctor.ready = true
~~~

### 19.4 构建 C++

~~~powershell
uv run --locked cmake --preset windows-gcc-debug
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug
~~~

关键结果：

~~~text
GNU C++ 16.1.0
Built realsight_runtime
Built realsight-perception
Built realsight-runtime-test
1/1 CTest passed
~~~

### 19.5 Python 全量回归

~~~powershell
uv run --locked pytest -q
~~~

结果：

~~~text
136 passed
~~~

其中前七章 120 项，第八章新增 16 项。

### 19.6 静态质量门

~~~text
Ruff format: passed
Ruff lint: passed
mypy strict scope: passed
~~~

---

## 20. 常见错误与诊断方法

### 错误一：直接运行全局 `python`

全局 Python 可能是 3.13，且没有安装项目。使用 `uv run --locked ...`。

### 错误二：手动激活环境后继续 `pip install`

这会让环境偏离锁文件。项目依赖使用 `uv add`/`uv lock`，环境使用 `uv sync --locked`。

### 错误三：把 `.venv` 提交版本控制

虚拟环境包含平台路径和二进制，不可移植。提交 `pyproject.toml` 与 `uv.lock`。

### 错误四：在新测试继续修改 `sys.path`

这会绕过 `src` 布局保护。新测试应从安装包导入。

### 错误五：立即删除旧 contracts

会破坏前七章测试。先用转发兼容，再增量迁移调用者。

### 错误六：把 `uv.lock` 当依赖意图

直接依赖仍应写在 `pyproject.toml`；lock 是解析结果。

### 错误七：全局设置 C++ 标准

目标要求应使用 `target_compile_features`，让依赖方向清楚传播。

### 错误八：doctor PASS 就认为摄像头可用

doctor 只检查工具链。摄像头、OpenCV、帧队列从第九章验证。

### 错误九：把 API Key 放进 example TOML

示例配置会提交仓库。敏感值由部署 secret store 提供。

### 错误十：CI 使用 `latest`

工具与 Action 都会变化。锁定版本或 commit SHA，并通过明确升级 PR 更新。

---

## 21. 本章能力边界

本章已经完成：

- Python 3.12.13 实机环境。
- 可安装的纯 Python `src` 包。
- 精确 `uv.lock`。
- 领域模型唯一实现与兼容迁移。
- TOML/环境变量配置快照。
- 可操作的工程 doctor。
- C++20 库、程序和测试目标。
- Windows GCC 本机构建。
- Python/C++ 质量门与 CI 定义。

本章没有完成：

- 第 6～7 章所有实现的正式分包迁移。
- OpenCV、摄像头、视频回放与帧队列。
- C++ Protobuf/gRPC 代码生成。
- Visual Studio/MSVC preset。
- Linux 摄像头实机测试。
- wheel/sdist 安装后的独立 smoke test。
- Docker、部署、SBOM、漏洞扫描与制品签名。
- 远程 GitHub Actions 实际运行。

这些限制不是遗漏，而是章节边界。特别要注意：本章的 Python package 不包含 C++ 可执行文件；它们是同仓、独立构建的两个组件。

---

## 22. 建议阅读顺序

代码基础较弱时，按以下顺序阅读：

1. `examples/ch08_engineering_bootstrap.py` 顶部说明与 `build_demo_summary`。
2. `config/realsight.example.toml`。
3. `python/src/realsight/config/settings.py` 的模型，再读 `load_settings`。
4. `python/src/realsight/doctor.py` 的 `CheckResult`，再读 `run_doctor`。
5. `python/src/realsight/cli.py`，观察 CLI 如何只做输入输出。
6. `pyproject.toml`，逐项映射到上述模块。
7. `python/tests/test_ch08_bootstrap.py`，从测试反推设计约束。
8. 根 `CMakeLists.txt`，再读 `cpp/CMakeLists.txt`。
9. C++ 头文件、实现、app、test 四个文件。
10. 最后阅读 `.github/workflows/ci.yml`。

不要一开始逐行阅读 `uv.lock`。它是工具管理的解析结果，应在依赖升级时查看 diff，而不是当作入门教材。

---

## 23. 课后练习

### 练习 1：解释四个版本文件

不用运行代码，逐一回答 `.python-version`、`requires-python`、直接依赖 `==`、`uv.lock` 各约束什么，并举例说明只保留其中一个会发生什么。

### 练习 2：增加配置字段

给 `RuntimeSettings` 增加 `artifact_verify_on_read: bool`：

- TOML 默认设为 true。
- 增加 `REALSIGHT_ARTIFACT_VERIFY_ON_READ`。
- 不允许用 `bool("false")` 转换字符串，因为它会得到 true。
- 增加 true/false/非法值测试。

### 练习 3：观察 `src` 布局

在不使用 `uv run` 的干净目录运行：

~~~powershell
python -c "import realsight"
~~~

再使用 `uv run --locked`。解释两次模块搜索路径和发行元数据差异。不要通过添加 `sys.path` 修复。

### 练习 4：设计迁移清单

选择第 7 章的 `GovernancePolicy`，设计迁入 `realsight.governance` 的步骤：哪些测试先复制，哪些导入路径后改，如何证明 checkpoint/SQLite 行为没变。只写迁移计划，不一次搬完整大文件。

### 练习 5：增加 release preset

在 `CMakePresets.json` 增加 `windows-gcc-release`，继承或复用已有字段，把 `CMAKE_BUILD_TYPE` 改为 Release，输出到独立构建目录。不要让 Debug/Release 共用目录。

### 练习 6：给 doctor 增加版本下限

当前 doctor 只证明工具可执行。设计结构化版本解析，验证 CMake >= 3.25；不要用简单字符串字典序比较 `"4.4"` 与 `"10.0"`。

### 练习 7：包制品 smoke test

研究 `uv build`，设计在隔离环境安装 wheel 后运行 `realsight-doctor --json` 的测试。解释为什么 editable install 通过仍不能完全证明 wheel 内容正确。

### 练习 8：画依赖方向

画出 `application/workflow/governance/services/storage/contracts` 的允许依赖箭头，并判断以下导入是否合理：

~~~text
contracts -> storage
workflow -> contracts
services -> workflow
application -> services
storage -> contracts
~~~

### 练习 9：CI 失败归因

分别说明 Ruff、mypy、pytest、CMake configure、C++ build、CTest 失败时第一步应该查看什么。不要把所有失败都归因于“环境问题”。

### 练习 10：自己写一个新包文件

在 `realsight.services` 中创建一个只包含 `Protocol` 的 `perception_port.py`，顶部按本课程要求写整体逻辑、技术栈、调用流程和边界。不要实现 gRPC；第十章再提供真实 adapter。

---

## 24. 本章小结

第八章没有新增智能能力，却完成了从“七组可运行示例”到“一个有明确构建契约的工程”的转变：

1. Python 目标从文档中的 3.12 变成实机 3.12.13。
2. 依赖从章节 requirements 迁入 `pyproject.toml + uv.lock`。
3. 正式代码进入 `python/src/realsight`，不再依赖新代码修改 `sys.path`。
4. 稳定领域契约只有一个实现，旧路径通过兼容层转发。
5. TOML 与白名单环境变量生成不可变设置快照。
6. doctor 把环境问题变成结构化、可行动的检查结果。
7. C++ 通过 `RealSight::runtime`、C++20 target 和 preset 进入可构建状态。
8. Ruff、mypy、pytest、CTest 与 CI 分别守住不同质量边界。
9. 本地实际完成 136 项 Python 测试与 1 项 C++ 测试。

工程化不是用更多工具替代思考。每个工具都对应一个明确问题：uv 管 Python 环境和解析，CMake 管 C++ 目标关系，Pydantic 管边界数据，Ruff 管格式与静态规则，mypy 管类型一致性，pytest/CTest 管行为，CI 在另一台机器重做证明。

---

## 25. 到第 9 章的桥接

第八章留下了一个真实但很小的 C++ 运行时：

~~~text
RealSight::runtime
-> realsight-perception
-> realsight-runtime-test
~~~

第九章《C++ 实时感知运行时》将在这个目标上增加：

- `Frame` 与单调时钟时间戳。
- 摄像头和预录视频统一的 `FrameSource` 接口。
- 有界生产者/消费者队列。
- backpressure 与丢帧策略。
- RAII 帧生命周期。
- 优雅停止和错误传播。
- OpenCV 视频回放与摄像头适配器。
- 不依赖真实摄像头的确定性测试视频。

第九章仍不会急着接 gRPC。先证明 C++ 能持续、稳定、可取消地处理帧；第十章再把筛选后的 Observation 通过协议发给 Python。

第八章回答“代码怎样成为团队能一致构建的工程”，第九章开始回答“这个工程里的 C++ 运行时怎样正确接触连续现实世界”。

