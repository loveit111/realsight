# 第 6 章 检查点、证据账本与产物存储：让任务在进程退出后继续

> 本章关键词：持久化、LangGraph SqliteSaver、thread checkpoint、SQLite、Evidence Ledger、append-only、幂等、内容寻址、SHA-256、原子写入、跨库对账、进程重启恢复

第五章已经完成了真正的 `interrupt()` 与 `Command(resume=...)`：RealSight 可以请求背面标签，暂停；收到 Observation 后继续，请求接口特写；再次继续，询问笔记本型号。这个控制流本身是正确的。

但第五章的 checkpointer 是 `InMemorySaver`。它把状态保存在一个 Python 对象里。只要解释器退出，这个对象、thread checkpoint 和当前 interrupt 就全部消失。重新运行脚本得到的是一张相同的图，不是原来那次任务。

第六章解决的问题不是“多保存一个 JSON 文件”，而是建立可恢复系统的存储边界：

> 执行位置、业务证据、图像产物和跨会话知识具有不同生命周期、查询方式、体积与可信度，不能塞进同一个状态对象或同一张万能表。

本章把第五章状态图原样接到 LangGraph `SqliteSaver`，并用五个独立 Python 进程完成启动、标签恢复、接口恢复、用户恢复和最终检查。与此同时，我们建立一个独立 Evidence Ledger 和内容寻址产物目录。这样系统不仅“能继续”，还可以回答：哪个进程接收了什么、证据从哪张图提取、文件是否被篡改、工作流是否真正接纳了该证据。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 解释内存状态、持久化 checkpoint 和业务数据库的区别。
2. 说明为什么 `thread_id` 相同还不够，checkpointer 本身也必须持久化。
3. 使用 `langgraph-checkpoint-sqlite` 的 `SqliteSaver` 跨进程恢复图。
4. 说明 checkpoint 中为什么不能保存数据库连接、摄像头对象或图片字节。
5. 区分执行状态、Observation、Evidence、Artifact 和长期知识。
6. 设计 session manifest 与稳定目录结构。
7. 使用独立 SQLite 数据库存放会话、观察、候选证据和接纳关系。
8. 理解 append-only、幂等写入和 immutable ID 的意义。
9. 解释两个 SQLite 数据库为什么不能被一个普通事务原子提交。
10. 使用“候选记录 + 接纳关系 + 重启对账”缩小崩溃窗口。
11. 用 SHA-256、相对路径、大小和 MIME 类型管理图片产物。
12. 解释 `os.replace` 能保证什么、不能保证什么。
13. 正确启用 SQLite foreign keys、WAL、busy timeout 和显式关闭连接。
14. 运行五进程 Demo，并根据 PID、interrupt、表计数判断是否真的恢复。
15. 判断 SQLite 本地方案适合教学 MVP 的原因及其生产边界。
16. 说明第七章为何还要增加并发、超时、预算和审计治理。

### 1.2 学习前提

需要理解前五章的这些内容：

- `TaskSession.session_id == thread_id` 的课程 MVP 约束。
- RealityObject、Observation、Evidence、BeliefState、Action、RunEvent。
- Pydantic 正式契约与严格 checkpoint 序列化白名单。
- 第五章 `prepare -> interrupt -> resume -> apply -> assess` 循环。
- 错误恢复必须在发送 Command 前预检。
- Observation 表示一次观察结果，Evidence 表示从来源中提取的业务事实。

不要求掌握数据库索引优化、分布式事务或对象存储。第六章只做单机、单用户、单任务串行恢复。它不接入摄像头、LLM 或真实 OCR。

### 1.3 本章产物

- `requirements-ch06.txt`，固定 `langgraph-checkpoint-sqlite==3.1.0`。
- 通用 `BaseCheckpointSaver` 图构建入口。
- `SessionManifest` 与 `SessionLayout`。
- `checkpoints.sqlite3` 持久化执行状态。
- `evidence.sqlite3` 不可变证据账本。
- `artifacts/<session_id>/` 内容寻址图片目录。
- 候选 Evidence 与 Evidence Acceptance 两阶段记录。
- 从 checkpoint 到 Evidence Ledger 的幂等对账。
- 五个独立进程组成的可运行 Demo。
- 图片篡改、不可变 ID、跨库恢复、存储边界测试。
- 独立质量鉴定报告。

本章新建的 Demo 和测试文件都在文件顶部提供中文大注释，明确整体逻辑、技术栈、调用流程和章节边界。关键类、事务和阶段函数均有中文 docstring 或行内说明。

---

## 2. 第五章究竟还缺什么

### 2.1 重建 graph 不等于恢复任务

下面两段代码看起来相似，语义完全不同。

第一种是在同一内存 saver 中恢复：

~~~python
graph, saver = build_active_observation_graph()
graph.invoke(initial_state, config=config)
graph.invoke(Command(resume=payload), config=config)
~~~

第二种在程序重启后重新创建 `InMemorySaver`：

~~~python
graph, new_saver = build_active_observation_graph()
graph.invoke(Command(resume=payload), config=config)
~~~

新 saver 中没有原 thread。即使 `thread_id` 字符串完全相同，也没有 checkpoint 可读。`thread_id` 是查询键，不是状态本身。

可以把它类比为：知道书签名字叫 `session-001`，不代表书签所在的书还存在。持久 checkpointer 同时保存“书”和“书签位置”。

### 2.2 只保存最终 JSON 为什么不够

一种常见初版方案是在每次暂停时写：

~~~json
{
  "session_id": "session-001",
  "missing_fields": ["charger_label"]
}
~~~

它没有保存：

- 图下一步应从哪个节点继续。
- 当前 interrupt ID。
- pending task 与 channel version。
- 节点执行错误和 checkpoint 历史。
- LangGraph 恢复所需的内部元数据。

手写 JSON 可以成为业务快照，却不能冒充框架 checkpoint。反过来，也不应直接查询 LangGraph 内部表来做业务报表，因为 checkpointer schema 属于框架实现，不是我们的 Evidence 查询契约。

### 2.3 “持久化”至少有三层问题

本章必须同时回答：

1. **可执行恢复**：程序重启后，图能否找到当前 interrupt 并继续？
2. **业务追溯**：能否知道系统收到哪些 Observation/Evidence，哪些已被接纳？
3. **文件完整性**：Evidence 指向的图片是否仍存在、是否还是原内容？

只解决第一项，会得到能继续但难审计的 Agent；只解决第二项，会得到有记录但不会恢复的普通数据库应用；只把图片复制进目录，又无法知道它属于哪个 Observation。

---

## 3. 四类数据必须分开

### 3.1 执行状态：系统运行到哪里

执行状态包括：

- ActiveObservationState。
- 当前节点和下一节点。
- interrupt payload 与 interrupt ID。
- pending request/action。
- BeliefState 的当前认知快照。
- LangGraph channel version 和 task metadata。

它按 `thread_id` 组织，由 checkpointer 维护。应用通过 `graph.get_state()` 使用，不直接依赖内部 SQL 表。

### 3.2 证据账本：系统收到了什么

业务账本包括：

- 一次 Observation 的结构化元数据。
- 一条候选 Evidence 的完整值与来源。
- 哪个 thread 接纳了哪个 evidence_id。
- session 与 RealityObject 的绑定。

它需要按字段、来源和 evidence_id 查询，需要明确不变量和迁移策略。它属于 RealSight，不属于 LangGraph。

### 3.3 产物：大文件本身

产物包括关键帧、裁剪图、OCR 中间图片和未来的短视频片段。这些文件：

- 体积远大于结构化状态。
- 经常要被图像库直接读取。
- 需要生命周期清理和完整性校验。
- 后期可能迁移到对象存储。

因此本章将图片放在文件系统，SQLite 只保存相对路径、SHA-256、字节数和 MIME 类型，不使用 BLOB 塞进 checkpoint。

### 3.4 长期知识：跨任务复用的资料

长期知识可能是：

- 某笔记本官方 USB-C 输入功率。
- 某充电器型号的官方档位表。
- USB PD 标准版本和约束。
- 厂商说明书的来源 URL、发布日期与版本。

这些资料不属于某次摄像头 session，也不能因为某次 OCR 读到“65W”就自动升级成长期真理。它们需要独立来源审核、版本和有效期，第十二章资料检索与规则引擎再建立。

### 3.5 四类数据对照表

| 数据类别 | 本章载体 | 主键/定位 | 生命周期 | 主要读取者 |
|---|---|---|---|---|
| 执行状态 | checkpoints.sqlite3 | thread_id + checkpoint | 一次任务执行历史 | LangGraph |
| 结构化证据 | evidence.sqlite3 | session_id + evidence_id | 审计/保留期内 | 工作流、规则、审计 |
| 图片产物 | artifacts/session_id/hash.ppm | SHA-256 + 相对路径 | 会话或归档策略 | 视觉工具、人工复核 |
| 长期知识 | 本章不创建 | 文档/设备/版本 ID | 跨 session | 第 12 章检索工具 |

这里允许同一 Evidence 同时出现在 checkpoint 的 BeliefState 和业务 ledger 中，因为它们是面向不同职责的两种投影：checkpoint 回答“图当时相信什么并从哪里继续”，ledger 回答“系统收到什么、哪个 thread 接纳了什么”。重复数据是有意的，关键是定义对账方向和不直接修改框架内部表。

---

## 4. 第六章存储架构

~~~mermaid
flowchart LR
    CLI["独立 Python 进程"] --> G["第 5 章 LangGraph"]
    G --> CP["SqliteSaver / checkpoints.sqlite3"]
    CLI --> ES["EvidenceStore / evidence.sqlite3"]
    CLI --> FS["artifacts/session_id/SHA-256.ppm"]
    FS -->|"相对路径 + hash"| ES
    G -->|"BeliefState.ledger"| REC["幂等 reconcile"]
    REC --> ES
    K["长期知识"] -.->|"第 12 章再实现"| CLI
~~~

目录结构：

~~~text
<storage-root>/
├── session.json
├── checkpoints.sqlite3
├── evidence.sqlite3
└── artifacts/
    └── session-ch06-001/
        ├── <label-sha256>.ppm
        └── <port-sha256>.ppm
~~~

没有 `knowledge/`，也没有把图片内容写进 SQLite。

### 4.1 为什么使用两个 SQLite 数据库

使用两个数据库不是因为 SQLite 不能建更多表，而是为了让所有权清楚：

- `checkpoints.sqlite3` 的 schema 和写入时机归 LangGraph。
- `evidence.sqlite3` 的 schema、索引、不变量和迁移归 RealSight。
- 将来 checkpointer 可换 Postgres，Evidence Store 也可独立迁移。
- 业务代码不会误把框架内部 checkpoint 行当正式审计 API。

代价是两个数据库不能共享一个普通原子事务。这个代价不会被隐藏，第九节专门处理。

### 4.2 为什么不为每个图片创建一个数据库

SQLite 适合结构化元数据，不适合把大量图片当行内 BLOB 反复复制。文件系统已经擅长顺序读写大文件，图像库也接受路径。未来迁移对象存储时，ArtifactRecord 的 `relative_path` 可演进为受控 object key，Evidence 和 Observation 契约不必整体重写。

---

## 5. LangGraph SqliteSaver

### 5.1 独立依赖

LangGraph 官方持久化文档把 checkpointer 实现拆成独立包：`langgraph-checkpoint` 提供基础接口和内存实现，`langgraph-checkpoint-sqlite` 提供 `SqliteSaver`/`AsyncSqliteSaver`，适合实验和本地工作流；生产环境通常考虑 Postgres saver。[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

本章固定：

~~~text
langgraph-checkpoint-sqlite==3.1.0
~~~

PyPI 当前包说明要求 Python 3.10+，并强调严格 msgpack 或显式反序列化允许列表。[langgraph-checkpoint-sqlite on PyPI](https://pypi.org/project/langgraph-checkpoint-sqlite/)

### 5.2 图构建器依赖抽象接口

第五章原函数只把参数标注成 `InMemorySaver`。第六章将它改为：

~~~python
def build_active_observation_graph(
    checkpointer: BaseCheckpointSaver | None = None,
) -> tuple[Any, BaseCheckpointSaver]:
~~~

图节点不知道底层是内存还是 SQLite。这是依赖倒置的一个小例子：业务工作流依赖 checkpointer 协议，不依赖某个存储实现。

### 5.3 显式 serializer 白名单

~~~python
serializer = JsonPlusSerializer(
    allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES,
)
saver = SqliteSaver(connection, serde=serializer)
graph, _ = build_active_observation_graph(saver)
~~~

第六章继续沿用第五章的显式类型白名单，并在测试子进程中设置：

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
~~~

持久化意味着不可信数据在磁盘停留更久，不能为了方便改成任意 Python 类型反序列化。

### 5.4 每个进程重新构建图

恢复过程不是反序列化整个 graph 对象。每个进程都会：

1. 从代码重新构建相同图定义。
2. 打开同一个 checkpoints.sqlite3。
3. 使用同一个 thread_id 查询 snapshot。
4. 从 snapshot.tasks 读取当前 interrupt。
5. 通过安全恢复入口提交 Command。
6. 关闭连接并退出。

稳定图定义、稳定状态类路径和 schema 兼容性都因此变得重要。若部署新版代码时删除旧节点或重命名可持久化类，旧 checkpoint 可能无法恢复。第八章工程化时需要把迁移和发布策略纳入版本管理。

### 5.5 SQLite Saver 的边界

SQLite saver 适合本地教学、CLI、个人桌面应用和小规模单机工作流。它不是“用了数据库就自动企业级”：

- 同步 saver 不适合高并发多写。
- SQLite WAL 要求相关进程位于同一台主机，不能把数据库文件当网络共享盘使用。
- 多实例服务需要明确锁、连接池、备份和迁移。
- 正式多节点部署通常改用 Postgres 或由 Agent Server 管理持久化。

本课程与 2026 企业实践接轨的重点是 checkpointer 抽象、职责分离、幂等、审计和故障模型，而不是把 SQLite 宣传成任意规模的最终后端。

---

## 6. SessionManifest：稳定索引，不是第二份 State

### 6.1 Manifest 内容

~~~python
class SessionManifest(StrictContract):
    schema_version: Literal[1] = 1
    session_id: Identifier
    target_id: Identifier
    created_at: AwareDatetime
    checkpoint_database: Literal["checkpoints.sqlite3"]
    evidence_database: Literal["evidence.sqlite3"]
    artifact_directory: Literal["artifacts"]
~~~

它只回答：

- 这是哪个 session？
- 它绑定哪个 RealityObject？
- 存储布局版本是什么？
- 三类存储在哪里？

它不保存 `missing_fields`、当前 interrupt 或最终判断。动态状态只放在 checkpoint，避免两份可变 JSON 互相覆盖。

### 6.2 原子替换

写 manifest 时先在同目录创建临时文件，再调用：

~~~python
os.replace(temporary, layout.manifest_path)
~~~

普通读取者要么看到旧完整文件，要么看到新完整文件，不会看到 Python 正在写到一半的 JSON。必须在同一目录创建临时文件，因为跨文件系统移动不一定具有相同语义。

这仍不是“断电绝不丢失”的完整承诺。若要抵抗突然掉电，需要研究文件和目录 `fsync`、底层文件系统与部署平台。本章只保证正常进程并发读取时不暴露半写文件。

### 6.3 不覆盖已有会话

`start` 若发现 session.json 已存在会抛出 `SessionAlreadyExists`。恢复必须走 `resume-*` 命令。静默覆盖会造成：

- 新 manifest 指向旧 checkpoint。
- 同一 session_id 换绑另一个 target。
- 旧 Evidence 被新任务误引用。

“创建”和“恢复”是两个不同命令，不靠猜测决定。

---

## 7. Evidence Ledger 的表设计

### 7.1 sessions

`sessions` 固定 session 与 target 的关系。相同 session_id 只能重复写完全相同的 manifest，不能以后换一个 target_id。

### 7.2 artifacts

保存：

- session_id。
- artifact_id。
- relative_path。
- sha256。
- byte_size。
- mime_type。
- created_at。
- 完整版本化 payload_json。

复合主键是 `(session_id, artifact_id)`；同 session 的 SHA-256 唯一。

### 7.3 observations

保存 Observation 完整 JSON，并用外键关联 ArtifactRecord。Observation 记录“某个请求得到了一次什么质量的观察”，不直接表示其中每个字段都可信。

### 7.4 evidence_records

保存候选 Evidence 的完整 JSON、field、source 和状态。候选意味着“系统收到或构造过这条结构化事实”，并不意味着图一定接纳。

例如：

~~~text
Evidence: charger_label = "USB-C PD 65W"
source_type: vision_extraction
source_id: observation-ch06-label-001
~~~

若 request_id、target_id 或字段不匹配，第五章恢复入口会拒绝它；候选记录仍可留作审计。

### 7.5 evidence_acceptances

保存：

~~~text
(session_id, evidence_id, thread_id, accepted_at)
~~~

它表达的是一个关系事件：某个 thread 的 BeliefState 已经包含这条 Evidence。把 acceptance 单独建表有两个好处：

- 不更新原 Evidence 行，保持候选事实不可变。
- 一个候选事实是否被工作流采用可以独立查询。

### 7.6 外键必须每个连接显式开启

SQLite 官方文档说明，为兼容历史行为，foreign key enforcement 通常需要应用在每个连接上显式执行 `PRAGMA foreign_keys = ON`。[SQLite Foreign Key Support](https://www.sqlite.org/foreignkeys.html)

因此 `EvidenceStore.connection()` 每次都会：

~~~python
connection.execute("PRAGMA foreign_keys = ON")
~~~

只在 CREATE TABLE 中写 `FOREIGN KEY` 而不启用 enforcement，会得到看起来有关联、实际可写孤儿行的数据库。

---

## 8. 不可变记录与幂等

### 8.1 append-only 不等于永不增加表

本章的“不可变”指：

- 相同 session_id 不改绑 target。
- 相同 observation_id 不改成另一张图。
- 相同 evidence_id 不改 value/source/status。
- 相同 artifact_id 不改 hash/path/size。

系统可以追加新记录，也可以追加 acceptance 关系，但不能用 UPDATE 把过去事实悄悄改写。

### 8.2 为什么 ID 必须稳定

网络重试和进程恢复可能重复发送同一 Observation。若每次重试都产生随机 ID，账本会出现多条无法判断是否重复的事实。

本章规则：

~~~text
相同 ID + 相同 canonical JSON -> 幂等成功，不重复插入
相同 ID + 不同 canonical JSON -> ImmutableRecordConflict
~~~

稳定 JSON 使用 `sort_keys=True`，所以字典键顺序不同不会被误判为内容变化。

### 8.3 为什么不能 INSERT OR REPLACE

`INSERT OR REPLACE` 常被误用为“方便的 upsert”。对于审计数据，它可能先删除旧行再插入新行，改变历史并触发外键行为。Evidence Ledger 更安全的策略是：

1. 按主键查询现有 payload。
2. 相同则返回幂等结果。
3. 不同则抛冲突。
4. 不存在才插入。

第七章增加并发写时，还需要把“查后插”的竞态收紧为事务锁、唯一约束冲突处理或数据库原子语句。本章串行 MVP 已由唯一约束提供最终防线，但不宣称解决多个写进程同时争抢同一 ID。

---

## 9. 两个数据库之间的崩溃窗口

### 9.1 不可能的理想事务

我们希望一次视觉恢复原子完成：

~~~te
保存图片
+ 写 Observation
+ 写 Evidence
+ LangGraph checkpoint 接纳 Evidence
= 全部成功或全部失败
~~~

但图片在文件系统，业务记录在 evidence.sqlite3，图状态在 checkpoints.sqlite3。普通 SQLite 事务不能同时覆盖这三种资源。SQLite 官方 WAL 文档也明确提醒，涉及多个数据库的事务即使对每个数据库分别原子，也不对所有数据库整体原子；WAL 还要求所有进程位于同一主机。[SQLite WAL](https://www.sqlite.org/wal.html)

隐藏这个事实比接受短暂不一致更危险。

### 9.2 本章的提交顺序

视觉恢复采用：

~~~text
1. 内容寻址写图片
2. 记录 ArtifactRecord
3. 记录 Observation 候选
4. 记录 Evidence 候选
5. 预检并恢复 LangGraph
6. 从新 BeliefState.ledger 写 acceptance
~~~

如果第 1～4 步成功而第 5 步失败，账本中有“收到但未接纳”的候选。这是可解释状态，不是假成功。

如果第 5 步成功而第 6 步前进程崩溃，checkpoint 已接纳 Evidence，但 acceptance 暂时缺失。下一次打开 session 会修复。

### 9.3 reconcile_state

~~~python
for evidence in state.belief.ledger.values():
    record_evidence_candidate(evidence)  # 幂等
    insert_acceptance_if_missing(
        evidence_id=evidence.evidence_id,
        thread_id=state.session.thread_id,
    )
~~~ 

每个 `start`、`resume` 和 `inspect` 都会对账。测试会人工删除 laptop_model 的 acceptance，随后重新运行 inspect；结果显示：

~~~text
repaired_acceptances = 1
evidence_acceptances = 5
~~~

### 9.4 对账的权威方向

本章约定：

- checkpoint 的 BeliefState.ledger 是“图已接纳集合”的权威来源。
- evidence_records 是“系统收到集合”的权威来源。
- evidence_acceptances 是两者的可查询关系投影。

不能反过来看到 candidate 就直接塞入 BeliefState，因为候选可能被 request/target/view 校验拒绝。

### 9.5 尚未解决的窗口

本章仍可能留下：

- 图片已写、ArtifactRecord 尚未提交的孤儿文件。
- 候选证据已写，但外部请求永远不再重试。
- 两个进程同时恢复同一 interrupt 的竞态。
- 机器断电造成的文件系统级持久性问题。

后续可加入垃圾回收扫描、outbox/inbox、任务租约、幂等 command key 和 thread lock。第七章先处理运行治理；更完整的分布式可靠性不在一个月 MVP 范围内。

---

## 10. 图片产物的工程规则

### 10.1 内容寻址

~~~python
digest = hashlib.sha256(content).hexdigest()
artifact_id = f"artifact-{digest}"
filename = f"{digest}.ppm"
~~~

相同内容得到相同 ID 和文件名。优点：

- 重试天然去重。
- 文件名不依赖用户上传名称。
- 可重新计算 hash 检测篡改。
- 后期迁移对象存储时 key 稳定。

SHA-256 在这里用于完整性和内容寻址，不用于证明图片来源真实。攻击者若能同时替换图片和数据库 hash，仍可伪造，因此数据库和目录权限、审计日志、签名或可信采集链是不同安全问题。

### 10.2 为什么 Demo 用 PPM

本章不安装 Pillow 或 OpenCV，但又不应该创建扩展名是 `.jpg`、内容却是随意字节的假图片。PPM 的 P6 格式足够简单，可以用标准库生成真实 4x2 像素图：

~~~python
return b"P6\n4 2\n255\n" + pixels
~~~

第九章 C++ 运行时会产生真实摄像头帧，第十章再决定传裁剪图、路径还是按需字节。

### 10.3 只保存相对路径

Observation.image_path 保存：

~~~text
artifacts/session-ch06-001/<sha256>.ppm
~~~

不保存机器绝对路径。绝对路径会把开发机用户名、盘符和部署目录写入业务记录，迁移后也立即失效。

读取时将相对路径与 storage root 组合，再用解析后的父子关系检查它仍位于 artifacts 下。仅检查字符串是否包含 `..` 不够，因为不同平台分隔符、符号链接和规范化都可能影响最终路径。

### 10.4 完整性检查

`verify_artifacts()` 对每项记录检查：

1. 路径解析后没有逃出 artifact root。
2. 文件存在且是普通文件。
3. 实际字节数等于 byte_size。
4. 重新计算 SHA-256 等于记录值。

测试在合法 PPM 后追加 `tampered`，必须抛出 `ArtifactIntegrityError`。

### 10.5 数据库不保存 BLOB

测试会读取 `PRAGMA table_info(artifacts)`，确认没有 BLOB 列。数据库只保存 metadata。未来不应仅凭这一测试断言“所有图片都没进数据库”，但它把本章 schema 的意图固定下来。

---

## 11. SQLite 使用细节

### 11.1 一个操作一个短连接

EvidenceStore 每次操作打开连接，设置 row factory、foreign keys 和 busy timeout；成功 commit，异常 rollback，最终显式 close。

~~~python
try:
    yield connection
    connection.commit()
except Exception:
    connection.rollback()
    raise
finally:
    connection.close()
~~~

这不是高并发服务的连接池，但对教学 CLI 容易观察，也避免长时间占用 Windows 文件句柄。

### 11.2 `with sqlite3.connect()` 不会关闭连接

第一轮测试在 Windows 暴露了真实问题：测试辅助函数写成 `with sqlite3.connect(path) as connection`，用例结束删除临时目录时报 WinError 32。

Python 官方文档明确说明：Connection context manager 负责事务 commit/rollback，不会关闭连接；需要手动 `close()` 或使用 `contextlib.closing()`。[Python sqlite3 Context Manager](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)

修正后测试使用：

~~~python
with closing(sqlite3.connect(database)) as connection:
    ...
~~~

这类问题在 Linux 上有时因允许删除已打开文件而不立即暴露，Windows 更早把资源生命周期错误指出来。

### 11.3 WAL 的作用与限制

Evidence DB 初始化时执行：

~~~sql
PRAGMA journal_mode = WAL;
~~~

WAL 允许读者和写者更平稳地并行，但仍只有一个 writer。SQLite 官方文档还指出 WAL 不适合网络文件系统，`-wal` 与 `-shm` 文件也是数据库状态的一部分，不应在数据库打开时随意复制或删除。[SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)

因此：

- 本章数据库位于本机磁盘。
- `busy_timeout=5000` 只让短暂锁竞争等待，不解决业务并发语义。
- 备份不能只在运行中随手复制主 `.sqlite3` 文件。
- 第七章仍需 thread 级并发控制。

### 11.4 参数化 SQL

所有外部值通过 `?` 参数绑定，不通过字符串拼接进入 SQL。代码中 f-string 只用于内部固定表名或列名，不接收用户自定义 SQL 标识符。

### 11.5 schema_version

manifest 与 Pydantic 记录都有 `schema_version=1`，SQLite 还设置 `PRAGMA user_version=1`。第八章正式工程化时应增加迁移脚本，而不是继续在 `CREATE TABLE IF NOT EXISTS` 中无限追加字段。

---

## 12. 五进程 Demo 如何证明恢复

### 12.1 为什么不是两个函数调用

如果 `start()` 与 `resume()` 在同一解释器中运行，即使重新构建 graph，也可能无意复用全局变量、模块缓存或未关闭连接。第六章用 `subprocess.run()` 启动五个独立解释器：

| 进程 | 命令 | 从磁盘读到的状态 | 写入结果 |
|---|---|---|---|
| 1 | start | 无旧 checkpoint | 暂停 BACK_LABEL |
| 2 | resume-label | 当前标签 interrupt | 接纳 label，暂停 PORT_CLOSEUP |
| 3 | resume-port | 当前接口 interrupt | 接纳 port，暂停 USER_INPUT |
| 4 | resume-user | 当前用户 interrupt | 接纳 laptop_model，到 RUN_RULES |
| 5 | inspect | 完成态 checkpoint | 对账并验证两张图片 |

每个阶段结束都会关闭数据库连接并退出。后一个阶段只能通过文件恢复。

### 12.2 start

`phase_start()`：

1. 创建 manifest 和 Evidence Store schema。
2. 构造第五章 initial state，其中已有功率与协议 Evidence。
3. 用 SqliteSaver 首次 invoke。
4. 图暂停在 back_label。
5. 把 BeliefState 初始 ledger 对账到业务库。

此时表计数：

~~~json
{
  "artifacts": 0,
  "observations": 0,
  "evidence_records": 2,
  "evidence_acceptances": 2
}
~~~

### 12.3 resume-label 与 resume-port

每个恢复进程先调用 `graph.get_state(config)`，从 `snapshot.tasks[].interrupts` 读取当前 ID。它不依赖上一个进程把 ID 放在内存变量中。

然后：

1. 验证当前 ViewType 与 required_features。
2. 生成并保存 PPM。
3. 构造引用真实相对路径的 Observation。
4. 构造 source_id 等于 observation_id 的 Evidence。
5. 记录候选。
6. 调用第五章安全恢复入口。
7. 对账新的 BeliefState。

标签恢复后 Evidence 数为 3；接口恢复后为 4。

### 12.4 resume-user

用户恢复没有图片。第五章 wait 节点会把 UserResume 转成 laptop_model Evidence。图结束后 reconcile 从 BeliefState 发现这条新 Evidence，将它写入 evidence_records 和 acceptance。

最终 route 是 `run_rules`，SessionStatus 仍是 `running`，不是 `completed`。这是有意的：第六章只准备了 RUN_RULES Action，第十二章才实现兼容规则，第十三章才让主图执行规则并生成最终答案。现在提前标 completed 会谎报业务任务已经完成。

### 12.5 inspect

最后一个独立进程：

- 重新读取 final checkpoint。
- 对账 acceptance。
- 验证两项 ArtifactRecord。
- 输出数据库存在性、计数和 accepted_fields。
- 明确 `long_term_knowledge_stored=false`。

---

## 13. 运行 Demo

### 13.1 安装依赖

在项目目录运行：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ch06.txt
~~~

课程目标环境仍是 Python 3.12；当前工作区实际 `.venv` 为 Python 3.13。第八章会用 uv 创建并锁定 3.12 环境，现在不能把 3.12 写成已经实机验证。

### 13.2 自动创建临时目录

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py demo
~~~

`demo` 不指定 root 时创建唯一临时目录，不覆盖旧任务。

### 13.3 关键实测输出

本次实际运行得到五个不同 PID：

~~~text
process_ids = [131184, 119016, 106924, 80708, 86628]
distinct_process_count = 5
~~~

状态演进：

~~~text
start        -> waiting_observation / back_label
resume-label -> waiting_observation / port_closeup
resume-port  -> waiting_user / laptop_model
resume-user  -> run_rules / no interrupt
inspect      -> run_rules / 2 artifacts verified
~~~

最终摘要：

~~~json
{
  "missing_fields": [],
  "accepted_fields": [
    "charger_label",
    "charger_max_power_w",
    "charger_port_type",
    "charger_protocol",
    "laptop_model"
  ],
  "counts": {
    "sessions": 1,
    "artifacts": 2,
    "observations": 2,
    "evidence_records": 5,
    "evidence_acceptances": 5
  },
  "verified_artifacts": 2,
  "repaired_acceptances": 0,
  "long_term_knowledge_stored": false
}
~~~

不同 PID 本身不是全部证明；还要结合以下事实：

- 每个子命令是独立 `subprocess`。
- SQLite 连接在阶段结束显式关闭。
- 后续阶段从 snapshot 读取当前 interrupt。
- interrupt ID 随每次暂停变化。
- missing_fields 按证据逐步减少。
- 最终 checkpoint 和业务 ledger 一致。

### 13.4 手工分阶段运行

为了亲自观察进程退出，可指定目录逐条执行：

~~~powershell
$root = ".demo\ch06-manual"
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py start --root $root
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py resume-label --root $root
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py resume-port --root $root
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py resume-user --root $root
.\.venv\Scripts\python.exe examples\ch06_persistent_storage.py inspect --root $root
~~~

每条命令结束后解释器退出。可以在任意两步之间重新打开终端，再执行下一条。

---

## 14. 自动化测试

### 14.1 运行第六章测试

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_ch06*.py" -v
~~~

第六章共 9 项测试：

1. 五个独立进程恢复一个 thread 到 RUN_RULES。
2. 三个 interrupt ID 不同且缺口逐步减少。
3. 同 Evidence 幂等、同 ID 异内容冲突。
4. 删除 acceptance 后 inspect 自动修复。
5. start 拒绝覆盖 session。
6. 两项图片是有效 PPM，数据库无 BLOB 列。
7. 相同图片内容寻址去重。
8. 修改图片后 SHA-256 检查失败。
9. checkpoint 与 evidence 数据库 schema 分离且无 knowledge 表。

专项实测：**9/9 通过**。

### 14.2 为什么测试会故意删 acceptance

测试不是鼓励业务代码删除不可变审计关系，而是在可控环境模拟崩溃窗口。它先让 checkpoint 和账本完整，再直接删除一项 acceptance；下一次 inspect 必须从 checkpoint 恢复它。

### 14.3 为什么测试检查 Windows 文件清理

临时目录在 tearDown 删除。若任何 SQLite 连接忘记 close，Windows 会阻止删除数据库。本章第一轮测试确实因此失败，修复后才能通过。这让“资源释放”成为可验证行为，不只是一句代码风格建议。

---

## 15. 技术栈地图

| 技术 | 是什么 | 本章为什么用 | 不承担什么 | 后续演进 |
|---|---|---|---|---|
| SqliteSaver | LangGraph 本地持久 checkpointer | 跨进程恢复 thread/interrupt | 业务证据查询 | 生产可换 Postgres saver |
| sqlite3 | Python 标准库数据库接口 | Evidence Ledger、事务、外键 | Agent 规划、图调度 | 第 8 章仓储模块化 |
| Pydantic v2 | 运行时契约校验 | manifest、artifact、领域对象 | 文件持久性 | 持续复用 |
| SHA-256 | 内容摘要 | 去重和篡改检测 | 来源真实性、加密 | 对象存储 key/校验 |
| pathlib | 跨平台路径 API | Windows 与其他平台一致布局 | 路径授权本身 | 第 9～11 章复用 |
| os.replace | 原子替换文件名 | 避免读到半写 manifest/产物 | 断电级 fsync 保证 | 可加耐久写策略 |
| subprocess | 创建独立解释器 | 证明真正跨进程恢复 | 生产进程管理 | 第 14 章由 API 服务替代 |
| SQLite WAL | 写前日志模式 | 本机读写协作 | 多 writer、网络文件系统 | 高并发时迁移服务型 DB |

### 15.1 与 2026 企业工程的关系

企业级不是框架数量，而是能清楚回答：

- 数据由谁拥有？
- 失败发生在哪个提交边界？
- 重试是否幂等？
- 记录是否可审计？
- 大文件是否与结构化状态分离？
- 本地实现如何替换成生产实现？

本章使用轻量 SQLite，但保留了企业系统常见的接口与思想：checkpointer abstraction、append-only ledger、content-addressed artifact、idempotent reconciliation、schema version 和故障注入测试。

---

## 16. 能力边界与错误做法

### 16.1 本章已经完成

- Python 进程退出后恢复同一 LangGraph thread。
- 三次 interrupt 跨进程推进。
- 证据候选与接纳关系分离。
- checkpoint 到 Evidence Ledger 的幂等对账。
- 图片文件内容寻址和完整性验证。
- 存储类别与 schema 职责分离。

### 16.2 本章没有完成

- 多用户并发写同一 thread。
- SQLite 数据库加密和密钥管理。
- 自动备份、保留期、删除和 GDPR/隐私流程。
- 对象存储、CDN 或跨机器共享。
- C++ 摄像头真实图片。
- OCR/多模态 Evidence 提取。
- 长期设备资料库。
- USB-C 确定性规则执行。
- FastAPI 身份认证与授权。

### 16.3 常见错误做法

**错误一：把 JPEG bytes 放进 GraphState。**

每个 super-step 可能保存完整状态，checkpoint 迅速膨胀，序列化和恢复都变慢。

**错误二：把 checkpoint 数据库当业务数据库。**

框架升级可能改变内部 schema，业务报表与审计被实现细节绑定。

**错误三：看到 Evidence candidate 就直接判兼容。**

候选可能尚未通过 request、target、view 和来源关联校验。

**错误四：用同一个 evidence_id 覆盖新值。**

历史被改写，无法解释之前的决策依据。新事实应产生新 ID；冲突由 BeliefState 显式表达。

**错误五：认为 WAL 等于并发治理。**

WAL 改善数据库读写协作，不决定两个 API 请求谁有权消费同一个 interrupt。

**错误六：`with sqlite3.connect()` 后认为文件已关闭。**

上下文管理器只处理事务；连接仍需显式关闭。

**错误七：把本次 OCR 结果写进长期知识。**

会话 Evidence 与可复用规格资料的来源、审核和生命周期完全不同。

---

## 17. 课后练习

### 练习 1：观察磁盘演进

手工执行五个子命令，每一步记录：

- checkpoints.sqlite3 大小。
- evidence_records 数量。
- evidence_acceptances 数量。
- artifacts 文件数量。
- 当前 interrupt ID。

解释为什么标签恢复后四项不是同时增加相同数量。

### 练习 2：制造未接纳候选

在测试中构造 request_id 错误的 ObservationResume：

1. 先记录 Observation 和 Evidence candidate。
2. 调用安全恢复入口并确认失败。
3. 查询 evidence_records 与 evidence_acceptances。

预期：candidate 存在，acceptance 不存在，checkpoint 仍等待原 interrupt。

### 练习 3：内容寻址

两次写入相同 PPM，确认 artifact_id 与 relative_path 相同、表计数仍为 1；修改一个像素后再次写入，确认产生新 SHA-256 和新文件。

### 练习 4：对账故障注入

分别模拟：

- 删除一项 acceptance。
- 删除一张图片。
- 修改 Evidence payload_json。

说明哪种能自动修复，哪种应报错，为什么不能一律“自动修复”。

### 练习 5：设计保留策略

不用写代码，设计三类保留期：

- checkpoint 历史。
- Evidence Ledger。
- 原始图片产物。

考虑最终结论已生成、用户请求删除、证据发生争议三种情况。

### 练习 6：生产替换表

写一张替换表：

| 本章实现 | 生产候选 | 哪个接口保持不变 |
|---|---|---|
| SqliteSaver | PostgresSaver | BaseCheckpointSaver |
| 本地 artifacts | S3/MinIO/Azure Blob | ArtifactRecord/object key |
| sqlite EvidenceStore | Postgres repository | record/reconcile/query |

重点不是选云厂商，而是说明迁移时哪些领域契约不应改变。

---

## 18. 本章小结

第六章把第五章的“同进程可恢复”升级为“进程退出后仍可恢复”。真正的关键不是把 `InMemorySaver` 换成 `SqliteSaver` 这一行，而是建立了四条边界：

1. checkpoint 保存执行位置和图认知，不作为业务查询 API。
2. Evidence Ledger 保存候选事实与接纳关系，不直接控制图节点。
3. Artifact Store 保存图片字节，数据库只保存受控相对路径和完整性元数据。
4. 长期知识不混入 session，留给有来源和版本治理的资料系统。

两个数据库之间无法一次原子提交，因此本章没有假装不存在故障窗口，而是使用不可变候选、接纳关系和 checkpoint 对账，让每种中间状态都可以解释和恢复。

最终 Demo 由五个不同 PID 完成同一 thread：背面标签、接口特写、用户型号依次恢复，五条 Evidence 全部被接纳，两项图片通过 SHA-256 检查，长期知识仍为空。这个结果证明了本章存储闭环。

---

## 19. 到第 7 章的桥接

持久化解决“系统记得”，却没有解决“系统是否受控”。现在需要面对新的问题：

- 两个请求同时恢复同一 interrupt 怎么办？
- 一次任务最多观察多少次、调用多少工具？
- C++ 服务超时后重试几次？
- 用户取消时如何停止后续工作？
- 每个 RunEvent 是否包含 correlation ID 和耗时？
- 候选证据长期未接纳如何处理？
- SQLite busy 或磁盘写入失败如何分类？

因此第七章进入“中间件与系统治理”：在不改变本章领域对象和存储边界的前提下，增加 thread 级互斥、预算、重试、超时、权限和可审计日志。第六章提供持久地基，第七章让这个地基上的执行不失控。
