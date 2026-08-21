"""
RealSight 第 8 章配置模块：把 TOML 文件和环境变量变成经过校验的应用设置。

这个文件的整体逻辑
------------------
工程初始化不能把路径、日志等级和治理预算散落在代码常量里。本模块先用 Python 3.12
标准库 ``tomllib`` 读取一份普通 TOML，再只允许一小组明确命名的环境变量覆盖它，
最后交给 Pydantic 校验。这样，本地开发可以读取文件，CI 或部署环境可以覆盖少量值，
而拼错字段会立即失败，不会悄悄使用错误默认值。

使用的技术栈
------------
- Python 3.12 ``tomllib``：结构化解析 TOML，不使用手工字符串切割。
- ``pathlib``：以跨平台方式解析数据目录和配置文件相对路径。
- Pydantic v2：拒绝未知字段，检查日志等级、预算正数和 capability 集合。
- 环境变量：只支持 ``REALSIGHT_*`` 白名单，不把任意环境内容注入配置。

调用流程
--------
``load_settings(path, environ)``
    -> 读取并解析 TOML
    -> 检查顶层只包含 runtime/governance
    -> 应用明确允许的 REALSIGHT_* 覆盖
    -> AppSettings.model_validate()
    -> 把相对 data_dir 锚定到配置文件所在目录
    -> 返回不可变 AppSettings

边界
----
本章不读取模型密钥，不实现动态刷新，也不把 TOML 当作权限安全边界。敏感值应在后续
部署阶段进入密钥系统；治理策略仍必须由第 7 章中间件在执行时强制实施。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigurationError(ValueError):
    """配置文件无法读取、解析或包含未知结构时抛出的统一错误。"""


class RuntimeSettings(BaseModel):
    """进程级设置；当前只保留数据目录和日志等级。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Path("runtime-data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """允许用户输入小写，但模型内部始终保存统一的大写值。"""

        return value.upper() if isinstance(value, str) else value


class GovernanceSettings(BaseModel):
    """第 7 章治理策略的配置形状；本章只加载，不执行预算扣减。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1, max_length=128)
    allowed_capabilities: frozenset[str] = Field(min_length=1)
    max_commands: int = Field(ge=1)
    max_observations: int = Field(ge=1)
    max_external_attempts: int = Field(ge=1)
    max_cost_units: int = Field(ge=1)

    @field_validator("allowed_capabilities")
    @classmethod
    def capabilities_are_well_formed(
        cls, capabilities: frozenset[str]
    ) -> frozenset[str]:
        """capability 使用 namespace.name 形式，降低拼写和含义碰撞风险。"""

        for capability in capabilities:
            parts = capability.split(".")
            if len(parts) < 2 or any(not part.isidentifier() for part in parts):
                raise ValueError(
                    "capability must contain identifier segments separated by dots"
                )
        return capabilities


class AgentSettings(BaseModel):
    """第 13、14 章的主 Agent 配置；密钥始终不属于 TOML 文件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deterministic", "openai"] = "deterministic"
    openai_model: str = Field(default="gpt-5.6-terra", min_length=1, max_length=128)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    max_iterations: int = Field(default=12, ge=1, le=50)


class AppSettings(BaseModel):
    """RealSight Python 进程启动后使用的完整、不可变配置快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: RuntimeSettings
    agent: AgentSettings = Field(default_factory=AgentSettings)
    governance: GovernanceSettings


# 每个环境变量明确映射到一个配置位置和转换函数。
# 这种白名单虽然多写几行，却能避免“任意字符串被塞进任意字段”。
_ENVIRONMENT_OVERRIDES: dict[str, tuple[str, str, type[str] | type[int]]] = {
    "REALSIGHT_DATA_DIR": ("runtime", "data_dir", str),
    "REALSIGHT_LOG_LEVEL": ("runtime", "log_level", str),
    "REALSIGHT_AGENT_PROVIDER": ("agent", "provider", str),
    "REALSIGHT_OPENAI_MODEL": ("agent", "openai_model", str),
    "REALSIGHT_REASONING_EFFORT": ("agent", "reasoning_effort", str),
    "REALSIGHT_MAX_COMMANDS": ("governance", "max_commands", int),
    "REALSIGHT_MAX_OBSERVATIONS": ("governance", "max_observations", int),
    "REALSIGHT_MAX_EXTERNAL_ATTEMPTS": (
        "governance",
        "max_external_attempts",
        int,
    ),
    "REALSIGHT_MAX_COST_UNITS": ("governance", "max_cost_units", int),
}


def _read_toml(path: Path) -> dict[str, Any]:
    """读取 TOML，并把文件系统和解析异常转换成有上下文的配置错误。"""

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot load config {path}: {exc}") from exc

    if not isinstance(data, dict):  # pragma: no cover - tomllib 正常返回 dict
        raise ConfigurationError("config root must be a TOML table")
    return data


def _apply_environment_overrides(
    raw: dict[str, Any], environ: Mapping[str, str]
) -> dict[str, Any]:
    """复制配置并应用白名单覆盖，避免修改调用者传入的原始字典。"""

    merged: dict[str, Any] = {
        section: dict(values) if isinstance(values, dict) else values
        for section, values in raw.items()
    }
    for variable, (section, field, converter) in _ENVIRONMENT_OVERRIDES.items():
        if variable not in environ:
            continue
        try:
            converted = converter(environ[variable])
        except ValueError as exc:
            raise ConfigurationError(
                f"environment variable {variable} has an invalid value"
            ) from exc
        section_values = merged.setdefault(section, {})
        if not isinstance(section_values, dict):
            raise ConfigurationError(f"config section {section!r} must be a table")
        section_values[field] = converted
    return merged


def load_settings(
    path: str | Path,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    """加载配置快照，并相对配置文件解析数据目录。"""

    config_path = Path(path).expanduser().resolve()
    raw = _read_toml(config_path)
    merged = _apply_environment_overrides(
        raw, os.environ if environ is None else environ
    )
    settings = AppSettings.model_validate(merged)

    # 相对路径以配置文件所在目录为基准，而不是受当前工作目录影响。
    data_dir = settings.runtime.data_dir
    if not data_dir.is_absolute():
        data_dir = (config_path.parent / data_dir).resolve()
    runtime = settings.runtime.model_copy(update={"data_dir": data_dir})
    return settings.model_copy(update={"runtime": runtime})


__all__ = [
    "AppSettings",
    "AgentSettings",
    "ConfigurationError",
    "GovernanceSettings",
    "RuntimeSettings",
    "load_settings",
]
