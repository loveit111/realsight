# 第 7 章质量鉴定报告

## 1. 鉴定结论

**结论：逻辑闭环、技术栈说明、小 Demo、前后章节桥接四项通过，可以交付第七章；通过范围限定为“单机 SQLite、同一 session 串行治理的教学 MVP”，不能宣称已实现分布式锁、真实计费或完整安全体系。**

本章没有在正常 Demo 跑通后直接判定通过。独立审计主动检查：

1. 权限、lease、幂等、预算与 operation 的执行顺序是否可能让副作用提前发生。
2. lease 只在调用前续期时，旧 owner 是否能在租约过期后晚到提交。
3. 无序 capability 集合是否能跨进程产生稳定 policy hash。
4. timeout 是否真正取消下游协程，而不只是停止上层等待。
5. retry 是否对所有异常盲目执行，是否按真实 attempt 扣成本。
6. 外部取消是否写终态、传播 CancelledError 并释放 lease。
7. stale RUNNING 命令恢复后是否重复扣 logical budget 或执行第四次。
8. audit log 是否与状态修改共同提交，并与领域 RunEvent 保持边界。

审计过程中发现并修正三项真实问题：

- 初版只在 operation 前续租，成功提交前缺少活租约栅栏。
- policy hash 对 `frozenset` 转换后的列表顺序没有显式稳定化。
- 晚到提交测试最初使用 1ms lease，在 Windows 调度下过早失效，未进入目标故障窗口。

此外补充了 LEASE_LOST 审计：即使命令因租约丢失不能写终态，本进程观察到的治理事实仍会追加，finally 继续尝试释放自己的 lease。

---

## 2. 逻辑闭环鉴定

**结果：通过。**

最终执行链：

~~~text
GovernedCommand
-> 注册并核对不可变 GovernancePolicy
-> capability 白名单
-> 获取同 thread SQLite lease
-> command_id + request hash 幂等判定
-> 新命令原子预留 command/observation 预算
-> 每次尝试前续租并预留 attempt/cost
-> asyncio.timeout 执行 operation
-> 瞬态错误/超时：记录结果、有限重试、指数退避
-> 永久错误：立即失败
-> 外部取消：写 CANCELLED，重新传播 CancelledError
-> success/failure 前检查 live lease
-> 状态与审计在同一 IMMEDIATE 事务提交
-> finally 释放 lease
~~~

### 2.1 执行顺序是否合理

- Permission 在 lease 前，未授权命令不能占住 thread。
- Lease 在 command claim 前，同 thread 幂等/预算检查串行执行。
- Idempotency 在预算前，缓存重放不重复收费。
- Logical budget 在 operation 前；attempt/cost 在每次真实调用前。
- Retry 只在显式瞬态错误/超时且次数仍允许时发生。
- 提交结果前执行 live lease fence。
- finally 覆盖 success、failure、quota、timeout、cancel 和 conflict。

Demo/测试用 operation.calls 证明 deny/cache/busy/quota/cost 路径没有先执行 callable。

### 2.2 修正问题 A：过期 owner 晚到提交

初版流程：

~~~text
renew lease
-> operation
-> mark_succeeded
~~~

若 operation 运行超过 lease，另一 owner 可能接管，而旧 owner 仍用 commands.owner_token 提交。

修正：`reserve_attempt`、`mark_succeeded` 和 `mark_failed` 都在自身 IMMEDIATE 事务内调用 `_require_live_lease`，检查 lease 存在、owner 相同、expires_at 未过期。

测试在 attempt 已预留后等待 lease 过期，晚到 success 必须抛 LeaseLost，command 仍为 RUNNING。另一个 middleware 测试让 operation 主动使 lease 过期，确认 LEASE_LOST 进入审计且最终 lease 行清理。

### 2.3 修正问题 B：无序 policy 指纹

`allowed_capabilities` 是 frozenset。`json.dumps(sort_keys=True)` 只排序对象键，不保证集合转换成列表后的顺序。不同 Python 进程的 hash seed 可能让等价 policy 产生不同 JSON。

修正：计算 policy hash 前显式：

~~~python
payload["allowed_capabilities"] = sorted(policy.allowed_capabilities)
~~~

测试用不同输入顺序构造等价集合，canonical JSON 必须完全相同并按字典序排列 capability。

### 2.4 修正问题 C：故障测试时序

初版 late commit 测试用 1ms lease，Windows 上 claim/reserve 尚未完成就过期，测试失败在准备阶段，没有验证晚到提交。

修正：使用 50ms 完成 claim/reserve，再异步等待 70ms。业务断言没有放宽，只让故障注入稳定进入目标阶段。

### 2.5 stale command 恢复

旧 lease 过期后，新 owner 可以接管 RUNNING command：

- 不重复扣 consumed_commands/observations。
- 从持久 attempt_count + 1 继续。
- 已经耗尽 max_attempts 时直接 RetryExhausted，operation.calls=0。

这避免进程崩溃后偷偷出现第四次调用。

---

## 3. 技术栈清晰度鉴定

**结果：通过。**

| 技术 | 本章作用 | 选择理由 | 明确限制 | 后续章节 |
|---|---|---|---|---|
| Pydantic v2 | policy/command/result/audit 契约 | 严格字段与版本化 JSON | 不提供事务 | 第 8 章分包 |
| asyncio.timeout | 单 attempt 超时、向下取消 | Python 3.12 标准结构化工具 | cooperative，不强杀阻塞代码 | 第 10 章配合 gRPC deadline |
| SQLite BEGIN IMMEDIATE | 预算、claim、lease 原子写 | 本机多连接写串行 | 单 writer、非分布式 | 生产数据库事务 |
| SHA-256 request hash | 同 ID 参数一致性 | 稳定、易测试 | 不替代下游幂等 | 传入 C++/tool |
| thread lease | 同 thread 串行与崩溃过期 | 可跨事件循环/进程共享 | 无 heartbeat/fencing | 后期增强 |
| append-only audit | 解释允许/拒绝/重试/终态 | 与状态同事务 | 未防篡改、未脱敏 | 第 14 章查询 |
| capability allowlist | operation 前权限 | 确定性 least privilege | 无用户身份/RBAC | API/HITL |
| integer cost unit | 教学预算 | 可确定故障测试 | 非真实货币 | usage adapter |

正文说明 middleware 不是 Agent，并结合当前官方 [LangChain Middleware](https://docs.langchain.com/oss/python/langchain/middleware/overview)、[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)、[Python asyncio timeout](https://docs.python.org/3.12/library/asyncio-task.html#timeouts) 与 [SQLite transaction](https://www.sqlite.org/lang_transaction.html) 解释技术语义。

### 3.1 是否与 2026 技术栈接轨

通过。接轨点不是堆框架，而是：

- middleware 执行钩子。
- capability/permission before tool。
- stable idempotency key。
- retry taxonomy 与 budget reservation。
- timeout/cancellation propagation。
- lease ownership + commit fence。
- policy version/fingerprint。
- append-only operational audit。
- failure injection tests。

本章不直接使用预构建 ToolRetryMiddleware，是为了同时治理 C++ observation、规则和非 Agent 工具，并让初学者看到持久预算的完整提交顺序。第十三章可适配到正式 Agent middleware。

---

## 4. Demo 鉴定

**结果：通过。**

### 4.1 Demo 是否足够小

不需要 LLM、摄像头、gRPC 或网络。四个 service stub 分别只做：

- CountingOperation：证明是否调用。
- FlakyOperation：前两次瞬态失败。
- ControlledOperation：持有 lease 制造并发。
- HangingOperation：超过 timeout 并统计取消。

业务结果只是小 JSON，关注点集中在治理。

### 4.2 输出是否可验证

实测：

~~~text
permission_denied = PermissionDenied
flaky attempts/calls = 3/3
replay cached/calls = true/0
thread_busy operation calls = 0
hanging calls/cancellations = 3/3
observation quota calls = 0
cost budget calls = 0
active_leases = 0
~~~

最终预算：

~~~text
commands = 4/4
observations = 1/1
external_attempts = 7/10
cost_units = 6/6
~~~

命令状态：

~~~text
flaky observation = succeeded
controlled rule = succeeded
timeout tool = failed
quota observation = no command row
cost tool = failed
~~~

审计：41 条，sequence 严格递增；其中 attempt_started=7、retry_scheduled=4、attempt_timed_out=3、budget_exhausted=2、command_succeeded=2、command_failed=2。

### 4.3 为什么 41 条事件不是噪声

Demo 只打印按类型计数，不把 owner token 和每条内部细节全部展示给普通用户。完整事件留在数据库供测试/审计读取。正文已区分 GovernanceAuditEvent 与 UI RunEvent，避免把内部日志直接推给用户。

### 4.4 Python 可读性

- Demo 顶部大注释包含整体逻辑、技术栈、调用流程、边界和阅读顺序。
- 测试顶部大注释包含四层不变量和测试调用链。
- 每个 policy/command/event/exception/repository 方法有中文 docstring。
- execute 内按治理顺序编排，关键异常与 finally 有中文解释。
- 代码较长是因为第八章尚未分包；正文明确把拆分留到下一章。

---

## 5. 前后章节桥接鉴定

**结果：通过。**

### 5.1 向前桥接

- 复用第二章 ToolCall/RunEvent 分层思想，中间件不伪装成 Agent。
- 复用第三章 timeout、Task、Event 和取消传播。
- 复用第四章 `session_id == thread_id` 与 Pydantic StrictContract。
- 处理第五章预检到 invoke 之间的同 thread 并发风险。
- 沿用第六章 SQLite、append-only、canonical JSON、幂等和资源边界。
- governance.sqlite3 与 checkpoint/evidence/artifact 分工明确。

### 5.2 向后桥接

- 第 8 章：拆分 contracts/workflow/storage/governance/services，建立 uv/CMake/CI。
- 第 9～10 章：Observe gRPC 使用 command_id、deadline 和取消。
- 第 11 章：视觉工具调用走 capability、attempt/cost 和 audit。
- 第 12 章：资料检索与规则使用不同 capability/retry 策略。
- 第 13 章：主图节点调用统一 GovernanceMiddleware。
- 第 14 章：API 将 PermissionDenied/ThreadBusy/Budget/Timeout 映射为受控状态码，暴露脱敏进度。

正文最后只规划第八章，不提前创建正式工程骨架。

---

## 6. 测试鉴定

第七章专项：**19/19 通过**。

前七章全项目回归：**120/120 通过**。

运行条件：

~~~powershell
$env:LANGGRAPH_STRICT_MSGPACK='true'
~~~

覆盖：

- permission 零调用/零预算。
- 持久 cache replay。
- 同 ID 参数冲突。
- failure replay 防副作用。
- transient retry success。
- timeout 三次取消。
- permanent error 不重试。
- observation/cost/global attempt budgets。
- thread busy。
- external cancellation cleanup。
- stale RUNNING recovery。
- lease lost late commit fence + audit。
- stale attempts exhausted。
- policy immutability/canonical unordered set。
- strict audit sequence。

所有第七章 Python 文件通过 py_compile。完整 Demo 在同一依赖环境中再次运行通过。

---

## 7. 残余风险

### R1：SQLite lease 不是分布式锁

严重度：高，范围内明确。

没有 heartbeat、单调 fencing token、多节点时钟策略或网络分区处理。旧 operation 即使不能提交本地 command，仍可能已经对外部服务产生副作用。

处理：下游使用 command_id 幂等；多机部署采用数据库/协调服务 lease 与 fencing。

### R2：下游幂等未实现

严重度：高，后续 C++/tool 接入前必须处理。

进程可能在下游成功、本地 mark_succeeded 前崩溃。恢复 RUNNING 会再次调用。

处理：第 10 章把 command_id 放入 gRPC 请求；工具服务保存最近请求结果或使用自然幂等操作。

### R3：timeout 是 cooperative

严重度：中。

协程吞掉 CancelledError、阻塞事件循环或底层 C 调用不响应时，实际耗时可超过 timeout。

处理：gRPC deadline、可取消 C++ operation、隔离阻塞工作和进程级终止策略。

### R4：权限模型只有静态 capability

严重度：真实产品中高。

没有 user/org identity、RBAC、资源所有权和 human approval。

处理：第 14 章 API 身份上下文；敏感 capability 使用第 5 章 interrupt approval。

### R5：cost unit 不是实际计费

严重度：中低。

没有 token usage、缓存折扣、供应商价格或结算补偿。

处理：模型/视觉适配器返回 usage；预留与实际消耗分开结算。

### R6：audit 未防篡改、未脱敏

严重度：中。

SQLite 文件有写权限的攻击者可改日志，错误消息也可能含内部数据。`MAX(sequence)+1` 长期性能有限。

处理：访问控制、脱敏、远程不可变日志、hash chain/签名、独立 sequence 生成。

### R7：三类数据库仍为最终一致

严重度：中。

checkpoint、Evidence Ledger、governance command 各自原子，不是全局事务。

处理：第 13 章使用 command_id/correlation_id 对账，必要时 outbox。

### R8：retry 缺少 jitter/circuit breaker

严重度：中低。

指数退避没有 cap 和随机抖动；大量任务可能同步重试。

处理：第 8/13 章配置化 retry policy，后期加入 jitter、熔断和服务级限流。

### R9：Python 3.12 尚未实机验证

严重度：中低。

当前工作区仍为 Python 3.13.13；正文引用并使用 3.12 支持的 asyncio API，但目标环境将在第八章创建后实测。

---

## 8. 最终验收表

| 验收项 | 结论 | 证据 |
|---|---|---|
| 逻辑闭环 | 通过 | permission -> lease -> idempotency -> budget -> execute -> audit -> release |
| 技术栈说明 | 通过 | 每项技术说明用途、限制和后续演进 |
| 小 Demo | 通过 | 确定性服务，无 LLM/摄像头依赖 |
| 权限执行前拒绝 | 通过 | denied calls=0、预算=0 |
| 幂等 | 通过 | 跨 Repository 缓存、参数冲突、失败不重放 |
| 并发治理 | 通过（单机） | thread busy、stale recovery、lease fence |
| 超时/取消 | 通过 | 3 calls/3 cancellations，外部 cancel 传播 |
| 预算 | 通过 | command/observation/attempt/cost 四层测试 |
| 审计 | 通过 | 状态同事务、sequence 严格、LEASE_LOST |
| 前章桥接 | 通过 | 复用 async、contract、interrupt、SQLite 设计 |
| 后章桥接 | 通过 | 明确进入第 8 章工程初始化及后续服务 |
| Python 注释 | 通过 | Demo/测试顶部大注释与关键代码中文解释 |
| 第七章测试 | 通过 | 19/19 |
| 全项目回归 | 通过 | 120/120 |
| 分布式锁/fencing | 未完成 | 明确残余风险 |
| 真实身份与计费 | 未完成 | 后续 API/adapter |
| Python 3.12 实测 | 待第 8 章 | 当前 3.13.13 |

综合判断：第七章可以交付。它让单机 RealSight 的外部操作具备明确权限、并发、幂等、预算、超时、重试、取消和审计边界，但不应把本地 SQLite lease 夸大为生产分布式治理。应停止在本章，等待用户审核后再进入第八章。
