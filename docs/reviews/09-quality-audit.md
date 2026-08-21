# 第 9 章质量鉴定报告：C++ 实时感知运行时

> 鉴定对象：`docs/chapters/09-cpp-realtime-perception-runtime.md`、第 9 章 C++ 源码、
> CMake、doctor、CI、Python 工程契约测试、CTest 与 AVI Demo。
>
> 鉴定原则：不因为“编译成功”自动判定课程通过；分别审查逻辑、技术栈、Demo、桥接，
> 对未验证能力保留限制。

---

## 1. 最终结论

| 必审项 | 结论 | 限定 |
|---|---|---|
| 逻辑是否闭环 | 通过 | 单源、单采集线程、单消费回调范围内闭环 |
| 技术栈是否介绍清楚 | 通过 | C++20/OpenCV/CMake/CTest/doctor 均说明职责与边界 |
| 是否有小型可运行 Demo | 通过 | 真实生成并解码 24 帧 MJPG AVI，不依赖 LLM/Python/摄像头 |
| 是否桥接前后章节 | 通过 | 继承第 8 章工程目标，为第 10 章质量评分和 gRPC 留出明确接口 |

章节可以交付，但结论不是“完整实时视觉产品已完成”。物理摄像头没有纳入自动测试，
OpenCV backend 阻塞读取不能被 stop token 强制中断，质量评分和跨语言传输仍属于第 10 章。

---

## 2. 鉴定范围与方法

本次审查包含：

- 所有第 9 章新增/修改的 `.hpp`、`.cpp` 和 CMake 文件。
- `realsight-doctor` 的 C++ OpenCV 探测。
- 第 8 章阶段性 CMake 边界测试的演进。
- 四个 CTest 可执行程序。
- 第 9 章 Python 工程契约测试。
- CMake 配置、干净重编译和运行输出。
- 生成视频、完整回放、慢消费者回放与按原帧率回放。
- 第 1～9 章 Python 全量回归。

不包含：

- 远程 GitHub Actions runner 实际执行。当前目录没有远程 CI 运行证据。
- 用户物理摄像头占用与权限测试。
- 多摄像头、音频、GPU 和真实 USB-C 视觉识别。
- 第 10 章质量评分、Protobuf 或 gRPC。

---

## 3. 逻辑闭环鉴定

**结果：通过。**

正文推导顺序为：

```text
连续现实的过载问题
-> Frame 的身份/时间/像素
-> Frame 与 Observation 分层
-> FrameSource 抽象
-> OpenCV 文件/摄像头适配
-> 有界队列和溢出策略
-> 生产者/消费者
-> 协作停止与错误
-> RuntimeSummary
-> Demo/测试
-> 第 10 章 Observation 桥接
```

没有要求读者先理解 gRPC 才能理解帧，也没有把 Agent 路由混进设备采集线程。每个设计
选择都有可观察结果：队列策略对应 `dropped_frames`，结束语义对应 `stop_reason`，所有权
对应禁止复制和移动传递，回放节奏对应 `elapsed_ms`。

### 3.1 职责边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| `Frame` | 像素与源内时序身份 | 证据、OCR、网络 |
| `FrameSource` | 统一读取结果 | 队列策略、Agent |
| `OpenCvFrameSource` | 打开/读取视频和摄像头 | 质量评分、重连治理 |
| `BoundedFrameQueue` | 容量、等待、覆盖、关闭 | 图像算法、持久化 |
| `PerceptionRuntime` | 线程、停止、错误、统计 | gRPC、LangGraph |
| CLI | 参数、信号、Demo 消费者 | 生产 API、浏览器 UI |

职责不存在循环依赖。`RealSight::runtime` 公开帧运行时，CLI 依赖库；库不反向依赖 app。

---

## 4. 审计中发现并修正的问题

### F1：第 8 章测试把阶段性边界写成永久事实

初始测试断言 CMake 中不存在 `find_package(OpenCV)`。进入第 9 章后，这会把正确演进判定
为失败。

判断：第 8 章当时“不提前引入”是正确验收，但测试不应阻止后续章节实现计划能力。

修正：测试改为要求 OpenCV 已存在、gRPC 仍不存在，同时保留 target scoped C++20 检查。

### F2：原 C++ 测试依赖 `assert`

标准 `assert` 在 Release/NDEBUG 下可能完全消失，出现“测试进程退出 0，但什么都没检查”。

修正：增加显式 `TestContext`，失败打印文件/行并返回非零；Python 契约测试扫描 C++ 测试，
阻止重新引入运行时 `assert`。`static_assert` 保留，因为它是编译期约束。

### F3：读取结果最初容易退化为 bool

若只使用 `read=true/false`，视频 EOF、取消和设备故障无法区分。

修正：`SourceReadStatus` 固化 `frame/end_of_stream/stopped/error` 四态；运行时进一步输出
六种 `RuntimeStopReason`。

### F4：对 cv::Mat 所有权的表述可能过度

删除 `Frame` 复制确实约束逻辑所有权，但 `cv::Mat` 自身引用计数允许浅拷贝，不能声称
像素在所有代码路径上绝不共享。

修正：正文准确说明“源创建局部 Mat 并移动，Frame 禁止复制”；同时指出 ROI/浅拷贝仍
可能共享，真正隔离需要 `clone()` 且有成本。

### F5：无界队列会把吞吐问题变成内存和延迟问题

修正：队列强制正容量，并实现 `drop_oldest` 与 `block_producer`。正文用 1080p BGR 帧
计算容量成本，不把“加大队列”描述成性能修复。

### F6：取消的 drop push 可能误删旧帧

早期顺序是“队列满则移除旧帧，然后检查 stop token”。若调用在进入时已取消，新帧不会
接受，旧帧却已丢失。

修正：锁定后先检查关闭/取消，再执行溢出策略；增加预取消 push 保留旧序号的测试。

### F7：帧时间只使用 steady_clock 仍不保证严格大于

`steady_clock` 不倒退，但连续读取理论上可能取得相同 tick。CLI 使用严格递增检查时会
偶发失败。

修正：OpenCV 源若新时间不大于上一帧，增加一个最小时钟单位；视频测试从 `>=` 提升为
严格 `>`。

### F8：第三方 FrameSource 抛异常会终止进程

接口约定返回错误，但未来设备 SDK 适配器可能抛异常。异常逃出线程入口会触发
`std::terminate`。

修正：生产者线程边界捕获标准和未知异常，转成 `source_error`；新增抛异常源测试。

### F9：CMake 初次构建产生缺字段警告

指定初始化器只写部分字段，在 GCC `-Wmissing-field-initializers` 下产生警告。

修正：调用点显式初始化剩余 optional 字段；干净重编译无警告。

### F10：生成器与播放器被并行启动

初次 Demo 验证把独立命令并行执行，播放器可能在 AVI 尾部尚未写完时打开文件，得到
OpenCV/GStreamer 打开失败。

判断：代码正确拒绝了不完整输入，问题在 Demo 编排。

修正：正式说明要求生成器进程退出后再回放；最终按顺序执行成功。该失败保留在正文作为
真实故障案例。

### F11：CAP_ANY 对损坏文件产生无关 backend 噪声

修正：普通视频优先 `CAP_FFMPEG`，不可用再回退 `CAP_ANY`，并始终输出实际 backend。
本机有效文件使用 FFmpeg。MSYS2 OpenCV 可选依赖仍可能在退出时打印 GLib/GIO 环境警告，
退出码、JSON 和测试不受影响；报告没有把该环境噪声伪装成已彻底消失。

### F12：OpenCV 安装探测只检查 opencv4

本机 OpenCV 5 包发布 `opencv5` 构建元数据，固定检查 `opencv4` 会误报缺失。

修正：doctor 按 `opencv5 -> opencv4 -> opencv` 查找，并在没有 pkg-config metadata 时
回退检查编译器前缀下的 `OpenCVConfig.cmake`。

---

## 5. 技术栈说明鉴定

**结果：通过。**

| 技术 | 是什么 | 为什么本章使用 | 明确不负责 | 后章桥接 |
|---|---|---|---|---|
| C++20 | 原生语言标准 | jthread、stop token、chrono、移动语义 | Agent 推理 | 第 10 章继续作为感知端 |
| OpenCV 5.0.0 | 视觉/视频 I/O 库 | Mat、VideoCapture、VideoWriter | 证据决策 | 第 10 章质量指标 |
| `steady_clock` | 单调计时时钟 | 顺序、延迟、回放节奏 | 墙上日期时间 | Observation 时间契约 |
| RAII | 作用域资源管理 | 帧、线程、VideoCapture 自动释放 | 业务状态持久化 | gRPC channel 生命周期 |
| 移动语义 | 转移对象状态 | 高频帧逻辑所有权 | 绝对禁止 Mat 浅共享 | 关键帧传递 |
| FrameSource | 依赖倒置接口 | 文件/摄像头/测试源统一 | 队列与算法 | 可增加设备 SDK adapter |
| 有界队列 | 限制并发缓冲 | 限制内存、显式过载 | 自动提高吞吐 | gRPC 背压对照 |
| jthread/stop token | 协作式线程停止 | 可取消条件等待和循环 | 强制打断驱动 read | RPC 取消映射 |
| CMake target | 构建依赖图 | OpenCV include/lib 传播 | 包下载策略 | 第 10 章 proto target |
| CTest | C++ 测试注册/运行 | Debug/Release 都执行显式检查 | Python 测试 | 质量算法测试 |
| doctor | 环境探测 | 区分 Python wheel 与 C++ dev 包 | 摄像头认证 | 第 10 章检查 protoc/gRPC |

正文还解释了 2026 年 OpenCV 5 的稳定分支定位、C++17 最低要求和旧 C API 移除，并使用
官方项目资料，不以过时版本印象代替当前事实。

---

## 6. Demo 鉴定

**结果：通过。**

### 6.1 Demo 是否足够小

Demo 只有两个本地 C++ 进程：

```text
realsight-generate-video
-> 24 帧、160x90、20 FPS MJPG AVI
-> realsight-perception
-> 两条 JSON 事件
```

它不依赖大模型、Python Agent、网络、数据库或物理摄像头，集中验证第 9 章新增能力。

### 6.2 输入是否真实可解码

通过。生成器使用 OpenCV `VideoWriter` 写出 24,566 字节 AVI；OpenCV source CTest 也在
临时目录写入并读回 8 帧视频，不是把随机字节改名为 `.avi`。

### 6.3 完整性模式结果

```text
backend=FFMPEG
produced=24
consumed=24
dropped=0
last_sequence=23
timestamps_monotonic=true
stop_reason=source_exhausted
```

符合 `block_producer` 预期。

### 6.4 新鲜度模式结果

```text
queue_capacity=2
consumer_delay_ms=20
produced=24
consumed=3
dropped=21
last_sequence=23
```

满足计数守恒，且保留最后帧。精确消费数量受线程调度影响，因此测试只断言不变量，不把
`3` 当跨机器固定值。

### 6.5 回放节奏结果

24 帧、20 FPS 默认节奏实测约 1204ms；`--no-realtime` 完整回放约 4ms。两者证明
“媒体节奏”和“最大解码吞吐”是不同模式。

### 6.6 Demo 未证明什么

- 没有证明摄像头 0 在用户机器可用。
- 没有证明所有视频 codec/backend 均可用。
- 没有视觉质量或 USB-C 标签识别。
- 没有跨进程流。

---

## 7. 注释与可学习性鉴定

**结果：通过。**

所有第 9 章 C++ 头文件、源文件、测试文件、工具和两个 CMake 文件在顶部包含：

- 文件整体逻辑。
- 使用的技术栈。
- 调用流程。
- 关键边界，或紧随前三项的边界说明。

Python `test_ch09_cpp_runtime_contract.py` 和修改后的 doctor 保留完整模块说明。自动化测试
逐文件扫描前三项，防止未来新增文件遗漏。

正文提供面向基础较弱学习者的阅读顺序，从 `Frame` 到 `FrameSource`、队列、runtime、
OpenCV adapter，最后才读 CLI；没有要求从最长入口硬啃。

---

## 8. 前后章节桥接鉴定

**结果：通过。**

### 8.1 向前桥接第 8 章

- 延续 `RealSight::runtime` 别名 target。
- 延续 target scoped `cxx_std_20` 和警告选项。
- 使用既有 Windows GCC preset、CTest 和 Linux CI job。
- doctor 从六项演进为七项，未另写重复自检脚本。
- CMake 阶段边界测试随课程演进，没有删除 gRPC 防抢跑检查。
- Python 正式包和前 8 章领域类型未改名。

### 8.2 向后桥接第 10 章

第 10 章可以直接在 `FrameConsumer` 位置加入：

```text
QualityScorer
-> KeyFrameSelector
-> Observation builder
-> artifact writer
-> gRPC adapter
```

可复用字段与行为包括：

- `sequence` 与严格单调时间。
- `cv::Mat` 像素。
- 过载策略和 dropped 统计。
- 外部取消。
- source/consumer 错误边界。
- CMake OpenCV target 与 CTest。

本章没有提前生成 proto、链接 gRPC 或把每帧序列化，因此后章职责清晰。

---

## 9. 实际验证结果

### 9.1 环境

| 项目 | 实测 |
|---|---|
| Python | 3.12.13 |
| uv | 0.11.30 |
| CMake | 4.4.2 |
| GCC | 16.2.0 |
| OpenCV | 5.0.0 |
| OpenCV CMake 前缀 | `E:/msys64/ucrt64` |

### 9.2 构建与 C++ 行为

| 检查 | 结果 |
|---|---|
| CMake configure | 通过，找到 core/imgproc/videoio |
| clean build | 通过，无编译警告 |
| Debug CTest | 4/4，约 0.55s |
| Release CTest | 4/4，约 0.51s |
| AVI 生成器 | 退出码 0，24 帧，24,566 bytes |
| 完整回放 | 24/24/0，退出码 0 |
| 慢消费者 | 24/3/21，最后序号 23，退出码 0 |
| 实时节奏 | 约 1204ms，退出码 0 |

### 9.3 Python 质量门禁

最终定稿复验结果：

- Python 全量 pytest：141/141，约 10.62s。
- 其中第 8/9 章相关 pytest：21/21。
- Ruff format/check：通过。
- mypy strict scope：通过，13 个 source files。
- doctor：7/7 pass，`ready=true`。
- `uv lock --check` 与 locked sync：通过，55 packages。

### 9.4 CI

Linux C++ job 已增加 `libopencv-dev` 安装步骤，然后执行 configure/build/ctest。工作流命令
有本地对应验证，但当前目录没有远程 GitHub Actions run，故结论是“CI 定义已更新”，
不是“远程 CI 已通过”。

---

## 10. 残余风险与限制

### R1：物理摄像头未自动验收

代码路径存在，但设备索引、权限、驱动和占用情况只能在目标机器确认。章节给出手工命令
和检查项，没有虚构测试结果。

### R2：阻塞驱动读取不能硬取消

stop token 能取消队列与回放等待，不能保证中断 backend 内部阻塞。第 10 章传输取消不能
掩盖这个底层限制；生产版本可能需要 backend 超时或进程隔离。

### R3：单生产者、单消费者

符合一个月 MVP 的单摄像头边界。多消费者广播会改变所有权、队列和丢帧语义，不能只
复制一个回调。

### R4：cv::Mat 并非绝对唯一像素所有权

Frame 禁止复制，但未来 ROI/浅拷贝仍可共享像素。第 10 章若并行评分，必须规定只读、
clone 或独占修改策略。

### R5：source_position 受 backend 影响

`CAP_PROP_POS_MSEC` 可选且精度由 backend 决定，不能作为跨进程唯一时间。协议必须定义
序号与相对时钟语义。

### R6：codec 可用性跨平台变化

MJPG/FFmpeg 在本机通过，Linux CI 依赖发行版 OpenCV。测试会暴露缺 codec，但不能保证
所有用户视频格式。

### R7：OpenCV MSYS2 包体积大

本次事务包含较多视频/视觉依赖。团队应在构建镜像固定包版本和缓存；不要让每次应用启动
动态安装依赖。

### R8：GLib/GIO 环境警告

本机 OpenCV 可选组件在进程退出时可能输出 Windows 应用清单警告，即使实际 backend 是
FFmpeg。退出码、JSON 和测试通过。固定部署镜像可裁剪 backend；应用不能简单吞掉全部
stderr。

### R9：轻量 C++ TestContext 不是成熟测试框架

当前测试数量小且显式。后续若参数化、fixture 和并发诊断显著增长，应评估 Catch2 或
GoogleTest，并固定依赖来源。

### R10：没有远程 CI 证据

YAML 已更新，本地等价门禁通过；推送后仍需看 GitHub Actions 结果。

---

## 11. 最终门禁

| 门禁 | 状态 | 备注 |
|---|---|---|
| 逻辑闭环 | PASS | 单源实时运行时范围内成立 |
| 技术栈清楚 | PASS | 职责、取舍、版本与边界完整 |
| Demo 可运行 | PASS | 真实 AVI 两种策略实测 |
| 前后桥接 | PASS | 第 8 章增量演进，第 10 章接口明确 |
| 注释要求 | PASS | C++/Python/CMake 均有顶部说明 |
| C++ 测试 | PASS | Debug 4/4、Release 4/4，不依赖运行时 assert |
| 摄像头硬件认证 | NOT CLAIMED | 留作目标机器手工验收 |
| 第 10 章能力 | NOT IMPLEMENTED | 质量评分、Observation、gRPC 未抢跑 |

最终判断：第 9 章达到“工程型教学 MVP”的交付标准，可以进入用户复核；在用户确认前不
继续生成第 10 章。
