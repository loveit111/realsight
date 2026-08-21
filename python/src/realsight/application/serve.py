"""
第 14 章 API 服务器启动入口。

文件逻辑
--------
这个入口读取已验证 TOML 设置，选择 deterministic 或 OpenAIPlanner，并以 SQLite
checkpoint 启动 FastAPI。OPENAI_API_KEY 只由 SDK 从环境读取；配置、日志和 API
状态都不会保存该密钥。默认没有自动摄像头/OCR，部署者需显式注入第 10、11 章的
真实适配器，教学回放则由 examples 使用。

技术栈
------
- argparse、pathlib；
- RealSight Pydantic 配置与 Planner 工厂；
- SQLite LangGraph checkpoint；
- Uvicorn ASGI 服务器和 FastAPI。

调用流程
--------
realsight-api --config ...
    -> load_settings -> planner_from_settings
    -> TaskService.with_sqlite -> create_app
    -> uvicorn.run -> HTTP/WebSocket 请求。

边界
----
仅绑定 127.0.0.1，避免将无认证的教学 API 暴露到网络。C++ 摄像头运行时不在容器中，
也不由这个文件启动。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from realsight.application.api import create_app
from realsight.application.task_service import TaskService
from realsight.config import load_settings
from realsight.workflow.planner import planner_from_settings


def _project_root() -> Path:
    """定位工程根目录，确保默认配置和教学资料路径不受当前工作目录影响。"""

    return Path(__file__).resolve().parents[4]


def main() -> int:
    """启动单进程本机 API，进程退出时关闭 SQLite 连接。"""

    parser = argparse.ArgumentParser(description="Run the local RealSight teaching API")
    parser.add_argument(
        "--config",
        type=Path,
        default=_project_root() / "config" / "realsight.example.toml",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = load_settings(args.config)
    planner = planner_from_settings(
        settings.agent.provider,
        settings.agent.openai_model,
        settings.agent.reasoning_effort,
    )
    dependencies = TaskService.default_dependencies(_project_root(), planner=planner)
    service = TaskService.with_sqlite(
        settings.runtime.data_dir / "realsight-checkpoints.sqlite3", dependencies
    )
    try:
        uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
