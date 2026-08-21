# 第 8 章质量鉴定报告

## 1. 鉴定结论

**结论：逻辑闭环、技术栈说明、小 Demo、前后章节桥接四项通过，可以交付第八章；通过范围限定为“C++/Python 单仓开发骨架与本地/CI 质量门”，不能宣称已完成摄像头运行时、远程 CI、部署或全部历史代码分包。**

本报告先于正式交付检查了：

1. `pyproject.toml` 是否真能选择 Python 3.12、生成 lock 并安装 src 包。
2. 正式模型迁移是否复制出两套类，旧章节是否继续运行。
3. 配置是否结构化解析、拒绝未知字段并稳定解析相对路径。
4. doctor 是否报告真实工具，而不是在缺失时伪造成功。
5. CMake 是否以 target 表达 C++20、包含目录和链接关系。
6. C++ 程序与测试是否真实编译、链接和执行。
7. Ruff、mypy、pytest 是否运行在锁定 Python 3.12 环境。
8. CI 是否最小权限、锁文件模式，并降低第三方 Action 浮动风险。
9. 正文命令、数字和实际输出是否一致。

---

## 2. 逻辑闭环鉴定

**结果：通过。**

### 2.1 Python 初始化链

~~~text
.python-version + requires-python
-> uv 选择 CPython 3.12.13
-> pyproject.toml 表达直接依赖和 build backend
-> uv.lock 固化完整解析
-> uv sync --locked 安装 .venv 和 editable realsight 包
-> src 包导入、配置加载、doctor
-> Ruff/mypy/pytest
~~~

每一步都有失败出口：版本不符由 uv/doctor 失败，锁文件过期由 `--locked` 失败，配置错误由 Pydantic 失败，行为错误由测试失败。

### 2.2 C++ 初始化链

~~~text
CMakeLists.txt + CMakePresets.json
-> CMake 4.4.2 configure
-> MSYS2 UCRT64 GCC 16.1.0
-> realsight_runtime static library
-> realsight-perception + realsight-runtime-test
-> CTest
~~~

实际构建和 1/1 CTest 通过。应用目标与测试目标都链接 `RealSight::runtime`，没有重复编译实现源文件。

### 2.3 配置链

~~~text
TOML
-> tomllib
-> 显式 REALSIGHT_* 覆盖
-> Pydantic strict fields
-> 相对路径锚定到 config 文件
-> frozen AppSettings
~~~

未知 section、非法 capability 和错误整数都有测试。无关环境变量不会进入模型或输出。

### 2.4 迁移链

~~~text
正式唯一实现：realsight.contracts.models
-> 新代码：realsight.contracts
-> 旧代码：contracts.models 兼容转发
-> 跨进程断言 old is new
-> 前七章回归
~~~

这避免了最危险的“字段相同但类身份不同”。

---

## 3. 审计中发现并修正的问题

### F1：兼容入口在严格 src 环境不可导入

初版只保留 `contracts/models.py`。第八章测试不把仓库根加入 `sys.path`，因此直接导入失败。

判断：正式 src 测试无法导入旧根模块是预期隔离，但旧示例的兼容性仍需真实验证。

修正：

- 增加 `contracts/__init__.py`。
- 保留唯一实现转发。
- 专项测试另起 Python 进程，以仓库根为 cwd 模拟旧章节，断言类身份相同。

### F2：章节边界测试误把注释当依赖

初版断言 `"OpenCV" not in cpp_cmake`，而注释说明第 9 章才加入 OpenCV，因此测试失败。

判断：代码边界正确，测试过宽。

修正：改为检查实际 `find_package(OpenCV` 与 `find_package(gRPC`，保留可读注释。

### F3：README 与 CLI 接口不一致

README 写 `realsight-doctor --strict-python`，CLI 只有 `--no-strict-python`，默认已经严格。

判断：真实用户会得到参数错误，属于交付缺陷。

修正：README 改用无额外参数的真实命令，并重新执行通过。

### F4：第 7 章成功测试在 Python 3.12 Windows 偶发超时

迁移到目标 Python 3.12 后，`FlakyOperation` 的 2ms sleep 在 25ms 默认阈值下偶发超过 deadline。136 项中 1 项失败。

判断：测试把“明确瞬态错误后重试成功”与“极低延迟调度”耦合；不是第八章契约迁移回归。

修正：测试默认 timeout 调到 100ms，为 Windows/CI 调度留余量。专门的 `HangingOperation` 测试仍每次超时、收到取消并在三次后失败。全量回归变为 136/136。

### F5：CI checkout 使用可移动标签

初版 `actions/checkout@v6` 可被标签更新。GitHub 官方安全建议使用完整 commit SHA。

修正：钉住经核验的 v6.0.2 完整 SHA；`setup-uv` 同样使用完整 SHA，且 uv 版本固定为 0.11.30。

### F6：doctor 声称支持未验证的 MSVC

初版候选包含 `cl`，却统一传 `--version`，并且本章没有 Visual Studio preset。

判断：能力声明超过实现。

修正：本章 doctor 仅检查实际支持的 `g++`/`clang++`。MSVC 留到建立独立探测和 preset 后再宣称支持。

### F7：本机 CMake 不在 PATH

初始环境有 GCC 16.1.0，但无 `cmake` 命令。

修正：将 CMake 4.4.2 放入 uv dev 依赖，使用 `uv run --locked cmake`。这样 Python 工具环境锁定构建前端，原生 GCC 仍由 MSYS2 管理。

---

## 4. 技术栈说明鉴定

**结果：通过。**

| 技术 | 是什么 | 本章为什么使用 | 不负责什么 | 后续桥接 |
|---|---|---|---|---|
| Python 3.12.13 | 目标解释器 | 与课程既有 asyncio/Pydantic 验证一致 | C++ 编译 | 第 9～14 章 Python 端 |
| uv 0.11.30 | Python 项目/环境工具 | 选择 Python、锁定、同步、运行 | C++ 原生编译 | 所有 Python 章节 |
| pyproject.toml | 标准项目元数据 | 依赖、入口、工具配置 | 精确传递解析 | 第 14 章服务入口 |
| uv.lock | 精确跨平台解析 | 可复现安装 | 表达依赖设计意图 | CI/部署 |
| uv_build | 纯 Python build backend | 安装 src 包和 CLI | 编译独立 C++ 服务 | wheel smoke test 待补 |
| src layout | 包目录布局 | 防止根目录偶然导入 | 业务分层本身 | 正式包持续扩展 |
| tomllib | Python TOML 解析器 | 结构化、标准库、无手工切割 | 密钥管理 | 第 14 章启动配置 |
| Pydantic v2 | 运行时数据校验 | 严格配置与领域契约 | 权限强制、SQL 事务 | 所有边界 |
| CMake 4.4.2 | C++ 构建系统前端 | 跨平台 target 与构建图 | 编译器本身 | 第 9～10 章 OpenCV/gRPC |
| GCC 16.1.0 | C++ 编译器 | Windows 首要环境实机编译 | 构建依赖解析 | 第 9 章运行时 |
| CMake Presets | 共享构建配置 | 统一 Windows GCC 命令 | 用户本机私有路径 | 第 9 章扩展 target |
| CTest | C++ 测试执行器 | 注册并执行 runtime test | Python 测试 | 帧队列/质量测试 |
| Ruff 0.15.22 | formatter/linter | 风格与静态错误门 | 类型/业务正确性 | 新正式包 |
| mypy 2.3.0 | 静态类型检查 | strict 检查新代码 | Pydantic 运行时校验 | 逐步扩大范围 |
| pytest 9.1.1 | Python 测试运行器 | 新 pytest 与旧 unittest 共存 | C++ 测试 | 全量回归 |
| GitHub Actions | CI 定义 | 在 Windows/Linux 重做构建 | 本地开发替代品 | 第 14 章发布前门禁 |

正文同时说明 2026 年版本状态：Python 3.12 支持期、uv lock/sync 语义、`uv_build` 上界、CMake target/preset 与 GitHub Action SHA。引用优先使用官方资料。

---

## 5. Demo 鉴定

**结果：通过。**

### 5.1 Python Demo 是否足够小

调用链只有：

~~~text
load_settings
-> TaskSession
-> ObservationRequest
-> run_doctor
-> JSON
~~~

没有 LLM、摄像头、网络或数据库。它集中验证本章新增工程能力，而不是重复 USB-C 规则。

实际输出可验证：

- `session.status=running`
- `view_type=back_label`
- required fields 为功率与协议
- policy capability 有序输出
- doctor 六项 PASS
- `ready=true`

### 5.2 C++ Demo 是否足够小

`RuntimeInfo -> to_json -> stdout`，只证明库、公开头文件、链接与 C++20。实际 JSON 含服务名、0.1.0、C++20 和三个 planned capability。

### 5.3 Demo 是否诚实

通过。planned capability 明确不是 implemented capability；正文说明不代表摄像头/OpenCV/gRPC 已完成。doctor 的 ready 也明确限定为第八章前置条件。

---

## 6. 注释与可学习性鉴定

**结果：通过。**

本章新增或实质修改的 Python 文件均在顶部说明：

- 整体逻辑。
- 使用的技术栈。
- 调用流程。
- 关键边界或模块所有权。

主要文件还对配置白名单、相对路径、doctor 退出码、兼容跨进程测试等位置增加中文注释。空 `__init__.py` 也补充包职责和依赖方向，不让初学者面对只有一句模糊描述的模块。

注释没有逐行翻译语法；它们集中解释“为什么”和“在架构中的位置”。

---

## 7. 前后章节桥接鉴定

**结果：通过。**

### 7.1 向前桥接

- 第 4 章：稳定 Pydantic 模型原样迁入正式包，名称和字段不改。
- 第 5 章：`ActiveObservationState` 仍位于稳定模块，checkpoint 类型未复制。
- 第 6 章：`storage` 包边界已建立，但不冒险一次迁完 SQLite 大文件。
- 第 7 章：治理配置进入 TOML，`governance` 包边界已建立；执行实现仍由原测试保护。
- 120 项前七章测试在 Python 3.12 下继续通过。

兼容层保证历史课程代码可运行，新代码不再使用 `sys.path` 技巧。

### 7.2 向后桥接

第 9 章已有：

- `RealSight::runtime` 库目标。
- 公开 include 路径。
- app 与 test 链接方式。
- C++20 和警告选项。
- Windows GCC preset。
- CTest 和 Linux CI 基础。

因此第 9 章可以增量增加 `Frame`、`FrameSource`、有界队列、回放和 OpenCV，而不重新解决工程入口。

第 10 章可在 contracts 与 services 边界加入 Protobuf 生成 target 和 gRPC adapter；本章没有抢跑。

---

## 8. 实际验证结果

### 8.1 Python

| 检查 | 结果 |
|---|---|
| uv lock/sync | 55 packages resolved，Python 3.12.13 |
| package build/install | realsight 0.1.0 editable install 成功 |
| 第 8 章专项测试 | 16/16 |
| 前 7 章回归 | 120/120 |
| Python 总测试 | 136/136，最终复验 10.63s |
| Ruff format | 通过 |
| Ruff lint | 通过 |
| mypy strict scope | 通过，12 source files |
| Python Demo | 退出码 0，doctor ready=true |

### 8.2 C++

| 检查 | 结果 |
|---|---|
| CMake configure | 通过，CMake 4.4.2 |
| compiler | MSYS2 UCRT64 GCC 16.1.0 |
| library | realsight_runtime 构建成功 |
| app | realsight-perception 构建并运行成功 |
| CTest | 1/1 |

### 8.3 CI

workflow 结构和命令经本地对应命令验证；当前目录不是 Git 仓库，未推送 GitHub，因此没有远程 runner 运行证据。审计不把“写了 YAML”表述成“CI 已通过”。

---

## 9. 残余风险与限制

### R1：正式分包迁移未完成

第 6～7 章实现仍在 examples。风险是后续正式功能可能继续复制代码。缓解：包边界已建立，迁移必须携带既有测试并保持唯一实现。

### R2：mypy 排除正式 contracts models

运行时契约由 Pydantic 测试充分保护，但尚未进入 strict mypy 范围。后续应评估 Pydantic plugin、类型别名和渐进迁移，不应删除验证器换取绿灯。

### R3：Python wheel 独立 smoke test 未完成

editable install 证明开发包可用，不完全证明 wheel 包含所有数据。后续 CI 可 `uv build` 后在隔离环境导入并运行 CLI。

### R4：C++ 测试使用 assert

当前 Debug 构建有效；Release 下 `NDEBUG` 可能移除断言。第 9 章应使用测试框架或显式失败返回，不依赖 assert 作为完整测试基础。

### R5：doctor 只检查工具存在

未结构化验证 CMake/GCC 下限，也不检查 OpenCV、摄像头和驱动。正文明确 ready 语义，后续按章节扩展检查。

### R6：配置覆盖不是密钥管理

无 secret store、脱敏与动态轮换。当前配置不含敏感值；第 14 章部署时补充。

### R7：CI 未远程运行

YAML 可能存在 hosted runner 特有问题。推送仓库后必须以真实 workflow run 作为验收，不以本地静态阅读替代。

### R8：只提供 Windows GCC preset

Linux 使用非 preset CI 命令，MSVC 未声明支持。后续有真实需要再加 preset 与实测，不提前扩大兼容矩阵。

### R9：C++ 与 Python 版本号手工重复

两处当前同为 0.1.0，但无单一生成源。版本发布前应增加 CMake configure 或生成步骤，避免漂移。

### R10：CI 依赖供应链仍需维护

Action 钉 SHA 只避免标签漂移，不自动获得补丁。需要 Dependabot/Renovate 或人工升级流程；锁文件也要定期审计漏洞。

### R11：根目录不是 Git 仓库

无法用 `git diff` 审查 lockfile 或 CI 变更，也不能运行远程 CI。正式项目进入版本控制是下一项外部操作，但用户没有要求本章初始化 Git，因此未擅自创建。

---

## 10. 最终验收矩阵

| 项目 | 结论 | 备注 |
|---|---|---|
| 逻辑闭环 | 通过 | Python/C++ 两条构建链均闭环 |
| 技术栈清楚 | 通过 | 是什么、为什么、边界、后续均说明 |
| 小 Demo | 通过 | Python bootstrap + C++ runtime info |
| Demo 可运行 | 通过 | 两者实际退出码 0 |
| Python 注释 | 通过 | 顶部总览、技术栈、调用流程齐全 |
| 前章桥接 | 通过 | 唯一模型实现、旧路径兼容、120 回归 |
| 后章桥接 | 通过 | C++ target 可增量加入 Frame/OpenCV |
| Python 3.12 实测 | 通过 | 3.12.13，不再使用全局 3.13 |
| 锁文件 | 通过 | uv.lock 已生成并 locked sync |
| C++ 构建 | 通过 | GCC 16.1.0、CMake 4.4.2 |
| 自动测试 | 通过 | Python 136，C++ 1 |
| 远程 CI | 未验证 | 仅完成 workflow 定义和本地等价命令 |
| 生产部署 | 不在本章 | 无 Docker/secret/SBOM/signing |

综合判断：第八章可以交付。它没有把目录美化误当作工程完成，而是用 Python 3.12 安装、锁文件、包身份、配置校验、doctor、CMake 构建和双语言测试建立可验证工程边界。交付后应停止在本章，等待用户审核，再进入第九章 C++ 实时感知运行时。
