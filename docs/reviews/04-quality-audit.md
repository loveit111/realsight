# 第 4 章质量鉴定报告

## 1. 鉴定结论

**结论：正文、技术栈说明、Demo 与前后桥接四项通过，可以作为第四章正式教学版本交付；但保留一个明确的环境残余风险：课程目标 Python 3.12 未在当前机器实跑，本次使用项目本地 Python 3.13 `.venv` 验证。**

本报告没有根据“Demo 能跑”直接判定全部正确。初版实现后执行了对抗式审读，发现并修正三类正常路径测试未暴露的问题：

1. Protobuf 必需 scalar 缺少 presence：质量总分和采集时间缺失时会静默成为 0/Unix epoch。
2. BeliefState 可把重复 Evidence 伪装成两条冲突，支持列表也可能不含当前活跃证据。
3. GraphState 只检查 target_id，不足以证明 REQUEST_VIEW Action 与 pending_request 是同一请求。

修正后增加相应失败测试，第四章 29/29 通过，前四章全量回归 70/70 通过。

---

## 2. 逻辑闭环鉴定

**结果：通过。**

本章推导顺序为：

~~~text
前三章出现多个输入/状态/跨进程边界
-> dataclass 与自由字典不能充分校验不变量
-> 区分 Python 语义、工作流演进、跨语言传输三层契约
-> 用 Pydantic 固化八个领域类型
-> 用 RealSightGraphState 聚合跨对象约束
-> 用 LangGraph 节点和条件边演进状态
-> 用 Protobuf 固定 C++/Python wire 边界
-> 通过显式适配器重新执行业务校验
-> Demo 与失败测试验证闭环
-> 为第五章中断恢复提供稳定状态
~~~

已核对的职责边界：

- Observation 与 Evidence 没有混同。
- C++ 感知运行时仍是服务，不被包装成 Agent。
- LangGraph 节点不承担 Pydantic 字段解析。
- Protobuf 不承担 USB-C 业务规则。
- GraphState 不保存 gRPC channel、摄像头、Task 或模型客户端。
- 第四章只准备 `run_rules` Action，没有提前实现第十二章规则。
- `.proto` 只覆盖跨 C++ 边界的数据，没有复制 Python 全部业务状态。

### 已修正问题 A：Protobuf 缺失值伪装成零值

初版 `overall_score` 与 `captured_at_unix_ms` 使用普通 proto3 scalar。字段缺失时，生成 API 会返回默认 0；0 又分别是合法最低质量分和可解析时间戳，Pydantic 无法判断字段是否真的出现。

修正：

- 两个字段改为 `optional`。
- 适配器使用 `HasField` 明确检查 presence。
- 新增缺失时间戳和总分的拒绝测试。

### 已修正问题 B：伪冲突与断裂支持关系

初版只要求 conflicts 至少有两个元素，同一 Evidence 重复两次也能通过；supporting_evidence_ids 只要求引用同字段，却不要求包含当前活跃 Evidence。

修正：

- 冲突 Evidence ID 必须唯一。
- 冲突必须包含至少两个不同 JSON 值。
- supporting ID 必须唯一。
- supporting 列表必须包含当前活跃 Evidence ID。
- 新增两项失败测试。

### 已修正问题 C：请求与动作关联不完整

初版只检查 REQUEST_VIEW Action 与 State 的 target_id。两个 request_id 不同但目标相同的请求仍可能分别存在于 Action 和 pending_request 中，导致取消与结果关联错误。

修正：

- REQUEST_VIEW payload 的 session_id 必须与当前会话一致。
- Action 内请求必须等于 GraphState.pending_request。
- pending_request 必须对应一个且仅一个 REQUEST_VIEW Action。
- 三种 route 必须有对应动作。
- 新增错会话和请求错配测试。

---

## 3. 技术栈清晰度鉴定

**结果：通过。**

正文对每项技术回答了“是什么、为什么使用、在本章做什么、后续在哪章继续”：

| 技术 | 本章定位 | 使用理由 | 后续桥接 |
|---|---|---|---|
| Pydantic v2 | Python 业务契约 | 字段与聚合校验、JSON 序列化 | 第 6 章持久化、第 14 章 API |
| LangGraph | 低频任务状态编排 | 节点、条件边、未来检查点与中断 | 第 5、6、13 章 |
| Protobuf | Python/C++ wire schema | 生成跨语言类型、presence、oneof | 第 10 章 gRPC 联调 |
| grpcio-tools | 教学期协议编译 | 真实验证 `.proto` 而非只展示文本 | 第 8 章构建任务 |
| JSON | 检查点预演与调试投影 | 可读、可恢复 Pydantic 状态 | 第 6 章 SQLite |

正文明确说明：

- LangGraph 不等于 LangChain，也不要求每个图都调用模型。
- Pydantic State 的校验成本适合低频编排，不进入 C++ 逐帧路径。
- DeepAgents CompiledSubAgent 常见 `messages` 是适配接口要求，本章主领域图不机械添加。
- Protobuf 成功解析不等于业务数据可信，必须经过适配器和 Pydantic。
- 浏览器边界仍使用 HTTP/JSON/WebSocket，C++ 内部服务使用 gRPC/Protobuf。

实际验证依赖：

- pydantic 2.13.4
- protobuf 6.33.6
- grpcio-tools 1.81.1
- langgraph 1.2.10

参考资料限定为 Pydantic、LangGraph、Protobuf 官方文档，以及用户指定的深度研搜课程作为架构启发。

---

## 4. Demo 鉴定

**结果：通过。**

Demo 规模受控，不依赖 LLM、摄像头、网络服务或数据库，实际覆盖：

1. Pydantic 拒绝 `confidence=1.2`。
2. LangGraph 第一轮选择 `request_observation`。
3. ObservationRequest 经真实 protoc 生成类型并完成 bytes 往返。
4. 补齐标签 Evidence 后选择 `await_user`。
5. 补齐 laptop_model 后选择 `run_rules`。
6. GraphState 经 JSON 往返后对象相等。

关键实测输出：

~~~text
route 1 = request_observation
protobuf wire bytes = 181
request_equal = True
route 2 = await_user
route 3 = run_rules
checkpoint state_equal = True
~~~

`wire_bytes=181` 只作为本次输入的可验证输出，不被误写成性能结论。

### 代码可读性

用户要求所有生成 Python 学习文件在顶部说明逻辑、技术栈和调用流程。已检查：

- `contracts/models.py`：顶部大注释完整，验证器和复杂不变量有中文说明。
- `examples/ch04_state_and_contracts.py`：顶部大注释完整，图节点与适配器有中文说明。
- `tests/test_ch04_state_and_contracts.py`：顶部大注释完整，测试分组和测试数据有中文说明。

生成的 pb2 只存在于系统临时目录，不作为手写学习文件交付。

---

## 5. 前后章节桥接鉴定

**结果：通过。**

### 向前桥接

- 保留第一章八个领域类型的稳定名称。
- 把第一章 BeliefState 四区互斥升级为可执行聚合校验。
- 把第二章 Action/RunEvent 区分升级为判别 payload 和事件序号约束。
- 把第三章模拟 ObservationRequest 与服务事件映射到未来 proto 服务。
- 没有修改前三章源文件或覆盖用户已有学习注释。

前三章 dataclass 文件继续作为章节快照；第四章后的新代码应优先使用 Pydantic 契约。第八章统一工程目录时再迁移旧 Demo 的导入，避免此时为了“整洁”破坏学习演进记录。

### 向后桥接

- 第 5 章：TaskSession.thread_id、GraphState、route 和 Action 进入 interrupt/resume。
- 第 6 章：model_dump_json 可作为检查点序列化起点，ledger 与图片产物分开存储。
- 第 7 章：RunEvent、correlation_id、schema_version 用于治理与审计。
- 第 8 章：requirements 快照迁移到 Python 3.12 的 pyproject.toml/uv.lock，proto 生成进入构建。
- 第 10 章：realsight.proto 生成 C++/Python gRPC stub，接入真实流与取消。
- 第 13 章：主 LangGraph 编排复用本章状态，子 Agent 通过适配层返回 Evidence。
- 第 14 章：RunEvent 投影为 WebSocket 进度，Pydantic 模型用于 API 边界。

---

## 6. 测试鉴定

第四章测试：**29/29 通过**。

覆盖维度：

- 字段范围与严格类型。
- 多字段语义。
- BeliefState 聚合不变量。
- target/session/request 隔离。
- Action 判别联合。
- 三条 LangGraph 条件边。
- proto 编译与 service descriptor。
- optional presence。
- Pydantic/Protobuf 二进制往返。
- JSON 状态往返。

前四章全项目回归：**70/70 通过**，执行时间约 0.23 秒。

---

## 7. 残余风险与明确限制

### R1：Python 3.12 未在当前机器实跑

严重度：中低。

本机只有全局 Python 3.13 和 3.11/3.9 conda 环境。本章项目本地 `.venv` 使用 Python 3.13。代码没有使用 3.13 专属语法，LangGraph 当前要求 Python 3.10+，但“应当兼容”不能替代 CI 实测。

处理：第八章创建正式 Python 3.12 + uv 环境，并运行前八章全量测试。

### R2：尚无跨版本 schema breaking 检查

严重度：中低。

当前是 proto v1 初始版本，没有历史 schema。仅靠人工约定无法长期阻止字段编号复用。

处理：第八章或第十章在 CI 中加入 protoc 编译和 schema breaking 检查；删除字段时 reserve 编号与名称。

### R3：未进行 C++ wire 互操作测试

严重度：中。

本章只使用 Python 生成类型验证二进制。跨语言目标还需要 C++ 序列化/Python 解析与反向测试。

处理：第十章生成 C++ 类型后加入 golden bytes 或端到端 gRPC 集成测试。

### R4：当前图尚未持久化和真正中断

严重度：预期内。

`await_user` 当前作为图出口，JSON 往返只是可序列化性预演。

处理：第五章实现 interrupt/resume，第六章加入 SQLite checkpointer。

---

## 8. 最终验收表

| 验收项 | 结论 | 证据 |
|---|---|---|
| 逻辑是否闭环 | 通过 | 三层契约到三分支图和协议往返形成闭环 |
| 有无概念跳跃 | 通过 | 从前三章边界问题逐步引入 Pydantic、LangGraph、Protobuf |
| 职责是否冲突 | 通过 | 业务语义、状态演进、wire schema 分层明确 |
| 技术栈是否讲清 | 通过 | 说明是什么、为什么、在哪用、后续去哪章 |
| Demo 是否足够小 | 通过 | 无模型/摄像头/网络，三轮确定性路由 |
| Demo 是否真实可运行 | 通过 | 使用真实 LangGraph 与真实 protoc 编译/bytes 往返 |
| 输出是否可验证 | 通过 | 路由、181 bytes、对象相等、29 项测试 |
| 与前章是否桥接 | 通过 | 复用八类型、Action、RunEvent、异步服务边界 |
| 与后章是否桥接 | 通过 | State/thread_id/proto 进入第 5、6、10、13 章 |
| Python 文件教学注释 | 通过 | 三个手写文件均有顶部总说明和关键注释 |
| Python 3.12 实机验证 | 待第 8 章 | 当前仅在项目本地 Python 3.13 `.venv` 实跑 |

综合判断：第四章可交付并停止在本章，等待用户确认或提出修改；不提前生成第五章正文。
