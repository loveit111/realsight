# RealSight 真实设备评测手册

## 目的与安全底线

这套评测回答三件事：C++ 是否挑到可读关键帧、OCR/Evidence 是否正确抽取字段、规则是否
在缺失或冲突时保持保守。它不证明 USB PD 电气协商、线缆能力或长期充电安全。

最低准入条件：人工真值为 `insufficient_evidence` 或 `evidence_conflict` 时，系统输出
`conditions_met` 的次数必须为 0。若不为 0，不能把项目作为核心简历经历。

## 固定 30 样本矩阵

| 条件 | 数量 | 拍摄控制 | 主要观察 |
|---|---:|---|---|
| normal | 5 | 正对、对焦、普通室内光 | 基准 |
| tilted | 4 | 左右/上下倾斜但仍完整 | 透视鲁棒性 |
| mild_glare | 4 | 小范围反光不遮关键字段 | 质量与 OCR |
| strong_glare | 4 | 反光覆盖功率或协议 | 是否拒绝/请求补充 |
| blurred | 4 | 轻微移动或失焦 | 清晰度阈值 |
| underexposed | 3 | 降低照明但保留轮廓 | 曝光阈值 |
| partially_occluded | 3 | 分别遮挡功率、协议、输出档位 | 缺证据行为 |
| conflicting_print | 3 | 受控打印样本中功率/档位矛盾 | 冲突优先级 |
| 合计 | 30 | 每项只改变主要变量 | 不挑选“最好看的”结果 |

建议先固定充电器、笔记本型号、相机、分辨率和光源，再按预先编号采集。失败样本不得
删除重拍后只保留成功版本；可追加重拍，但两个记录都要保留。

## 人工真值

采集前由人读取标签和受控测试样本，填写：

- `truth.power_w`：标签明确标注或可由输出档位核对的最大功率。
- `truth.protocol`：只记录可见且人工确认的标准化协议名。
- `truth.verdict`：将同一笔记本规格输入现有简化规则后的期望类别。

真值不能复制 OCR 输出。真实厂商资料需要保存文档 ID、版本和核验日期；本月仍使用教学
规格夹具时，报告必须明确这一限制。

## JSONL 记录

复制 `test-data/device-evaluation/manifest.example.jsonl` 为 `manifest.jsonl`，每行替换为
一个真实样本。示例行是格式演示，绝不能进入报告。主要字段：

| 区块 | 必填内容 |
|---|---|
| `artifact` | 原始视频或关键帧相对路径；公开时可仅保留授权样本/哈希 |
| `truth` | 功率、协议和人工期望 verdict |
| `perception` | accepted、四类质量信号、issues、produced/consumed/dropped |
| `ocr` | 原始文字行、平均置信度、provider 和模型版本 |
| `evidence` | 最终字段集合与冲突字段集合 |
| `outcome` | 实际抽取功率/协议和规则 verdict；无结果为 `null` |
| `latency` | capture/perception/OCR/workflow/end-to-end 毫秒 |
| `notes` | 设备、异常和人为操作说明，不写密钥或个人信息 |

输入由严格 Pydantic 契约校验；未知字段、错误枚举、0～1 外质量分或重复 sample ID 会被
拒绝。

## 执行顺序

1. 记录相机、充电器、笔记本、代码 commit、配置和 OCR 版本。
2. 每个样本开始前清空本次任务 ID，保留原始工件。
3. 保存 C++ 输出的质量和帧统计。
4. 保存 OCR 原文，不只保存解析后的功率。
5. 保存 Evidence 字段、冲突和最终 verdict。
6. 用同一计时边界记录各阶段；不要混用 CPU 时间和墙钟时间。
7. 完成 30 行后运行严格评测脚本，不手工修改生成报告。

```powershell
uv run --locked python scripts/evaluate_device_dataset.py `
  test-data/device-evaluation/manifest.jsonl `
  --json-output runtime-data/device-report.json `
  --markdown-output runtime-data/device-report.md
```

## 指标口径

- 关键帧接受率：`accepted / 全部样本`。高并不必然好；困难图一律接受可能更危险。
- 功率准确率：预测值与真值差不超过 0.5W 的样本数 / 全部样本。
- 协议准确率：大小写和首尾空白归一化后的精确匹配数 / 全部样本。
- 端到端准确率：实际 verdict 与人工期望 verdict 完全相同的样本数 / 全部样本。
- 错误正结论：实际为 `conditions_met`、真值却不是该类别的样本数。
- p50/p95：各阶段真实毫秒记录的线性插值百分位。
- 丢帧率：总 dropped / 总 produced；同时报告三个原始总数。

## 失败样例复盘模板

每种主要失败至少公开一个经过脱敏的样例：

```text
sample_id:
输入条件与工件:
C++ quality/issues:
OCR 原文与置信度:
形成/缺失/冲突 Evidence:
实际 verdict / 真值:
故障首先出现在哪个边界:
是数据、阈值、契约、实现还是已知能力限制:
修复与回归测试:
仍未解决的风险:
```

不要只展示成功截图。能解释失败、阻止错误正结论并给出复现步骤，才是这份数据对简历的
真正价值。
