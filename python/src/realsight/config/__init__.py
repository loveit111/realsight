"""
RealSight 配置包公共入口。

整体逻辑
--------
应用代码只从这里导入 ``load_settings`` 和配置模型，不依赖 TOML 解析细节。

技术栈
------
Python 包转发、标准库 ``tomllib`` 与 Pydantic v2；具体实现位于 ``settings.py``。

调用流程
--------
配置文件/环境变量 -> ``settings.load_settings`` -> 不可变 ``AppSettings`` -> 应用启动。
"""

from realsight.config.settings import (
    AppSettings,
    ConfigurationError,
    GovernanceSettings,
    RuntimeSettings,
    load_settings,
)

__all__ = [
    "AppSettings",
    "ConfigurationError",
    "GovernanceSettings",
    "RuntimeSettings",
    "load_settings",
]
