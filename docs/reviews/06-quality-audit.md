# 第 6 章质量鉴定报告

## 1. 鉴定结论

**结论：逻辑闭环、技术栈说明、小 Demo、前后章节桥接四项通过，可以交付第六章；但通过范围严格限定为“单机、串行、教学 MVP 的跨进程恢复”，不能据此宣称已经具备多实例生产可靠性。**

本章没有因 SqliteSaver 首次运行成功就直接判定通过。独立审计重点检查了五个容易被成功路径掩盖的问题：

1. checkpoint 与 Evidence Ledger 同时保存 Evidence 是否构成职责冲突。
2. 两个 SQLite 数据库和文件系统无法被同一事务提交，崩溃时如何解释中间状态。
3. 图片原子替换、内容哈希与来源真实性是否被混为一谈。
4. SQLite Connection 上下文是否真正释放 Windows 文件句柄。
5. 图到达 RUN_RULES 后是否被错误标记为业务完成。

最终处理：

- 明确 checkpoint 与 ledger 是不同用途的投影，并定义对账方向。
- 将 Evidence 分为不可变 candidate 与 acceptance 关系。
- 每次打开 session 都从 BeliefState.ledger 幂等补做 acceptance。
- Artifact 使用相对路径、SHA-256、大小、MIME 和路径边界校验。
- 正文明确 SHA-256 不证明来源真实，`os.replace` 也不是断电级耐久保证。
- 显式关闭所有业务 SQLite 连接；测试辅助函数改用 `contextlib.closing()`。
- RUN_RULES 仍保持 SessionStatus.RUNNING，因为第 12 章规则尚未执行。

---

## 2. 逻辑闭环鉴定

**结果：通过。**

本章逻辑链：

~~~text
第五章 InMemorySaver 只能同进程恢复
-> thread_id 只是查询键，不包含状态
-> 引入 SqliteSaver 保存 LangGraph 执行历史
-> 每个阶段在新进程重建图并打开同一 checkpoint DB
-> 从 snapshot 读取当前 interrupt，而非复用内存变量
-> 图片写入内容寻址 artifact 目录
-> Observation 与 Evidence 先登记为不可变 candidate
-> 第五章安全恢复入口预检并消费当前 interrupt
-> 新 Evidence 进入 BeliefState.ledger
-> reconcile 写入 acceptance 关系
-> 下次进程启动可修复 checkpoint 已提交但 acceptance 未提交的窗口
-> 三次恢复后到 RUN_RULES
-> inspect 重新打开并校验数据库与图片
~~~

### 2.1 checkpoint 与业务 ledger 的重复是否合理

初看两处都保存 Evidence，可能被误解为“双重真相”。审计后保留该设计，但正文增加了权威方向：

- checkpoint 的 BeliefState.ledger 是“图已接纳集合”的权威来源。
- evidence_records 是“系统收到集合”的权威来源。
- evidence_acceptances 是二者之间的查询投影。

因此不从 candidate 反向强塞 Graph State，也不直接查询 LangGraph 内部表做业务审计。重复是不同所有者的投影，不是两个模块任意互相覆盖。

### 2.2 跨库一致性是否被虚假原子化

两个 SQLite 文件与 artifact 文件系统无法共享普通事务。代码和正文没有声称“一次恢复全局原子”，而是定义可解释顺序：

1. 保存 artifact。
2. 保存 ArtifactRecord、Observation candidate、Evidence candidate。
3. 恢复 LangGraph。
4. 保存 acceptance。

图拒绝输入时 candidate 可以存在但没有 acceptance；图已提交而 acceptance 缺失时，下次启动对账修复。测试通过删除一项 acceptance 验证修复，而不是只说明理论。

### 2.3 最终状态是否夸大

`resume-user` 后 route 为 RUN_RULES，checkpoint 没有下一节点，但 SessionStatus 仍为 RUNNING。这不是状态遗漏：本章只生成规则 Action，没有执行第 12 章兼容规则，也没有生成最终答案。正文已经明确说明不能标 COMPLETED。

### 2.4 生命周期是否清楚

- session manifest 固定身份与布局，不复制动态 State。
- checkpointer 连接只在 graph context 内存在，不进入 checkpoint。
- EvidenceStore 每次操作短连接并显式关闭。
- artifact 文件长期存在于 session 目录，State 只保存相对路径。
- 长期知识本章不创建表或目录。

---

## 3. 技术栈清晰度鉴定

**结果：通过。**

| 技术 | 本章角色 | 选择理由 | 明确限制 | 后续章节 |
|---|---|---|---|---|
| BaseCheckpointSaver | 图与存储实现的抽象边界 | 同一图可切换内存/SQLite | 不定义业务 schema | 第 8/13 章复用 |
| SqliteSaver 3.1.0 | thread checkpoint | 本地跨进程恢复、安装轻 | 非多实例生产后端 | 后期可换 Postgres |
| sqlite3 | Evidence Ledger | 标准库、事务、外键、易观察 | 无连接池、多 writer 受限 | 第 8 章仓储化 |
| Pydantic v2 | manifest/artifact/领域契约 | 拒绝损坏和未知输入 | 不保证磁盘原子性 | 持续复用 |
| SHA-256 | 内容寻址和完整性 | 稳定 key、重试去重 | 不证明采集来源真实 | 第 10/11 章复用 |
| os.replace | 正常运行下原子替换 | 避免读取半写文件 | 未实现 fsync 断电保证 | 可增加耐久写策略 |
| subprocess | 验证进程边界 | 排除内存对象复用 | 不是生产进程调度 | 第 14 章 API 替代 |
| SQLite WAL | 本机读写协作 | 读写可并行、调试方便 | 单 writer、非网络文件系统 | 高并发迁移数据库 |

正文说明了每项技术“是什么、为何使用、不负责什么、后续如何演进”。参考当前官方 [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[SQLite WAL](https://www.sqlite.org/wal.html)、[SQLite Foreign Keys](https://www.sqlite.org/foreignkeys.html) 与 [Python sqlite3](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)。

### 修正问题 A：SQLite 连接未真正关闭

第一轮 9 项测试中，8 项行为通过，但最后一个测试在 tearDown 删除 checkpoints.sqlite3 时出现 Windows WinError 32。

根因：测试辅助函数使用 `with sqlite3.connect(...)`，误以为退出 with 会关闭 Connection。实际只处理事务提交/回滚。

修正：测试使用 `contextlib.closing()`；业务 `EvidenceStore.connection()` 原本已在 finally 中显式 close。修正后专项和全量回归通过。

### 修正问题 B：第五章构建器类型过窄

原签名只接受 InMemorySaver，运行时虽可传 SqliteSaver，但类型契约错误。

修正：参数和返回值改为 BaseCheckpointSaver；默认仍创建 InMemorySaver，现有第五章行为不变。

---

## 4. Demo 鉴定

**结果：通过。**

### 4.1 是否足够小

Demo 不依赖 LLM、摄像头、OpenCV、网络或真实 OCR。图片使用标准库生成可解码 PPM，重点集中在持久化和边界，不提前实现第 9～12 章专业能力。

### 4.2 是否真的跨进程

主 Demo 使用 subprocess 创建五个解释器，实测 PID：

~~~text
[131184, 119016, 106924, 80708, 86628]
distinct_process_count = 5
~~~

每个阶段都重新：

- 导入图定义。
- 打开 checkpoint DB。
- 查询同一 thread。
- 从 snapshot 读取当前 interrupt。
- 关闭连接并退出。

这比同进程重建 graph 更强，排除了复用 InMemorySaver、全局变量和打开连接的可能。

### 4.3 输出是否可验证

~~~text
start        -> 3 missing / 2 accepted evidence
resume-label -> 2 missing / 3 accepted evidence / 1 artifact
resume-port  -> 1 missing / 4 accepted evidence / 2 artifacts
resume-user  -> 0 missing / 5 accepted evidence / RUN_RULES
inspect      -> 5 acceptances / 2 verified artifacts / no knowledge
~~~

最终表计数：

~~~text
sessions = 1
artifacts = 2
observations = 2
evidence_records = 5
evidence_acceptances = 5
~~~

### 4.4 失败路径是否覆盖

- 相同 evidence_id 不同内容抛 ImmutableRecordConflict。
- 已有 session 拒绝 start 覆盖。
- 图片被追加字节后抛 ArtifactIntegrityError。
- acceptance 缺失后可自动修复。
- checkpoint/evidence schema 分离测试通过。

未覆盖的失败路径列在残余风险中，没有因正常 Demo 成功而判定为已解决。

### 4.5 代码可读性

- Demo 顶部大注释：整体逻辑、技术栈、调用流程、跨库设计理由、阅读顺序。
- 测试顶部大注释：测试结构、技术栈、调用流程、章节边界。
- 路径、schema、事务、幂等、对账、产物和阶段函数均有中文注释。
- 代码较长，但按 SessionLayout -> EvidenceStore -> durable graph -> phase 命令顺序组织，适合逐段学习。

---

## 5. 前后章节桥接鉴定

**结果：通过。**

### 5.1 向前桥接

- 复用第一章 Observation/Evidence/Belief State 边界。
- 复用第二章 Action/RunEvent 思想；不把数据库写入伪装成 Agent。
- 复用第三章服务生命周期边界；连接和文件句柄不进入业务 State。
- 复用第四章 Pydantic 正式契约与 schema_version。
- 原样复用第五章状态图、interrupt、预检和 Evidence 合并。
- 只把 checkpointer 抽象从 InMemorySaver 放宽到 BaseCheckpointSaver，没有重命名稳定领域类型。

### 5.2 向后桥接

- 第 7 章：thread 锁、预算、超时、重试、权限、审计和候选清理。
- 第 8 章：把示例仓储整理为正式 Python 包、pyproject、uv.lock 和迁移机制。
- 第 9～10 章：C++ 关键帧写入同类 Artifact 接口，gRPC Observation 关联 request。
- 第 11 章：视觉工具把真实 Observation 转成 Evidence candidate。
- 第 12 章：长期设备资料独立存储，不污染 session ledger。
- 第 13 章：主图继续使用持久 checkpointer 和 reconcile。
- 第 14 章：API 只暴露安全恢复入口，并流式返回持久 RunEvent。

章节结尾明确提出第七章的问题，没有提前实现治理代码。

---

## 6. 测试鉴定

第六章专项：**9/9 通过**。

前六章全项目回归：**101/101 通过**。

运行条件：

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
~~~

实测依赖：

~~~text
Python 3.13.13
langgraph 1.2.10
langgraph-checkpoint 4.1.1
langgraph-checkpoint-sqlite 3.1.0
pydantic 2.13.4
~~~

额外验证：

- 所有章节 Python 文件通过 py_compile。
- 第五章 22 项回归单独通过，checkpointer 类型泛化未改变行为。
- 严格 msgpack 模式下跨进程恢复无警告。
- 第六章正文约 2.2 万字符，Demo/测试均有顶部教学注释。

---

## 7. 残余风险

### R1：同一 thread 并发恢复未治理

严重度：高，属于第 7 章明确任务。

两个进程可能同时读取同一个 interrupt，通过预检后争抢恢复。SQLite busy timeout 不能决定业务所有权。

处理：第七章增加 thread 级互斥/租约、幂等 command key 和并发测试。

### R2：跨资源仍为最终一致

严重度：中。

对账可以修复 acceptance 缺失，但 artifact 写入后数据库提交前崩溃可能留下孤儿文件；candidate 永久未接纳也会积累。

处理：增加 orphan 扫描、candidate 状态查询、保留期与垃圾回收；更复杂场景采用 outbox/inbox。

### R3：SQLite 只适合本机小规模

严重度：中，范围内可接受。

WAL 不是网络共享方案，也没有多实例连接池、在线迁移和高可用。

处理：保持 BaseCheckpointSaver 和仓储接口；生产部署评估 Postgres saver、Postgres Evidence Store 与对象存储。

### R4：原子替换不等于断电耐久

严重度：中低。

代码没有对文件和父目录执行 fsync，也没有测试突然断电。SHA-256 能在下次读取发现损坏，但不能保证损坏永不发生。

处理：根据部署平台增加 durable write、备份与恢复演练。

### R5：SHA-256 不证明来源真实性

严重度：中。

哈希只能证明当前字节与已登记摘要一致。若攻击者同时修改文件和数据库，或采集源一开始就伪造，哈希无法识别。

处理：限制文件权限，增加审计、可信采集标识；高安全场景考虑签名与外部不可变日志。

### R6：Python 3.12 尚未实机验证

严重度：中低。

课程目标为 Python 3.12，但当前 `.venv` 实际是 3.13.13。所用语法与依赖声明支持 3.12，不等于已经实测。

处理：第八章用 uv 创建 3.12 环境并执行 101+ 回归。

### R7：隐私、加密和保留策略未实现

严重度：真实产品中高，本月 MVP 未纳入。

图片可能含序列号、环境和个人信息。当前没有数据库加密、访问控制、自动删除和用户删除流程。

处理：第七章先增加权限与审计概念；正式产品另做隐私威胁建模和数据治理。

### R8：checkpoint/ledger schema 升级策略未实现

严重度：中。

已有 schema_version 和 PRAGMA user_version，但没有迁移脚本。图节点或 Pydantic 类重命名也可能影响旧 checkpoint。

处理：第八章工程化时加入迁移、兼容测试和发布约束。

---

## 8. 最终验收表

| 验收项 | 结论 | 证据 |
|---|---|---|
| 逻辑闭环 | 通过 | 持久 checkpoint -> candidate -> resume -> acceptance -> reconcile |
| 数据职责边界 | 通过 | checkpoint/evidence/artifact/knowledge 四类分离 |
| 技术栈说明 | 通过 | 是什么、为什么、限制、后续替换均有说明 |
| 小 Demo | 通过 | 五个独立进程，无 LLM/摄像头依赖 |
| 输出可验证 | 通过 | PID、interrupt、missing、表计数、hash 均可检查 |
| 失败路径 | 通过（本章范围） | 冲突、篡改、覆盖、对账测试 |
| 前章桥接 | 通过 | 复用第 4～5 章契约和图 |
| 后章桥接 | 通过 | 明确进入治理、工程化、C++、视觉、规则、API |
| Python 注释 | 通过 | 新 Demo/测试均有顶部大注释和关键块中文解释 |
| 严格序列化 | 通过 | strict msgpack 跨进程无警告 |
| 第六章测试 | 通过 | 9/9 |
| 全项目回归 | 通过 | 101/101 |
| 多进程同 thread 并发 | 未完成 | 第 7 章处理 |
| 跨机器/生产高可用 | 未完成 | SQLite 明确限定本机 MVP |
| 断电级耐久 | 未完成 | 未使用 fsync/备份演练 |
| Python 3.12 实测 | 待第 8 章 | 当前实际 3.13.13 |

综合判断：第六章可以交付。它证明的是可检查、可解释的单机跨进程恢复，不是所有持久化和并发问题已经结束。应停止在本章，等待用户审核后再进入第七章。
