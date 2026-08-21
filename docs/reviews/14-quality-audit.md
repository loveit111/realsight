# 第 14 章质量鉴定报告

## 鉴定结论

**通过。** HTTP、WebSocket、恢复、取消和离线回放在真实 FastAPI TestClient 中验证，
且没有把浏览器、图像或密钥越过既有边界。

| 维度 | 结论 | 依据 |
|---|---|---|
| 逻辑闭环 | 通过 | API 只调用 TaskService，TaskService 再调用第 13 章工作流。 |
| 技术栈说明 | 通过 | 解释 FastAPI、Pydantic、to_thread、WebSocket、SQLite 和 Docker 的职责与限制。 |
| 小 Demo | 通过 | `ch14_api_replay.py` 经 HTTP、WebSocket、resume 到 `CH14_DEMO_OK`。 |
| 代码验证 | 通过 | `test_ch14_api.py` 两项测试通过。 |
| 前后桥接 | 通过 | 消费第 13 章状态/事件/恢复接口，形成 14 章最终 API 闭环。 |

## 审计要点

1. WebSocket 使用 checkpoint 轮询和 `RunEvent.sequence` 补发，适合单进程教学服务；文档
   明确标记多实例事件分发为未实现项。
2. `asyncio.to_thread` 防止同步图阻塞 FastAPI 事件循环，但不替代第 7 章的超时和资源治理。
3. API DTO 先验证 `kind` 和字段，再转换为工作流 ResumePayload；图内还会二次检查
   request ID、视角和 target。
4. 取消端点能转发 provider 的取消请求并令旧 resume 失败；MVP 未承诺分布式强制中断。
5. Docker 只包含 Python 服务和 SQLite 数据目录，符合 C++ 硬件运行时保持本机的边界。

## 保留风险

- API 无认证、TLS、CORS 策略和速率限制，只应在本机学习使用。
- 默认服务器没有真实 OCR/VLM；未配置时会产生明确 gap，而不是伪造标签 Evidence。
- OpenAI 真实调用必须由用户显式提供 Key；默认测试只验证工具约束，不承担网络质量和成本。
