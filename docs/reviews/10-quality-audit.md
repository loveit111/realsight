# 第 10 章独立质量鉴定报告

## 1. 鉴定结论

鉴定结果：**在“单摄像头、单活动观察、本机 C++/Python、教学 MVP”范围内条件通过。**

这不是生产上线结论。正文、Demo 和测试已经形成闭环，但仍保留同步 gRPC、启发式阈值未用
真实数据校准、没有目标跟踪框、仅本地工件路径、没有 TLS、未自动测试物理摄像头等明确
限制。只要不把这些限制隐去，本章可以作为第 11 章输入基础。

鉴定日期：2026-08-17。

---

## 2. 鉴定方法

本次没有只检查“文件存在”，而是按四个必审维度执行：

1. 逻辑：检查职责、状态、取消、错误与字段方向是否自洽。
2. 技术栈：检查每项技术是否说明是什么、为什么用、在哪里实现及其限制。
3. Demo：编译 C++，运行算法测试，启动真实 gRPC 服务，由 Python 跨进程消费。
4. 桥接：向前复用第 3/4/9 章契约与运行时，向后只输出第 11 章需要的 Observation。

另外执行静态类型、格式、全量 Python 回归、Debug CTest 和显式 Cancel 联调。

---

## 3. 草稿阶段发现并修正的问题

### 3.1 目标占比可能被伪造

初始设计曾考虑用中央区域近似目标框。这个做法会让 `target_ratio` 看似精确却没有来源。

修正：质量评估器只有收到可信 ROI 才计算占比；当前 gRPC 服务无跟踪器，因此 Demo 中
`target_ratio=null`。单元测试同时覆盖有 ROI 的 0.25 和越界拒绝。

### 3.2 glare 字段方向不一致

旧示例使用 `glare=0.08` 表达“反光很少”，但 `QualitySignals` 总体约定是 0 差、1 好。

修正：协议、Pydantic 和正文统一规定 glare 为“抗反光质量”；0 强反光，1 低反光。旧示例
改为 `0.92`，C++ 公式输出也遵守越大越好。

### 3.3 资源竞争请求可能删除活动取消令牌

初版 `RESOURCE_EXHAUSTED` 分支调用了统一完成函数。若竞争请求使用与活动请求相同 ID，
理论上可能从注册表删除活动 stop_source。

修正：把“从 active map 注销并计数”和“仅记录 RPC 完成”拆开。只有成功注册的请求可以
调用 `complete_request(request_id)`；非法或资源竞争请求只调用完成计数。

### 3.4 生成 Protobuf 模块缺少静态类型接口

只有 `_pb2.py` 时，mypy 无法看到动态生成的消息和枚举属性，会产生大量假错误。

修正：生成器增加 `--pyi_out`，提交 `realsight_pb2.pyi`，手写客户端通过严格 mypy。

### 3.5 Windows 路径不是合法 JSON

第 9 章视频生成器声称输出 JSON，但 Windows 路径中的反斜杠未转义。

修正：C++ 工具对反斜杠和双引号执行 JSON 转义。此问题不属于 gRPC 核心，但会影响第 10
章一键联调输出的机器可读性，因此没有留到以后。

### 3.6 旧测试把阶段边界写死

第 8/9 章测试断言 CMake 不得出现 gRPC。第 10 章实施后，该断言不再代表正确架构。

修正：测试改为更稳定的边界检查，允许独立 transport target 使用 gRPC，但断言 runtime
target 的区段不链接 `gRPC::`。

---

## 4. 逻辑鉴定

### 4.1 推导顺序

正文顺序为：帧与观察边界 -> 不逐帧传输 -> 质量公式 -> 关键帧 -> 协议演进 -> gRPC 流
-> 取消/错误/背压 -> Python 适配 -> Demo/测试 -> 生产差距 -> 第 11 章桥接。

结论：顺序闭环，没有要求读者先理解尚未介绍的 OCR 或主 Agent。

### 4.2 职责边界

- C++ runtime：采集、队列、停止与统计。
- C++ quality/selector：画面条件与关键帧。
- C++ transport：wire 事件、状态码和取消注册。
- Python adapter：wire 到 Pydantic 的语义入口。
- 第 11 章：Observation 到 Evidence。

结论：没有把 C++ 称为 Agent，没有在 local_features 中输出 USB-C 业务字段。

### 4.3 状态与失败语义

`ACCEPTED`、`REJECTED`、Failure event 与非 OK gRPC status 已分开说明。显式取消既返回
failure 事件又以 `CANCELLED` 结束，成功 Demo 与取消 Demo 均验证。

结论：逻辑通过。

### 4.4 仍有的逻辑限制

- `no_frames` 通过 Failure event + gRPC OK 表达，调用者必须消费事件，不能只看终态。
- 应用超时在帧边界检查；永久阻塞的驱动读取无法被 stop_token 强制打断。
- 关键帧只按总体质量选择一张，没有语义多样性。

这些限制已进入正文，不影响当前受限验收，但不能用于宣称通用感知服务完成。

---

## 5. 技术栈鉴定

| 技术 | 是否说明是什么 | 是否说明为什么 | 是否指出实现位置 | 是否说明限制 |
|---|---|---|---|---|
| C++20 | 是 | 移动帧、停止、同步与强类型 | runtime/perception/transport | 是 |
| OpenCV | 是 | 像素统计、颜色转换、JPEG | quality_evaluator、imwrite | 是 |
| Protobuf | 是 | 单一跨语言契约、presence | realsight.proto、生成脚本 | 是 |
| gRPC | 是 | 流、deadline、取消、状态码 | service/client | 是 |
| Pydantic v2 | 是 | wire 后业务二次校验 | grpc_client.py | 是 |
| CMake | 是 | target 依赖与 C++ 代码生成 | cpp/CMakeLists.txt | 是 |
| uv/Python 3.12 | 是 | 锁定生成器与测试环境 | pyproject/uv.lock | 是 |
| pytest/CTest/mypy/ruff | 是 | 分层行为和静态质量 | tests/CI | 是 |

技术版本和本机实际版本已记录。官方资料只用于支持 API 和演进原则，质量阈值明确标注为本
项目启发式，没有把 OpenCV 函数文档误写成阈值科学证明。

结论：技术栈介绍通过。

---

## 6. Demo 鉴定

### 6.1 成功链路

实际命令：

```powershell
uv run --locked python scripts/run_ch10_demo.py
```

关键事实：

- C++ 服务绑定随机 loopback 端口。
- 24 帧测试视频在 C++ 内处理。
- Python 收到按 sequence 排序的进度流。
- 最终关键帧携带非负源帧序号和视频位置；具体候选随并发队列时序变化。
- 最终 Observation 为 accepted。
- sharpness/exposure/glare/overall 均在 0～1。
- target_ratio 为 null，符合无 ROI 边界。
- Python 验证 keyframe.jpg 存在。
- 最终输出 `CH10_DEMO_OK`。

### 6.2 取消链路

实际命令：

```powershell
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
```

关键事实：

- Python 在 source_opened 后调用 Cancel。
- CancelResponse.accepted=true。
- C++ 发送 code=cancelled 的 Failure。
- Python 收到 gRPC `CANCELLED`。
- 最终输出 `CH10_CANCEL_OK`。

### 6.3 Demo 大小与可理解性

对初学者的主 Demo 只有构造请求、迭代事件和验证工件三步；进程启动细节放在独立脚本。
生成代码有顶部说明，手写 Python 和 C++ 文件都有整体逻辑、技术栈、调用流程与边界。

结论：Demo 通过。

---

## 7. 桥接鉴定

### 7.1 与前章桥接

- 直接复用第 9 章 FrameSource、Frame、BoundedFrameQueue 和 PerceptionRuntime。
- 直接实现第 4 章预留的 server-streaming service。
- 使用第 3 章已有 deadline/cancel/event 概念。
- gRPC channel 没有进入第 6 章 checkpoint。
- 观察次数仍可由第 7 章治理层计数。

结论：前向桥接清晰。

### 7.2 与后章桥接

第 11 章可直接消费 accepted Pydantic Observation 的 image_path、质量、view_type、目标 ID
和来源帧位置，再生成 Evidence。第 10 章没有提前实现 OCR，也没有把质量分数当 Evidence
confidence。

结论：后向桥接清晰。

---

## 8. 验证结果

已完成：

- C++ Debug 构建成功，6/6 CTest 通过。
- C++ Release 构建成功，6/6 CTest 通过。
- Python 新增协议测试通过。
- Python 全量回归 147 passed。
- ruff format/check 通过。
- strict mypy 通过。
- uv 锁文件检查通过。
- Python Protobuf 重新生成成功。
- 成功跨语言 Demo 通过。
- Cancel 跨语言 Demo 通过。
- doctor 8/8 通过，识别 OpenCV、protoc、grpc_cpp_plugin 和 grpcio。
- 正文约 2.3 万字符，超过完整课程章的 8000 字目标且未靠重复代码填充。

---

## 9. 未通过生产验收的项目

以下项目不应被“课程通过”掩盖：

1. 没有真实充电器质量标注集和阈值校准报告。
2. 没有物理摄像头型号/驱动矩阵。
3. 没有 callback API 与并发压测。
4. 没有 TLS、认证和远程部署。
5. 没有对象存储、内容哈希和工件清理。
6. 没有目标检测/跟踪，因此 target_ratio 不可用。
7. 没有 P50/P95/P99 延迟、吞吐和长稳测试。
8. 没有驱动永久阻塞时的进程级恢复。
9. 没有 gRPC health、metrics、reflection 和 tracing。

这些是后续工程化方向，不阻塞第 11 章教学，但若项目范围扩大必须重新评估。

---

## 10. 最终判定

四项必审结果：

- 逻辑：通过，限制已公开。
- 技术栈：通过，职责、使用理由、位置和限制均清楚。
- 小 Demo：通过，成功与取消均为真实 C++/Python 跨进程链路。
- 前后桥接：通过，复用前章运行时，输出后章所需 Observation，不越界到 OCR。

最终判定：**条件通过，可以进入第 11 章；不得把本章描述为生产级多租户感知平台。**
