# 第三周：C++ 实时感知与 gRPC

这一周研究高频像素路径。你不需要先成为 C++ 专家，但必须能说明一帧图像的逻辑所有权，
生产者变快时队列怎样保护内存，停止如何从 RPC 传到采集循环，以及为什么跨进程只发送
Observation 元数据和关键帧引用。

## Day 13：C++20 最小基础与所有权——一帧图像到底归谁

### 今天结束时你必须会

- 区分声明/定义、头文件/源文件、编译/链接。
- 解释栈、堆、值、引用、指针、RAII、智能指针和移动语义。
- 看懂 `std::optional`、`std::chrono`、异常与 `[[nodiscard]]`。
- 解释 `Frame` 禁止复制及关键帧需要 `cv::Mat::clone()` 的完整理由。

### 1. C++ 源码怎样变成程序

头文件通常公开类型和函数声明，源文件提供实现。每个 `.cpp` 先独立编译为目标文件，链接器
再把它们与 OpenCV、gRPC 等库组合为可执行文件。找不到头文件是编译/包含路径问题；函数
声明可见但实现找不到是链接问题；程序启动后崩溃才是运行时问题。

`#pragma once` 防止同一头文件在一个编译单元重复展开。namespace 防止名字冲突。不要把所有
实现都塞进头文件，也不要为了修链接错误随意 include `.cpp`。

### 2. 栈、堆、引用和指针

局部 `Frame frame(...)` 对象本身有自动生命周期；它包含的 `cv::Mat` 是一个小型矩阵头，
背后像素缓冲由 OpenCV 引用计数管理。引用 `const Frame&` 表示借用且不为空；裸指针可以为空，
本项目 `selected()` 返回 `const SelectedKeyframe*` 表达“当前可能没有候选”。

`std::unique_ptr<OpenCvFrameSource>` 表达独占动态对象：离开作用域自动析构并释放
`VideoCapture`。`std::shared_ptr<std::stop_source>` 在 gRPC active request 表中表达多个地方
共同持有取消控制。不要因为 shared_ptr 方便就到处使用；共享所有权会让资源释放时刻更难推理。

### 3. RAII 是什么

RAII 的核心是让资源生命周期绑定对象生命周期：构造取得有效资源，析构释放。mutex lock、
jthread、unique_ptr、cv::Mat、VideoCapture 都利用这个思想。它不是“永远不会泄漏”的魔法；
若对象生命周期设计错误或形成 shared_ptr 环，仍会出问题。

项目中的例子：`OpenCvFrameSource::~OpenCvFrameSource()` 释放 capture；`std::unique_lock` 即使
异常也解锁；`std::jthread` 提供协作停止和析构 join，但正式代码仍显式 join 以明确收束点。

### 4. 移动语义为什么适合 Frame

Frame 禁止复制：

```cpp
Frame(const Frame&) = delete;
Frame& operator=(const Frame&) = delete;
Frame(Frame&&) noexcept = default;
```

采集源构造 Frame，`std::move` 放入队列，消费者再移动取走。这表达单一逻辑所有者并阻止
无意复制大型帧对象。移动后的对象仍可析构，但其业务内容不应再使用。

然而 `cv::Mat` 的普通拷贝是浅拷贝：复制矩阵头并共享像素缓冲。Frame 禁止 C++ 层复制，
并不意味着底层像素天然深拷贝。关键帧要在原 Frame 离开消费作用域后长期保存，而且后续
缓冲可能被释放或改写，所以候选用 `frame.pixels.clone()` 取得独立像素所有权。

### 5. 其他必须看懂的 C++20 语法

- `std::optional<T>`：可能没有 T；比魔法值 `-1` 更明确。
- `std::chrono`：单位进入类型，避免毫秒/秒混算。
- `enum class`：强类型枚举，不会随意转 int。
- `const`：当前接口承诺不经此引用修改对象。
- `noexcept`：承诺函数不抛异常，移动时有助于容器选择安全路径。
- `[[nodiscard]]`：提醒调用者不能忽略重要结果，如 push 是否成功。
- designated initializer：`.accepted = false` 让结构结果可读。

### 6. 今日阅读路径与练习

1. `runtime/frame.hpp`：只看 public 数据和五个特殊成员函数。
2. `runtime/frame.cpp`：构造时怎样移动 `cv::Mat`。
3. `frame_source.hpp`：纯虚接口和四种 read status。
4. `opencv_frame_source.cpp`：unique_ptr、异常与 RAII。
5. `keyframe_selector.cpp::make_candidate()`：唯一明确深拷贝点。

在个人 C++ 文件写一个只可移动的 `MiniBuffer`，放入 `std::deque` 再取出；分别尝试复制和
移动，阅读编译器错误。然后用两份 `cv::Mat` 测试浅拷贝：修改共享副本观察原图变化，再用
clone 证明独立。

```powershell
uv run --locked cmake --build --preset windows-gcc-debug
uv run --locked ctest --preset windows-gcc-debug -R "runtime|keyframe" -V
```

### 7. 常见陷阱

- 认为 `std::move` 会立即复制/移动字节；它主要是允许调用移动操作。
- 认为 const 引用永远安全；被引用对象仍必须活着。
- 认为 cv::Mat 赋值就是图像深拷贝。
- 返回局部对象裸指针，作用域结束后悬空。
- 捕获异常后忘记 RAII 之外的业务状态清理。
- 把 unique_ptr、shared_ptr 和原始指针都简单解释成“更安全的指针”。

### 8. 今日答辩

1. Frame 禁止复制解决的是什么问题？它是否保证像素绝不共享？
2. 为什么 `selected()` 返回指针，而 `FrameSource::read()` 使用 optional？
3. `cv::Mat::clone()` 为什么只在候选变好时调用？
4. 析构、异常和 RAII 怎样配合？
5. 编译成功但链接失败，最可能属于哪类问题？

---

## Day 14：生产者、消费者、有界队列与背压

### 今天结束时你必须会

- 画出采集线程、队列、消费线程和停止源的时序图。
- 解释 mutex、condition variable、谓词、原子计数分别保护什么。
- 根据实时/回放场景选择 `drop_oldest` 或 `block_producer`。
- 证明停止的 push 不会先删除旧帧。

### 1. 为什么需要队列

摄像头采集和质量评估速度不完全一致。若两者在同一循环，消费偶尔变慢会直接拖住采集；
若使用无限队列，帧会持续积压，内存和延迟越来越大。有界队列把最大积压固定为 capacity，
并要求明确溢出策略，这就是背压设计。

### 2. 两种溢出策略

**drop_oldest**：队列满时删除最旧未消费帧，再放入最新帧。适合实时摄像头，目标是看到
现在，代价是不能保证每帧都分析。

**block_producer**：队列满时生产者等待消费者腾位置。适合需要逐帧回放或确定性测试，
代价是采集可能落后；真实摄像头后端还可能在驱动层自行缓存/丢帧。

不能说某一种绝对更好。选择指标不同：实时新鲜度、完整性、内存、吞吐、最大延迟。

### 3. mutex 与 condition variable 的职责

mutex 保护共享 `frames_` 和 `closed_` 在同一时刻只能由一个线程修改。condition variable
让线程在“非空/未满/关闭”条件未满足时休眠，而不是忙循环占 CPU。等待必须带谓词，因为
存在虚假唤醒，也因为通知只表示“状态可能变了”。

`condition_variable_any` 的 stop-token wait 能在取消时返回。push 成功后通知 `not_empty_`；
pop 后通知 `not_full_`；close 要 notify_all 唤醒所有等待方。

统计 produced/dropped 在跨线程更新时使用 atomic；生产者错误文本由 mutex 保护。atomic
不是万能替代锁：队列包含多个相关状态，必须在同一个临界区维护不变量。

### 4. close 与 stop 不一样

`close()` 表示以后不接收新帧，但消费者仍可排空已存在帧。stop_token 表示调用方要求尽快
停止等待/工作。`wait_pop()` 在已关闭但队列仍有帧时仍返回旧帧；空且关闭才返回 nullopt。

push 的关键顺序是先检查 closed/stop，再处理 drop_oldest。如果先 pop 再发现取消，已取消
的新帧没有入队，旧帧却被误删。正式测试应固定这个顺序。

### 5. `PerceptionRuntime::run()` 的并发结构

```text
调用 run 的线程（消费者）          jthread（生产者）
queue.wait_pop()                    source.read(stop_token)
consumer(Frame&&)       ← queue ←  queue.push(move(frame))
计 consumed/决定停止               计 produced/dropped
request_stop + close                记录 EOF/error + close
                 → join ←
                 RuntimeSummary
```

外部 stop_token 通过 stop_callback 转发给本次 run 的 `stop_source`。消费者抛异常会被转成
`consumer_error`，不能逃出另一线程导致 `std::terminate`。最后先 request_stop 再 join，
确保 block_producer 等待可以醒来。

### 6. 实验：容量 1 与慢消费者

先读 `cpp/tests/frame_queue_test.cpp` 和 `perception_runtime_test.cpp`，再只改变一个变量：

1. queue capacity 设为 1。
2. fake source 快速生产固定数量帧。
3. consumer 每帧增加小延迟。
4. 分别运行 drop_oldest 和 block_producer。
5. 记录 produced、consumed、dropped、last sequence、elapsed。

预期不是死记数字，而是关系：drop_oldest 下 dropped 增加且 last sequence 更靠近最新；
block_producer 下 dropped 为 0、总耗时受消费者约束。

新增测试：队列已满后预先请求 stop，再 push 新帧，断言 dropped=0，try_pop 仍取得原旧帧。

```powershell
uv run --locked ctest --test-dir build/windows-gcc-debug `
  -R "frame_queue|perception_runtime" --output-on-failure
```

### 7. 常见陷阱

- 无界 queue 在短测试不爆内存，就误认为安全。
- wait 不写谓词，虚假唤醒后访问空队列。
- 持锁执行慢 consumer，生产者永远无法 push。
- close 时只通知消费者，不通知 block producer。
- 把 dropped 当作错误；实时系统中它可能是有意策略。
- 统计关系写成 `produced = consumed + dropped` 的绝对定律；停止时仍可能有未消费队列项。

### 8. 今日答辩

1. capacity=1 为什么不等于“没有队列”？
2. close 后为什么还允许 pop？
3. drop_oldest 的业务目标是什么？什么时候不能用？
4. 为什么停止检查必须早于 pop_front？
5. jthread 已自动 join，代码为什么仍显式收束？

---

## Day 15：OpenCV 视频源与真实摄像头——EOF、故障和时间

### 今天结束时你必须会

- 解释 `cv::VideoCapture` 在视频文件和摄像头场景的不同语义。
- 区分 frame、end_of_stream、stopped、error。
- 说明 steady clock、system clock 和视频 source position 各自用途。
- 用真实摄像头运行 CLI，并对错误设备号/拔出设备作诚实记录。

### 1. 为什么 FrameSource 不直接返回 bool

OpenCV `read()` 失败时，视频文件可能只是正常 EOF，摄像头却可能断开。用户取消也不是设备
故障。`SourceReadResult` 用四种 enum 明确表示，让 runtime 选择正确 stop reason，而不是把
所有 false 写成 error 或无限 retry。

### 2. `VideoCapture` 是适配层，不是业务层

`OpenCvFrameSource::open_video()` 优先 FFmpeg、失败后 CAP_ANY；`open_camera()` 用设备索引和
backend。请求宽高/FPS 的 `set()` 只是请求，驱动可能拒绝或调整，所以代码重新读取实际值
填 `SourceDescriptor`。不能仅根据 set 返回 true 就声称摄像头以目标规格工作。

VideoCapture 的析构/release 释放设备；当前实现不弹 GUI，不做 OCR、不评分。这样的边界让
测试可以用 fake FrameSource，而不要求 CI 有摄像头。

### 3. 三种时间为什么都需要

- `steady_clock captured_at`：单调递增，适合计算运行时耗时，不受系统改时钟影响。
- `system_clock captured_at_utc`：可转换成墙钟时间，写入跨进程 Observation 做审计。
- `source_position`：预录视频中的位置，便于回到原样本复现。

不能用 system clock 做严格耗时，因为 NTP/用户调时可能跳变；不能用 steady clock 数值跨
进程当绝对时间，因为它的 epoch 只在本机本次运行有意义。

### 4. 视频按原速回放

`pace_as_recorded` 根据 FPS 和 frame sequence 计算目标 steady-clock 时刻，用短至约 5ms 的
sleep 循环等待，期间检查 stop token。这比一次 sleep 到很久以后更容易响应取消。无有效
FPS 或第一帧不等待。

### 5. 真实设备实验

先运行视频源测试确保可重复基线：

```powershell
uv run --locked ctest --test-dir build/windows-gcc-debug `
  -R "opencv_source|perception_runtime" --output-on-failure
```

查看 CLI 帮助确定当前参数，再用摄像头 0：

```powershell
build\windows-gcc-debug\cpp\realsight-perception.exe --help
build\windows-gcc-debug\cpp\realsight-perception.exe `
  --camera 0 --max-frames 120 --queue-capacity 4 --overflow drop-oldest
```

记录实际 backend、宽高、FPS、summary。再用明显错误的设备号，确认在打开阶段失败，而不是
产生 accepted Observation。若条件允许，在运行中拔掉外接摄像头，记录 read 返回 error 前的
延迟；不要反复拔插内置设备。

`stop_token` 无法强制打断永久阻塞的驱动 read，这是当前边界。生产化可考虑厂商 API、设备
超时、独立采集进程或 watchdog，但本月不假装已解决。

### 6. 测试陷阱

- CI 测试硬编码 camera 0，导致无设备机器必失败。
- 把视频 EOF 当 retryable camera error。
- 用户取消被记录为 source_error，污染可靠性指标。
- 请求 1920×1080 后不读取实际分辨率。
- 用墙钟时间计算帧间隔。
- 摄像头 read 阻塞时认为 jthread 析构可以“杀死线程”。

### 7. 今日答辩

1. 视频和摄像头 `read=false` 为什么语义不同？
2. requested FPS 与 actual FPS 怎样验证？
3. 三种时间各解决什么？
4. 为什么回放 sleep 要定期看 stop token？
5. 拔设备后程序没有立即退出，可能卡在哪个边界？

---

## Day 16：画面质量与关键帧——可解释启发式，不是假准确率

### 今天结束时你必须会

- 用白话和公式解释清晰度、曝光、反光、ROI 占比与 overall score。
- 说明 accepted 由逐项阈值决定，而不是只看 overall。
- 解释最佳合格帧与最佳总体帧同时存在的理由。
- 用真实样本调一个阈值并补 C++ 测试。

### 1. 清晰度：Laplacian 方差

Laplacian 强调亮度快速变化的边缘；清晰文字通常有更多强边缘，模糊后变化变平。代码取
Laplacian 像素标准差平方作为方差，再除以 `sharpness_reference` 并 clamp 到 0～1：

```text
sharpness = clamp(laplacian_variance / 500)
```

它依赖纹理：纯色但对焦正确的画面也可能低，噪声很强的画面可能虚高。因此它是筛选启发式，
不是“OCR 清晰概率”。

### 2. 曝光：中心亮度与截断比例

代码同时考虑平均灰度是否接近 127.5，以及极暗/极亮像素比例：

```text
centered = 1 - abs(mean - 127.5) / 127.5
clipping = 1 - max(dark_ratio, bright_ratio)
exposure = clamp(0.6 * centered + 0.4 * clipping)
```

只看均值会漏掉“一半全黑、一半全白”的严重截断；只看截断又可能误罚正常黑色标签，所以
两者组合。低于阈值后再根据 mean 区分 under/overexposed。

### 3. 反光：HSV 低饱和、高亮像素

白色镜面高光常具有低饱和、高亮度。代码用 HSV mask 统计 glare pixel ratio：

```text
glare_quality = clamp(1 - glare_ratio / maximum_glare_pixel_ratio)
```

这里 `glare` 字段表示抗反光质量，1 好、0 差，与其他 score 方向一致。它可能把正常白底标签
误当高光，所以阈值必须用真实样本校准。

### 4. target ratio 与 overall

只有外部提供可信 ROI 时，target ratio 才是 ROI 面积/整帧面积；没有检测器时保持 nullopt，
不能假装整个画面就是目标。基础 overall 权重为 sharpness 0.45、exposure 0.35、glare 0.20；
有 ROI 时增加 0.15 后再除总权重。

accepted 不是 `overall > 某值`，而是 issues 为空：任何单项低于门槛都拒绝。这样避免高曝光
分把严重模糊平均掉。

### 5. KeyframeSelector 的两个候选

- `best_accepted_`：合格帧里 overall 最高，用于正常 Observation。
- `best_overall_`：所有帧里 overall 最高；全不合格时仍保存一张用于解释失败。

同分保留更早帧，使结果确定。只有分数严格变好才 clone；但一帧可能同时更新两个候选，
当前实现会 clone 两次，这是可讨论的小优化点，不能误说“每个输入最多一次 clone”。

### 6. 样本实验

拍或制作至少五类样本：清晰、运动模糊、过暗、过曝、强反光。固定分辨率和 ROI，记录 raw
measurements、signals、issues、accepted。先不要改阈值，观察误判来源。

```powershell
uv run --locked ctest --test-dir build/windows-gcc-debug `
  -R "quality_evaluator|keyframe_selector" --output-on-failure
```

选择一个阈值只改一次，例如 minimum_sharpness。写测试覆盖阈值下方、等于阈值、上方，解释
为何边界用 `<` 而不是 `<=`。保存调参前后混淆情况，不能只展示改善样本。

### 7. 常见陷阱

- 把 overall score 当 OCR accuracy。
- 只优化 accepted rate，阈值越松看似越好却放进大量坏帧。
- 在没有检测器时硬填 target_ratio=1。
- 用一张样本调阈值，然后声称普遍有效。
- 只保存最佳合格帧，全失败时无法诊断。
- 以为候选像素只是借用 Frame，不需要 clone。

### 8. 今日答辩

1. 为什么 Laplacian 方差可能被噪声欺骗？
2. exposure 为什么同时看均值和 clipping？
3. glare 的 1 和 0 分别代表什么？
4. overall 高为什么仍可能 rejected？
5. 全部帧不合格时保存最佳总体帧有什么价值？

---

## Day 17：Protobuf 与 gRPC——跨语言流、deadline、cancel 和 status

### 今天结束时你必须会

- 看懂 message、enum、repeated、optional、oneof、service 和字段编号。
- 解释 server streaming 为什么适合一次主动观察。
- 沿链路讲清 Python request 到 C++ Observation 再回 Python 的显式转换。
- 分别验证成功、取消、超时、无帧、非法请求和断开。

### 1. gRPC 在这里解决什么

C++ 与 Python 是独立进程，不能传 `cv::Mat` 指针。Protobuf 定义跨语言消息的线格式，gRPC
提供调用、流、deadline、取消和 status。RealSight 的 `Observe` 是 server streaming：客户端
发一个 ObservationRequest，服务连续返回 progress，最后返回 Observation 或 failure。

选择流是为了让长达数秒的摄像头扫描可观察和可取消，不是为了流式发送每帧。逐帧传 Python
会增加序列化、拷贝、网络与垃圾回收负担，并把高频背压问题推到业务进程。

### 2. Proto 元素的项目含义

- enum 的 0 留 `UNSPECIFIED`，避免默认值伪装成合法状态。
- repeated 表示多个 feature/protocol，不保证业务唯一性，适配层仍验证。
- optional 区分未提供与真实 0。
- oneof 保证一个 ObservationEvent 恰好是 progress/observation/failure 之一。
- service 定义 `Observe` 流和 `Cancel` 一元 RPC。
- field number 是 wire identity，只能新增新号，不复用旧号。

生成文件是编译产物：修改 `.proto` 后用脚本/protoc 重新生成，业务校验写在手写 adapter，
不要编辑 `*_pb2.py` 或生成 C++ 文件。

### 3. C++ server 的真实处理顺序

```text
validate request
→ register request / reject concurrent use
→ progress(request_accepted)
→ open video/camera
→ PerceptionRuntime + KeyframeSelector
→ periodic scanning progress
→ save one JPEG artifact
→ final Observation
→ unregister request and return gRPC Status
```

request ID 先按安全字符白名单验证，之后才能参与 artifact 路径。image_path 只是同机文件引用，
不是远程可访问 URL。服务当前一次只允许一个 active Observation，竞争请求返回
RESOURCE_EXHAUSTED；不能宣传高并发。

### 4. deadline 与两种 cancel

请求 payload 有业务 timeout_ms，Python RPC 也有 gRPC deadline。服务循环在帧边界检查 elapsed、
`ServerContext::IsCancelled()` 和 shared stop_source。显式 Cancel RPC 通过 request ID 找 active
stop_source 并 request_stop。

它们是协作式停止：若摄像头驱动永久卡在 `read()`，检查点尚未返回，不能硬中断。Cancel
response accepted 表示取消请求已找到并发出，不等于硬件在该纳秒已释放。

### 5. Python adapter 为什么再次转换

`perception/grpc_client.py` 把 Pydantic request 显式填进 Proto；读取事件时检查 schema version、
request/target ID、sequence、oneof 类型、枚举和 optional presence，再构造 Python dataclass/
Pydantic Observation。gRPC OK 但流里没有最终 accepted/rejected Observation，也不能默认为成功。

transport status 与业务 failure 都要保留：INVALID_ARGUMENT 不应盲目 retry，UNAVAILABLE 可能
可重试，DEADLINE_EXCEEDED 需重新判断设备状态，CANCELLED 可能由用户主动触发。

### 6. 实验矩阵

```powershell
uv run --locked python scripts/run_ch10_demo.py
uv run --locked python scripts/run_ch10_demo.py --verify-cancel
uv run --locked pytest python/tests/test_ch10_grpc_adapter.py -q
```

再手动完成：

- 服务未启动：连接失败。
- timeout_ms 很小：deadline exceeded。
- 非法 ID 或空 features：INVALID_ARGUMENT。
- 空视频/无帧：failure event，流可正常结束但 provider 不应返回 Observation。
- 客户端在 source_opened 后 Cancel：Cancel RPC accepted，Observe 返回 CANCELLED。

每种记录：最后一个 progress sequence、是否有 failure payload、最终 gRPC status、是否 retryable、
Python 抛出的结构化异常。不要只记录一行 stderr。

### 7. 常见陷阱

- gRPC Status OK 就认为业务 accepted。
- Proto oneof 有值就跳过 request/target 校验。
- 使用用户 request ID 直接拼路径而不验证。
- 逐帧传视频，破坏 C++ 本地背压边界。
- 手改 generated stub 修业务 bug。
- 将 image_path 当成任意远程部署都可访问。

### 8. 今日答辩

1. 为什么 `Observe` 用 server streaming 而不是 unary？
2. 为什么不传每帧？
3. oneof 与 optional 分别解决什么？
4. Cancel accepted 为什么不等于驱动立即停止？
5. gRPC OK 但没有 Observation 时 Python 应怎么处理？

---

## Day 18：C++ 周整合答辩与现场改造

### 上午：空白纸时序图

画出并讲解：

```text
Python PerceptionClient
→ C++ gRPC Observe
→ request registry / stop_source
→ OpenCvFrameSource
→ producer jthread
→ BoundedFrameQueue
→ consumer / FrameQualityEvaluator
→ KeyframeSelector clone
→ JPEG artifact
→ ObservationEvent stream
→ Python Pydantic Observation
```

图中必须标明：线程/进程边界、move、clone、mutex、stop 检查点、deadline、文件路径、没有跨越
边界的逐帧像素。

### 下午：独立增加一个指标或质量 issue

任选一项：

1. RuntimeSummary 增加 `peak_queue_size`。
2. KeyframeStatistics 增加 `best_score_updates` 并消除含义歧义。
3. QualityIssue 增加一种可由确定性测试构造的问题。

先做影响分析：header、source、JSON/CLI、服务 progress、Proto 是否需要、Python 是否需要、
C++ tests、文档。只在跨进程消费者确实需要字段时改 Proto；本地统计不应为了“展示完整”
自动进入网络契约。

要求：

- 先写失败测试。
- 不破坏现有 enum/string mapping。
- 并发统计没有 data race。
- CTest 6 组全部通过。
- 若改 Proto，重新生成两端、做旧字段兼容分析和 Python adapter 测试。

### 第三周 18 题

1. 头文件与源文件的职责是什么？
2. RAII 解决什么生命周期问题？
3. unique_ptr 与 shared_ptr 的所有权差别？
4. Frame 为什么 move-only？
5. cv::Mat 为什么可能浅共享？
6. clone 应发生在哪里？
7. 有界队列为何保护内存和延迟？
8. 两种 overflow policy 怎样选择？
9. mutex、condition variable、atomic 分别用于什么？
10. close 与 stop 有什么不同？
11. 视频 EOF 与摄像头空帧为何不同？
12. steady/system/source position 各是什么？
13. 四项质量信号怎样计算？
14. accepted 与 overall 的关系是什么？
15. 为什么保存最佳总体失败帧？
16. 为什么 Observe 使用 server stream？
17. deadline、context cancel、Cancel RPC 怎样汇合？
18. 哪些边界阻止你把当前实现称作高并发远程视觉服务？

### Week 3 Gate

按 [评分标准](ASSESSMENT-RUBRIC.md) 至少 80 分，并满足：

- 能从一条 dropped_frames 异常反推生产/消费速度关系。
- 能指出停止时“不误删旧帧”的代码顺序。
- 能解释关键帧 `clone()` 的所有权和性能权衡。
- 能运行成功与取消 gRPC 闭环。
- 60 分钟完成一个小指标/issue、对应测试和构建验证。

如果只会讲“C++ 性能高”“队列防阻塞”“gRPC 跨语言”，仍不合格；答案必须落到本项目的
对象、状态、线程和失败路径。
