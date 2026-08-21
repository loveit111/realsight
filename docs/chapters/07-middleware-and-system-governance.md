# 第 7 章 中间件与系统治理：让可恢复 Agent 在边界内运行

> 本章关键词：middleware、governance、least privilege、thread lease、idempotency、budget、timeout、retry、cancellation、audit log、BEGIN IMMEDIATE、fencing check

第六章解决了一个重要问题：程序退出以后，RealSight 仍能从 SQLite checkpoint 找到原 thread、当前 interrupt、Evidence Ledger 和图片产物。系统终于“记得自己运行到哪里”。

但持久化本身不会让系统更克制。如果主 Agent 因为 OCR 失败不断请求同一视角，SQLite 会忠实保存每一次循环；如果两个 API 请求同时恢复同一 interrupt，它们都可能通过读取阶段的预检；如果服务超时后无限重试，成本和等待时间会持续增长；如果模型能直接调用未授权操作，完整审计也只是记录事故经过。

因此第七章加入一个确定性的治理层：

> Agent 决定“想做什么”，治理中间件决定“是否允许做、由谁做、最多做几次、每次等多久、失败后是否重试，以及全过程如何审计”。

本章不调用真实模型，也不引入新的 Agent 框架。我们用 Python、Pydantic、asyncio 和 SQLite 实现一个可运行的小型 `GovernanceMiddleware`，把权限、同 thread 串行、命令幂等、调用预算、成本预算、超时、有限重试、取消和持久审计串成固定执行链。确定性服务替身会主动制造瞬态错误、永久错误、并发冲突和超时，验证中间件在失败路径上也能收口。

---

## 1. 本章目标、前提与产物

### 1.1 学习目标

完成本章后，你应当能够：

1. 解释 Agent 规划、工具实现和治理中间件的职责差异。
2. 说明为什么治理规则不能只写在 system prompt 中。
3. 设计 capability 白名单并在 operation 运行前执行权限检查。
4. 使用 SQLite thread lease 阻止同一 thread 的并发命令。
5. 解释 lease、mutex、database lock 和分布式锁的区别。
6. 使用 command_id 与请求 SHA-256 实现持久幂等。
7. 区分成功重放、ID 冲突、失败重放和 stale RUNNING 恢复。
8. 为逻辑命令、观察次数、外部尝试和成本单位建立不同预算。
9. 用 `BEGIN IMMEDIATE` 原子完成预算检查、扣减与命令登记。
10. 使用 `asyncio.timeout()` 限制单次 operation。
11. 正确传播 `CancelledError`，并在 finally 释放 thread lease。
12. 只重试显式瞬态错误和超时，不盲目重试所有 Exception。
13. 解释指数退避的作用与限制。
14. 使用 append-only 审计事件记录拒绝、重试、超时和终态。
15. 区分领域 RunEvent 与内部 GovernanceAuditEvent。
16. 通过底层服务调用计数证明权限、幂等和预算确实发生在执行前。
17. 说明本机 SQLite lease 为什么不能宣称为分布式锁。
18. 将本章能力桥接到第八章正式工程结构和后续 C++/Agent 主图。

### 1.2 学习前提

需要理解：

- 第二章 Action、ToolCall、ToolResult 与 RunEvent。
- 第三章 async/await、Task、timeout 和 cooperative cancellation。
- 第四章 Pydantic 正式契约与 `session_id == thread_id` MVP 约束。
- 第五章 interrupt_id、恢复预检与同 thread 竞态窗口。
- 第六章 SQLite 持久化、append-only、幂等和跨库故障窗口。

不要求掌握 OAuth、Kubernetes、Redis、分布式共识或云计费。本章的治理数据库位于单机，成本是教学整数单位，不是假装精确的真实货币账单。

### 1.3 本章产物

- `requirements-ch07.txt`，不增加新第三方依赖。
- `GovernancePolicy` 不可变策略模型。
- `GovernedCommand` 与请求 fingerprint。
- `GovernanceRepository` 持久预算、命令、lease 和 audit schema。
- `GovernanceMiddleware.execute()` 固定治理链。
- 权限、thread busy、幂等冲突、失败重放等分类异常。
- timeout + 瞬态错误有限重试。
- 取消终态与 finally 租约清理。
- 成功提交前的活租约栅栏检查。
- 可运行的确定性故障 Demo。
- 17 项第七章自动化测试。
- 独立质量鉴定报告。

新 Demo 与测试文件顶部都有中文大注释，说明整体逻辑、技术栈、调用流程和能力边界。关键模型、事务和控制流均有中文注释，适合代码基础较弱时逐段阅读。

---

## 2. 为什么“可恢复”还不够

### 2.1 无限主动观察

主动感知工作流可以在证据不足时再次请求视角。这是能力，也是风险：

~~~text
OCR 失败
-> REQUEST_VIEW(back_label)
-> 仍失败
-> REQUEST_VIEW(back_label)
-> 无限循环
~~~

第二章已有 `max_model_steps`，但现实系统还需要独立限制观察次数、外部服务尝试和成本。不能假设模型总会及时停止。

### 2.2 同一 interrupt 的并发恢复

第五章安全入口会：读取 checkpoint、校验 interrupt_id、再发送 Command。这两步之间存在时间窗口：

~~~text
请求 A 读取 interrupt X，预检通过
请求 B 读取 interrupt X，预检通过
请求 A 恢复
请求 B 也尝试恢复
~~~

interrupt_id 能发现旧响应，却不能独自完成并发串行化。第七章在更外层给整个 thread 加运行租约。

### 2.3 重试放大副作用

读一次设备资料失败后重试通常可接受；“向用户发通知”“修改库存”“控制硬件”失败后盲目重试，可能重复执行副作用。即使操作看起来只是观察，C++ 运行时也可能为每次请求分配帧、文件和计算资源。

因此重试必须同时回答：

- 哪类错误可重试？
- 最多几次？
- 每次是否收费？
- 下游是否接受稳定幂等键？
- 超时是否真正取消下游？

### 2.4 权限不能依赖模型自觉

system prompt 可以告诉模型“不要调用危险工具”，但 prompt 不是访问控制。模型输出、用户输入和工具参数都可能出错。权限必须在 callable 执行前由确定性代码检查。

### 2.5 日志不能只记录成功

真正有价值的审计问题经常是：

- 为什么这次观察没有执行？
- 是权限拒绝、thread busy，还是预算耗尽？
- timeout 是否取消了服务？
- 第几次重试后成功？
- 相同 command_id 是缓存重放还是参数冲突？

只保存最终 answer 无法回答这些问题。

---

## 3. 什么是治理中间件

### 3.1 中间件不是第二个 Agent

中间件不做开放式推理，不判断 USB-C 是否兼容，也不决定下一视角。它执行可预测规则：

~~~text
输入：GovernedCommand + async operation
输出：GovernedExecutionResult 或分类治理错误
规则：permission / lease / idempotency / budget / timeout / retry / audit
~~~

同一个 command 和同一份持久状态应得到一致治理行为。模型不能通过“解释得更有说服力”改变 max_observations。

### 3.2 为什么符合 DeepAgents 工程思路

当前 LangChain 官方 middleware 定位就是在 Agent 执行各阶段加入日志、分析、重试、fallback、early termination、rate limit 和 guardrail。[LangChain Middleware Overview](https://docs.langchain.com/oss/python/langchain/middleware/overview)

官方 built-in middleware 也提供 tool retry 和 tool call limit 等能力。[LangChain Built-in Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

DeepAgents 则强调可插拔 middleware、文件权限和对敏感操作的人机确认；权限应在 backend/tool 被调用前评估。[Deep Agents Overview](https://docs.langchain.com/oss/python/deepagents/overview)、[Deep Agents Backends and Permissions](https://docs.langchain.com/oss/python/deepagents/backends)

本章没有直接套用预构建 middleware，原因是教学目标更底层：

- RealSight 还要治理非 Agent 操作，例如 C++ 观察和确定性规则。
- 需要把预算、command idempotency 与第六章 SQLite 持久化连起来。
- 没有真实 LLM，预构建 Agent middleware 会增加无关复杂度。
- 先理解执行顺序，后续才能判断框架默认行为是否满足需求。

第十三章组装主 Agent 时，可以把这些规则做成 LangChain/LangGraph middleware 或节点包装器；领域概念不变。

### 3.3 operation 抽象

~~~python
GovernedOperation = Callable[
    [OperationAttempt],
    Awaitable[JsonValue],
]
~~~

operation 可以代表：

- 模型调用。
- C++ `Observe` gRPC 请求。
- OCR/视觉工具。
- 资料检索。
- USB-C 规则运行。

中间件不保存 callable，只保存 GovernedCommand。函数、数据库连接和 gRPC channel 仍是运行时资源。

---

## 4. 固定执行顺序

~~~mermaid
flowchart TD
    C["GovernedCommand"] --> P{"capability 允许？"}
    P -->|"否"| PD["审计 permission_denied / 拒绝"]
    P -->|"是"| L{"获取 thread lease？"}
    L -->|"否"| TB["审计 thread_busy / 拒绝"]
    L -->|"是"| I{"command_id 状态"}
    I -->|"同请求已成功"| CACHE["返回缓存，不扣预算"]
    I -->|"同 ID 不同请求"| CONFLICT["审计冲突 / 拒绝"]
    I -->|"失败终态"| PF["拒绝自动重放"]
    I -->|"新命令"| B{"预留命令/观察预算"}
    B -->|"不足"| BE["审计 budget_exhausted"]
    B -->|"允许"| A{"预留尝试/成本"}
    A -->|"不足"| FAIL["命令失败，operation 不运行"]
    A -->|"允许"| T["asyncio.timeout 执行 operation"]
    T -->|"成功"| FENCE{"活租约栅栏"}
    FENCE -->|"通过"| OK["保存结果 / command_succeeded"]
    FENCE -->|"丢失"| LOST["拒绝晚到提交"]
    T -->|"瞬态/超时"| R{"仍有重试次数？"}
    R -->|"是"| BACKOFF["审计 + 指数退避"]
    BACKOFF --> A
    R -->|"否"| FAIL
    T -->|"永久错误"| FAIL
    T -->|"外部取消"| CANCEL["command_cancelled / 传播取消"]
    CACHE --> FIN["finally 释放 lease"]
    OK --> FIN
    FAIL --> FIN
    CANCEL --> FIN
    LOST --> FIN
~~~

顺序不能随意调整：

- 权限先于 lease，避免未授权请求占住 thread。
- lease 先于 command claim，保证同 thread 串行检查。
- 幂等先于预算，缓存重放不能重复收费。
- 逻辑命令预算先于外部尝试预算。
- 每次重试都重新预留 attempt/cost。
- 成功提交前再次检查活租约，拒绝过期 owner 的晚到结果。
- lease 释放必须放在 finally，而不是只写在成功分支。

---

## 5. GovernancePolicy

### 5.1 策略字段

~~~python
class GovernancePolicy(StrictContract):
    policy_id: Identifier
    allowed_capabilities: frozenset[Capability]
    max_commands: int
    max_observations: int
    max_external_attempts: int
    max_cost_units: int
    max_attempts_per_command: int
    timeout_ms: int
    base_backoff_ms: int
    lease_ms: int
~~~

字段分为四组：

| 组 | 字段 | 解决问题 |
|---|---|---|
| 权限 | allowed_capabilities | 哪些操作可被调用 |
| 总量 | max_commands/max_observations | 一次任务允许做多少逻辑工作 |
| 调用 | max_external_attempts/max_attempts_per_command | 重试和服务请求上限 |
| 资源 | max_cost_units/timeout/backoff/lease | 成本、等待和并发生命周期 |

### 5.2 capability 命名

Demo 使用：

~~~text
perception.observe
rules.evaluate
spec.retrieve
~~~

未授权示例是 `admin.delete`。capability 描述能力，不直接等于 Python 函数名。未来可以让多个实现共享 `spec.retrieve`，也可以把高风险操作拆成更细权限。

### 5.3 策略不可中途改变

Repository 保存 canonical policy JSON 与 SHA-256。`allowed_capabilities` 是无序集合，计算 hash 前必须显式排序；只排序 JSON 字典键仍不能稳定集合转成列表后的顺序。相同 session 再登记时必须具有同一 thread 和同一 policy hash。

为什么不允许运行中把 `max_observations=1` 改成 100？

- 已有预算消费失去解释基准。
- 审计无法知道某次操作当时适用哪份规则。
- 并发调用可能看到不同策略。

正式系统可以创建 policy version 和显式迁移事件，但不能无痕覆盖。

### 5.4 lease 参数校验

`lease_ms` 必须长于一次 timeout + 基础 backoff。它只是最低合理性校验，不是完整租约公式。重试循环每次尝试前会续租；生产长任务还需要 heartbeat。

---

## 6. GovernedCommand 与幂等

### 6.1 稳定命令内容

~~~python
class GovernedCommand(StrictContract):
    command_id: Identifier
    session_id: Identifier
    thread_id: Identifier
    capability: Capability
    category: CommandCategory
    payload: dict[str, JsonValue]
    cost_units_per_attempt: int
~~~

它不包含 callable。相同命令可以在进程重启后由新代码重新找到 operation 实现。

### 6.2 command_id 与 request hash

只校验 command_id 不够：调用者可能错误复用 ID，却换了 payload。

~~~python
serialized = canonical_json(command.model_dump(mode="json"))
request_hash = sha256(serialized)
~~~

四种情况：

| 持久状态 | 新请求 | 行为 |
|---|---|---|
| 不存在 | 任意合法请求 | claim 新命令并扣逻辑预算 |
| SUCCEEDED | hash 相同 | 返回 result_json，cached=True |
| SUCCEEDED/RUNNING | hash 不同 | IdempotencyConflict |
| FAILED/CANCELLED | hash 相同 | PreviouslyFailed，不自动重做 |

### 6.3 为什么失败不自动重放

一个失败命令可能已经对下游产生部分副作用。例如发送请求后网络断开，调用者不知道服务是否执行。用同 command_id 自动重做并不一定安全。

本章要求：

- 命令内部的有限重试由同一次 execute 控制。
- 命令进入 FAILED/CANCELLED 后，同 ID 只返回失败语义。
- 用户明确重试应创建新 command_id，并在业务层关联原命令。

### 6.4 持久缓存不在 Python 对象里

测试第一次用 Repository A 执行成功，再创建 Repository B 和 Middleware B，重放相同命令。第二个 operation 的 calls 保持 0，预算快照不变。这证明缓存来自 governance.sqlite3，不是实例字段。

### 6.5 下游幂等仍然必要

如果进程在“下游已经执行”与“本地 mark_succeeded”之间崩溃，命令仍是 RUNNING。lease 过期后新 owner 会恢复并可能再次调用 operation。

所以真实有副作用服务必须接收 `command_id`：

~~~text
RealSight command_id
-> gRPC metadata / tool request idempotency_key
-> 下游按同 key 返回原结果或拒绝重复副作用
~~~

本地幂等表不能跨越网络替代下游幂等。

---

## 7. 同一 thread 的运行租约

### 7.1 为什么不是 `asyncio.Lock`

`asyncio.Lock` 只能协调同一个事件循环中的 Task。第六章已经允许不同 Python 进程恢复同一个 checkpoint，因此锁也必须至少落到进程共享的本机存储。

`thread_leases` 保存：

~~~text
thread_id
session_id
owner_token
expires_at
acquired_at
~~~

owner token 包含 PID 和 UUID，只标识本次执行，不作为用户身份。

### 7.2 BEGIN IMMEDIATE

SQLite 官方文档说明，同一时间只有一个写事务；`BEGIN IMMEDIATE` 会立即尝试开始写事务，已有其他 writer 时可能得到 SQLITE_BUSY。[SQLite Transactions](https://www.sqlite.org/lang_transaction.html)

本章所有“读后决定再写”的治理操作都放在 IMMEDIATE 事务中，例如：

~~~text
读取当前 lease
-> 判断是否过期
-> 插入/接管 lease
-> 追加 LEASE_ACQUIRED 或 THREAD_BUSY
-> COMMIT
~~~

这避免两个连接都先读到“无 lease”，再同时认为自己成功。

### 7.3 过期接管

进程崩溃时无法执行 finally。若 lease 永不失效，thread 会永久锁死。因此 expires_at 到期后，新 owner 可以接管。

若同 command_id 仍为 RUNNING：

- 不再次扣 logical command/observation 预算。
- 从持久 attempt_count 的下一次继续。
- 若已达到 max_attempts，直接失败，不偷偷执行第四次。
- 追加 STALE_COMMAND_RECOVERED。

### 7.4 为什么提交成功前再检查 lease

初版设计只在 operation 前续租。独立审计发现一个漏洞：

~~~text
owner A 获得 lease
-> operation 运行太久，lease 过期
-> owner B 接管 thread
-> A 晚到并提交 success
~~~

修正后 `mark_succeeded`、`mark_failed` 和 `reserve_attempt` 在自己的 IMMEDIATE 事务中验证：

- lease 行存在。
- owner_token 相同。
- expires_at 仍晚于当前时间。

失败抛 `LeaseLost`，晚到 owner 不得提交命令终态。

### 7.5 这仍不是完整 fencing token

本章检查能保护本地 commands 表，却不能撤销外部服务已经发生的副作用。真正分布式系统常使用单调递增 fencing token，让下游拒绝旧 token 请求；还需要 heartbeat、时钟策略和高可用锁服务。

SQLite lease 的准确定位是：

> 单机课程 MVP 的 thread 串行化与崩溃解锁机制，不是跨机器分布式锁。

---

## 8. 四类预算

### 8.1 max_commands

一个新的 command_id 成功 claim 时消费一次。缓存重放、权限拒绝、thread busy 和 claim 前预算拒绝都不消费。

它防止 Agent 无限创建不同工具调用。

### 8.2 max_observations

category 为 OBSERVATION 的新命令额外消费一次。它比 max_commands 更贴近主动感知成本：一次观察可能包含用户动作、摄像头等待、关键帧和视觉模型。

Demo 上限为 1。背面标签观察成功后，接口观察命令在 claim 阶段被拒绝，operation.calls 为 0，commands 表中也没有该命令。

实际 RealSight 最终需要接口观察，所以真实 policy 不会设成 1；这里故意设小是为了演示预算边界。

### 8.3 max_external_attempts

每次真正准备调用外部 operation 前消费一次，包括重试。第一次调用失败再重试，不会只记作“一条命令”。

测试把全局 attempts 上限设为 1：瞬态错误第一次发生后虽满足每命令 retry 条件，但第二次 reserve_attempt 因全局预算不足而失败，底层 calls 最终为 1。

### 8.4 max_cost_units

每次 attempt 消费 `cost_units_per_attempt`。

成本单位可以表示：

- 模型 token 档位的粗粒度权重。
- 视觉模型一次推理单位。
- 网络检索调用单位。
- GPU/CPU 预算权重。

它不是货币。真实价格会随模型、缓存、批处理和供应商变化，后续应由计费适配器返回实际 usage，再与预留值结算。

### 8.5 预算扣减语义

Demo 的 costly command 发生：

~~~text
逻辑命令仍有额度
-> command claim 成功，consumed_commands + 1
-> reserve_attempt 发现成本已满
-> operation 不运行
-> command 进入 FAILED
~~~

因此“被 claim 但没调用外部服务”也可以消耗 command slot，因为系统确实接受并处理了一条新命令。attempt/cost 没有消费。

### 8.6 为什么必须原子预留

错误做法：

~~~python
if consumed < limit:
    await operation()
    consumed += 1
~~~

两个并发请求都可能看到旧值。正确顺序是用数据库事务先预留，再执行。外部调用失败时通常不退回 attempt/cost，因为资源已经实际消耗。

---

## 9. 超时与取消

### 9.1 单次尝试超时

~~~python
async with asyncio.timeout(policy.timeout_ms / 1000.0):
    result = await operation(attempt)
~~~

Python 官方文档说明，`asyncio.timeout()` 超时时会取消当前受控任务，并在上下文外转换为 `TimeoutError`。[Python 3.12 asyncio Timeouts](https://docs.python.org/3.12/library/asyncio-task.html#timeouts)

中间件捕获 TimeoutError，记录 ATTEMPT_TIMED_OUT，再决定是否重试。

### 9.2 timeout 不是精确墙钟截止

超时会发出取消并等待协程清理。若 operation 在 C 扩展中阻塞、吞掉 CancelledError，或者清理很慢，总耗时可能超过 timeout_ms。

因此 timeout 是协作式控制，不是强制杀死任意代码。C++ gRPC 调用还应设置自己的 deadline 和 cancellation；Python timeout 只是一层上限。

### 9.3 外部取消

用户取消 API Task 时，CancelledError 可能发生在：

- operation 内。
- retry backoff 的 sleep 中。
- 其他 await 点。

execute 的外层捕获它：

1. 将活动命令标记 CANCELLED。
2. 追加 COMMAND_CANCELLED。
3. 重新 raise CancelledError。
4. finally 释放 lease。

不能把取消转换成普通空结果，否则上层会误以为任务正常完成。

### 9.4 Demo 如何验证向下取消

HangingOperation：

~~~python
try:
    await asyncio.sleep(1.0)
except asyncio.CancelledError:
    self.cancellations += 1
    raise
~~~

timeout 为 30ms，三次尝试后：

~~~text
hanging.calls = 3
hanging.cancellations = 3
command_status = failed
active_leases = 0
~~~

这比只断言 RetryExhausted 更强：它证明下游协程确实收到三次取消。

---

## 10. 重试策略

### 10.1 可重试错误白名单

本章只自动重试：

- `TransientOperationError`。
- `TimeoutError`。

普通 ValueError、Pydantic ValidationError、PermissionDenied、IdempotencyConflict 和业务规则错误不重试。

### 10.2 为什么不能 `except Exception: retry`

以下错误重试通常无意义：

- request_id 不匹配。
- capability 未授权。
- USB-C 规则输入冲突。
- 数据 schema 不合法。
- 文件路径越界。

盲目重试只会重复错误和成本，还可能掩盖程序 bug。

### 10.3 每命令次数上限

`max_attempts_per_command=3` 包含第一次调用，不是“第一次 + 再重试三次”。

FlakyOperation 前两次抛瞬态错误，第三次成功：

~~~text
attempt 1 -> transient -> retry
attempt 2 -> transient -> retry
attempt 3 -> success
~~~

HangingOperation 三次 timeout 后进入 RetryExhausted，不存在第四次。

### 10.4 指数退避

~~~python
backoff_ms = base_backoff_ms * 2 ** (attempt_number - 1)
~~~

退避给瞬态服务恢复时间，减少立即重试造成的压力。Demo 为加快测试使用 2ms 基础值；生产通常还会加入：

- 上限 cap。
- 随机 jitter，避免大量客户端同时重试。
- 服务端 Retry-After。
- 按错误类型使用不同策略。

### 10.5 retry 计划与 attempt 开始分开审计

ATTEMPT_FAILED 表示本次已失败；RETRY_SCHEDULED 表示治理层决定稍后再试；下一条 ATTEMPT_STARTED 才表示预算预留成功并真正开始。

若全局 attempt budget 在两者之间耗尽，会看到 retry planned 但没有下一次 operation。这是可解释状态。

---

## 11. 持久审计日志

### 11.1 记录哪些事件

AuditEventType 包括：

- permission_denied。
- lease_acquired/thread_busy/lease_lost/lease_released。
- command_claimed/stale_command_recovered。
- idempotency_replayed/conflict/previous_failure_replayed。
- budget_exhausted。
- attempt_started/failed/timed_out。
- retry_scheduled。
- command_succeeded/failed/cancelled。

### 11.2 状态修改与审计共同提交

例如 reserve_attempt 在一个 IMMEDIATE 事务中：

~~~text
检查剩余 attempt/cost
-> 扣减 session 预算
-> command.attempt_count + 1
-> 插入 ATTEMPT_STARTED
-> COMMIT
~~~

不会出现预算已扣但没有对应开始事件，或事件显示开始但预算没扣。

### 11.3 严格 sequence

每个 session 的事件 sequence 从 1 严格递增。当前实现用 `MAX(sequence)+1`，并依赖 IMMEDIATE 写事务串行化。

它适合小型教学审计；长日志下应改为专门 sequence row、数据库 sequence 或事件存储，避免每次扫描最大值。

### 11.4 GovernanceAuditEvent 与 RunEvent

| 类型 | 面向谁 | 内容示例 | 是否展示给普通用户 |
|---|---|---|---|
| RunEvent | 工作流/UI | 请求观察、证据更新、最终回答 | 通常展示摘要 |
| GovernanceAuditEvent | 运维/安全/调试 | permission denied、预算、owner、重试 | 默认不完整暴露 |

两者可以通过 session_id、thread_id、command_id/correlation_id 关联，但不能直接合并成一张“万能事件表”。治理日志可能包含内部策略和错误细节，API 要做脱敏和权限控制。

---

## 12. governance.sqlite3 schema

### 12.1 governance_sessions

保存不可变 policy、上限与已消费预算。

### 12.2 thread_leases

每个 thread 最多一行。租约成功获取、接管或释放都写审计。

### 12.3 commands

保存：

- request_hash。
- capability/category。
- RUNNING/SUCCEEDED/FAILED/CANCELLED。
- owner_token。
- attempt_count。
- result_json 或分类错误。

命令终态不可被相同 ID 自动改写。

### 12.4 audit_events

append-only 事件表，按 session + sequence 唯一。

### 12.5 为什么单独一个数据库

第六章已有：

- checkpoints.sqlite3：LangGraph 执行位置。
- evidence.sqlite3：Observation/Evidence/Artifact 元数据。
- artifacts：图片字节。

第七章新增 governance.sqlite3，保存运行策略、预算、命令和内部审计。这样：

- Evidence 不会因预算变化被改写。
- LangGraph checkpointer 不负责权限。
- 运维可以独立查询治理状态。
- 第八章可将 repository 分模块后，再决定生产是否合并到同一 Postgres 实例的不同 schema。

分数据库也带来跨库一致性问题。本章命令和治理日志自身原子，但它与第六章 checkpoint/Evidence 仍是最终一致。第十三章整合时要用 command_id/correlation_id 做对账。

---

## 13. Demo 场景

### 13.1 策略

~~~text
allowed = perception.observe, rules.evaluate, spec.retrieve
max_commands = 4
max_observations = 1
max_external_attempts = 10
max_cost_units = 6
max_attempts_per_command = 3
timeout_ms = 30
~~~

这些值为了快速触发边界，不是最终 USB-C 产品配置。

### 13.2 权限拒绝

`admin.delete` 不在白名单：

~~~text
denied.calls = 0
consumed_commands = 0（此时）
audit = permission_denied
~~~

### 13.3 瞬态观察与缓存重放

FlakyOperation：

~~~text
calls = 3
attempts = 3
result.cached = false
~~~

相同 command 重放：

~~~text
replay_operation.calls = 0
result.cached = true
result.value = 原成功结果
~~~

### 13.4 thread busy

ControlledOperation 取得 lease 后等待 Event。第二命令在第一条释放前执行：

~~~text
busy result = ThreadBusy
busy_operation.calls = 0
~~~

放行 Event 后第一条成功，finally 释放 lease。

### 13.5 timeout

HangingOperation 三次超过 30ms：

~~~text
calls = 3
cancellations = 3
status = failed
error = RetryExhausted
~~~

### 13.6 观察预算与成本预算

第二个 observation：

~~~text
BudgetExceeded("observation budget exhausted")
operation.calls = 0
command row = 不存在
~~~

成本命令：

~~~text
command claim 成功
reserve_attempt 发现 cost 6/6
operation.calls = 0
command status = failed
~~~

---

## 14. 运行 Demo

### 14.1 依赖

第七章没有增加新第三方库：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ch07.txt
~~~

### 14.2 命令

~~~powershell
.\.venv\Scripts\python.exe examples\ch07_governance_middleware.py demo
~~~

也可指定不覆盖旧会话的目录：

~~~powershell
.\.venv\Scripts\python.exe examples\ch07_governance_middleware.py demo --root .demo\ch07
~~~

### 14.3 关键实测输出

~~~text
permission_denied = PermissionDenied
flaky attempts = 3
replay cached = true
thread_busy = ThreadBusy
timeout_result = RetryExhausted
observation_quota = observation budget exhausted
cost_budget = cost budget exhausted
~~~

底层 operation 计数：

~~~json
{
  "denied": 0,
  "flaky": 3,
  "replay": 0,
  "controlled": 1,
  "busy": 0,
  "hanging": 3,
  "hanging_cancellations": 3,
  "observation_quota": 0,
  "cost_budget": 0
}
~~~

最终预算：

~~~json
{
  "consumed_commands": 4,
  "consumed_observations": 1,
  "consumed_external_attempts": 7,
  "consumed_cost_units": 6
}
~~~

其他关键值：

~~~text
command_count = 4
active_leases = 0
audit_event_count = 41
audit_sequence_is_strict = true
~~~

`active_leases=0` 是重要验收，不是装饰信息。它证明成功、失败、quota 拒绝和 timeout 后没有把 thread 永久锁住。

---

## 15. 自动化测试

### 15.1 运行

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_ch07*.py" -v
~~~

第七章 17 项测试覆盖：

1. 权限拒绝零调用、零预算。
2. 成功结果跨 Repository 实例缓存重放。
3. 同 command_id 改 payload 冲突。
4. 失败命令不自动重复副作用。
5. 瞬态错误两次后成功。
6. 三次 timeout 均向下取消。
7. 永久 ValueError 不重试。
8. observation quota 在 claim 阶段拒绝。
9. cost budget 在 operation 前拒绝。
10. 全局 attempt budget 中断 retry loop。
11. 同 thread 第二命令得到 busy。
12. 外部取消写 CANCELLED 并释放 lease。
13. 过期 RUNNING 命令恢复时不重复扣 command 预算。
14. lease 丢失阻止晚到 success 提交。
15. 已耗尽三次的 stale command 不执行第四次。
16. middleware 在晚到结果被拒绝时追加 LEASE_LOST，并清理 lease。
17. audit sequence 严格递增且 JSON 可读。
18. session 中途修改 policy 被拒绝。
19. 无序 capability 集合在不同输入顺序下得到相同 canonical policy JSON。

专项实测：**19/19 通过**。

### 15.2 为什么测试检查 calls

只断言异常类型不能证明 operation 没运行。每个服务替身都有 calls；HangingOperation 还有 cancellations。于是测试能区分：

- “先运行后报权限错误”的错误实现。
- “缓存重放但又调用一次”的错误实现。
- “预算已经不足仍调用服务”的错误实现。
- “只停止等待但没取消协程”的错误实现。

### 15.3 故障测试如何改进实现

实现后审计主动加入“lease 在 operation 期间过期”的测试。它推动代码在 mark_succeeded 前增加 live lease fence，而不是满足于调用前续租。

测试初版把 lease 设为 1ms，Windows 调度下在 reserve_attempt 前就过期，没能验证晚到提交。随后调整为：先用 50ms 完成 claim/reserve，再异步等待 70ms，稳定进入目标窗口。这个修正的是测试时序，不是放宽业务断言。

---

## 16. 技术栈地图

| 技术 | 本章角色 | 为什么使用 | 不承担什么 | 后续演进 |
|---|---|---|---|---|
| Pydantic v2 | policy/command/result/audit 契约 | 边界校验、版本化 JSON | 并发原子性 | 第 8 章正式包 |
| asyncio.timeout | 单尝试时限与取消 | 标准库、结构化范围 | 强杀阻塞 C 代码 | 第 10 章配合 gRPC deadline |
| SQLite BEGIN IMMEDIATE | 原子预算/claim/lease | 本机跨连接写串行 | 分布式共识 | 生产换数据库事务 |
| SHA-256 command hash | 幂等请求指纹 | 防止同 ID 换参数 | 下游副作用幂等 | command_id 传到 gRPC/tool |
| thread lease | 同 thread 串行与崩溃过期 | 跨事件循环、可持久 | 完整分布式锁 | heartbeat/fencing |
| append-only audit | 解释治理决定 | 状态修改同事务记录 | 普通用户进度流 | 第 14 章受控查询 |
| capability allowlist | 最小权限 | callable 前确定性拒绝 | 用户身份认证 | API RBAC/HITL |
| cost unit | 教学资源配额 | 可确定测试调用成本 | 真实货币结算 | usage adapter |

### 16.1 与 2026 企业实践接轨的地方

- policy as data，而不是散落 if。
- deny before execute。
- stable idempotency key + request fingerprint。
- retry taxonomy，而不是 catch-all。
- timeout/cancel 向下传播。
- budget reservation before side effect。
- lease owner 与提交 fence。
- append-only audit 与领域事件分离。
- 故障注入测试验证失败路径。

这些工程习惯比“用了多少 Agent”更能决定系统能否上线维护。

---

## 17. 能力边界和残余风险

### 17.1 已完成

- 静态 capability 白名单。
- 同机 SQLite thread lease。
- 成功命令持久幂等缓存。
- ID 参数冲突与失败重放保护。
- 四类持久预算。
- 单尝试 timeout、有限 retry 和退避。
- cooperative cancellation 与 finally cleanup。
- 状态修改同事务审计。
- 过期 owner 晚到提交拒绝。

### 17.2 尚未完成

- 用户/组织身份认证和 RBAC。
- 敏感操作 human approval 策略。
- lease heartbeat 和单调 fencing token。
- 多机器分布式锁与时钟偏差处理。
- retry jitter、熔断器、服务级限流。
- 模型实际 token/cost 结算。
- audit 防篡改签名、归档与查询权限。
- checkpoint/evidence/governance 三库统一 command 对账。
- FastAPI 429/403/409/504 错误映射。

### 17.3 不要误用

**错误一：把 max_attempts 写进 prompt。**

模型可以忘记或违反；确定性执行层才是强约束。

**错误二：权限检查放在 operation 后。**

副作用已经发生，拒绝只剩日志意义。

**错误三：缓存所有失败并当成功返回。**

失败重放要保持失败语义，不能返回 `None` 伪装完成。

**错误四：timeout 后用 shield 让副作用继续。**

调用者以为超时停止，后台仍可能执行。只有明确需要独立后台任务时才设计脱离请求的生命周期。

**错误五：重试不收费。**

每次真实外部尝试都消耗资源，预算必须按 attempt 记账。

**错误六：lease 过期就证明旧进程停止。**

旧 operation 可能仍运行。需要提交 fence 和下游幂等，分布式场景还需 fencing token。

**错误七：把 SQLite busy 当成 thread busy。**

SQLite busy 是数据库锁竞争；ThreadBusy 是业务上同一 thread 已有 owner。两者错误码和处理策略不同。

---

## 18. 课后练习

### 练习 1：预算手算

给定：

~~~text
max_commands = 3
max_external_attempts = 5
max_cost_units = 8
command A cost=2，第二次成功
command B cost=3，第一次 timeout，第二次准备重试
~~~

手算每一步预算，判断 B 第二次能否开始。

### 练习 2：错误分类

将以下错误分为 retryable/non-retryable，并解释原因：

- gRPC UNAVAILABLE。
- gRPC INVALID_ARGUMENT。
- OCR 返回空文本。
- Pydantic request_id mismatch。
- SQLite database locked。
- 用户取消。

不要只按异常名称判断，还要考虑 operation 是否幂等。

### 练习 3：添加 jitter

在 `_backoff` 中注入可测试的随机源，实现 full jitter。测试不能依赖真实随机时间；应传入确定性 random provider。

### 练习 4：敏感操作审批

设计 capability `device.control_power`：

~~~text
permission allowlist 通过
-> 检查是否需要 human approval
-> interrupt 等待用户
-> approval command 恢复
-> 再进入治理 execute
~~~

说明 approval 与 permission 为什么不是同一概念。

### 练习 5：下游幂等

为未来 C++ ObserveRequest 增加 command_id，设计 C++ 端最近命令缓存：

- 同 ID 同请求返回原 Observation。
- 同 ID 异请求返回错误。
- 缓存多久？
- 图片路径被清理后如何响应？

### 练习 6：审计查询

写 SQL 回答：

- 哪些 command timeout 三次？
- 哪些请求因 permission 被拒绝？
- 哪些 session 消耗成本最快？
- 哪些 stale command 被恢复？

### 练习 7：运行取消测试

在 `ControlledOperation` 等待时 cancel Task，逐行解释：

~~~text
CancelledError 从哪里进入
-> command status 何时变 CANCELLED
-> lease 在哪里释放
-> 为什么新命令可以立即执行
~~~

---

## 19. 本章小结

第七章没有增加新的 Agent，而是在现有 Agent、工具、C++ 服务和规则之间建立一条确定性执行边界。

它完成了：

1. capability 白名单在 operation 前拒绝未授权调用。
2. SQLite thread lease 让单机同 thread 命令串行。
3. command_id + request hash 区分缓存、冲突与失败重放。
4. logical command、observation、attempt、cost 四层预算。
5. asyncio timeout、显式瞬态错误重试和 cooperative cancellation。
6. 成功提交前活租约栅栏，阻止过期 owner 晚到写入。
7. append-only audit 解释所有允许、拒绝、重试和终态。

最终 Demo 的未授权、缓存、busy、quota 和 cost operation 调用次数均为 0；flaky 服务三次成功，hanging 服务三次都收到取消；四条命令被真正 claim，七次外部尝试消耗六个成本单位，结束后 active lease 为 0，41 条审计事件 sequence 严格递增。

这些数字共同证明治理规则不只存在于文档，而是真的挡在 operation 前后。

---

## 20. 到第 8 章的桥接

前七章现在已经有大量经过验证但仍放在 examples 中的代码：

- 正式 Pydantic 领域契约。
- Protobuf 协议。
- LangGraph interrupt/resume。
- SQLite checkpoint、Evidence Store、Artifact Store。
- Governance policy、repository 和 middleware。
- 120 项回归测试。

继续在单文件示例中增长会降低可维护性。第八章进入正式工程初始化：

- 建立 C++/Python 单仓目录。
- 用 Python 3.12 + uv 创建 `pyproject.toml` 和 lockfile。
- 将 contracts、workflow、storage、governance、services 分包。
- 用配置文件/环境变量加载 policy，而不是写死 Demo 值。
- 增加 CMake、格式化、静态检查、CI 和测试数据目录。
- 保持现有稳定领域类型和测试语义，不借工程重构偷偷改行为。

第七章回答“如何让执行受控”，第八章回答“如何让这些代码成为团队可以构建、测试和持续演进的工程”。
