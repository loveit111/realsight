"""
第 7 章 Demo：用确定性治理中间件控制权限、并发、预算、超时、重试和审计。

这个文件做什么
------------
第 6 章让 LangGraph thread、Evidence Ledger 和图片产物能够跨进程保存，但“能够恢复”
并不等于“恢复后一定受控”。如果两个请求同时消费一个 interrupt、Agent 不断请求新视角、
服务超时后无限重试，或者未授权工具直接执行，持久化只会把失控过程保存得更完整。

本文件实现一个不依赖 LLM 的 GovernanceMiddleware。所有未来的模型调用、工具调用、
观察请求或规则执行都可以作为异步 operation 交给它。中间件按固定顺序执行：
1. 检查 capability 是否位于白名单。
2. 获取同一 thread 的 SQLite 租约，拒绝并发命令。
3. 用 command_id + 请求哈希判断新命令、幂等重放或 ID 冲突。
4. 原子预留逻辑命令/观察预算；每次外部尝试再预留调用次数和成本单位。
5. 用 asyncio.timeout 限制单次尝试，只重试显式瞬态错误与超时。
6. 成功、失败、取消后更新持久命令状态并在 finally 中释放租约。
7. 把允许、拒绝、重试、超时、预算耗尽和清理写入 append-only 审计日志。

使用的技术栈
------------
- Python 3.12+ asyncio：异步 operation、timeout、Task、Event、取消传播和退避等待。
- Python 标准库 sqlite3：持久预算、命令幂等表、thread lease 和审计事件。
- SQLite BEGIN IMMEDIATE：把“检查预算 + 扣减预算 + 写命令”放进同一写事务。
- Pydantic v2：校验 GovernancePolicy、GovernedCommand、结果和审计事件。
- hashlib SHA-256：对 canonical command JSON 计算请求指纹，阻止同 ID 换参数。
- uuid/os/time：生成本次执行 owner token，并管理带过期时间的本机租约。

Demo 调用流程
------------
python examples/ch07_governance_middleware.py demo
    -> 创建 governance.sqlite3 和一份不可变 GovernancePolicy
    -> 未授权 admin.delete 在 operation 运行前被拒绝，预算不扣减
    -> flaky observation 前两次抛瞬态错误，第三次成功
    -> 同 command_id 重放直接返回缓存结果，服务调用数与预算不变
    -> slow tool 持有 thread lease 时，第二个命令得到 ThreadBusy
    -> hanging tool 每次被 timeout 取消，三次后 RetryExhausted
    -> 第二次 observation 因观察预算耗尽而不执行
    -> costly tool 已登记逻辑命令，但因成本预算不足在外部调用前失败
    -> 输出预算快照、命令状态、底层服务计数与审计事件计数

关键边界
--------
- 中间件是确定性运行治理，不负责 Agent 规划、视觉识别或 USB-C 业务判断。
- “成本单位”是教学用整数配额，不是假装精确的人民币/美元账单。
- 自动重试只覆盖 TransientOperationError 和 TimeoutError；业务校验错误不会重试。
- asyncio.timeout 会取消协程，但服务必须合作传播 CancelledError 并清理资源。
- SQLite lease 是单机 MVP；没有 heartbeat/fencing token，不能宣称分布式锁。
- 命令幂等只能阻止已完成命令再次执行。真实有副作用的下游也必须接收 command_id。

阅读建议
--------
先读 GovernancePolicy 与 GovernedCommand，再读 GovernanceRepository 的四组方法：
policy/lease、claim、reserve_attempt、finalize/audit；随后读 GovernanceMiddleware.execute
的固定执行顺序，最后阅读确定性服务替身和 run_demo_async。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StrictInt,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


# 直接运行 examples 下的文件时，把项目根加入导入路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.models import (  # noqa: E402
    Identifier,
    NonEmptyText,
    StrictContract,
    utc_now,
)


# 这些可复用类型把治理参数限制集中起来，避免每个字段重复写正则和范围。
Capability = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
    ),
]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
JSON_ADAPTER = TypeAdapter(JsonValue)


class CommandCategory(str, Enum):
    """预算按操作性质分类；观察命令还会消耗稀缺的 observation 配额。"""

    OBSERVATION = "observation"
    TOOL = "tool"
    MODEL = "model"
    RULE = "rule"


class CommandStatus(str, Enum):
    """持久命令的终态不会被同 command_id 静默改写。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditEventType(str, Enum):
    """治理层可审计事件；它们描述运行事实，不替代领域 RunEvent。"""

    PERMISSION_DENIED = "permission_denied"
    LEASE_ACQUIRED = "lease_acquired"
    THREAD_BUSY = "thread_busy"
    LEASE_LOST = "lease_lost"
    LEASE_RELEASED = "lease_released"
    COMMAND_CLAIMED = "command_claimed"
    STALE_COMMAND_RECOVERED = "stale_command_recovered"
    IDEMPOTENCY_REPLAYED = "idempotency_replayed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PREVIOUS_FAILURE_REPLAYED = "previous_failure_replayed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_TIMED_OUT = "attempt_timed_out"
    RETRY_SCHEDULED = "retry_scheduled"
    COMMAND_SUCCEEDED = "command_succeeded"
    COMMAND_FAILED = "command_failed"
    COMMAND_CANCELLED = "command_cancelled"


class GovernancePolicy(StrictContract):
    """一个 session 的不可变治理策略；修改策略必须创建新 policy/session 版本。"""

    schema_version: Literal[1] = 1
    policy_id: Identifier
    allowed_capabilities: frozenset[Capability]
    max_commands: PositiveInt
    max_observations: NonNegativeInt
    max_external_attempts: PositiveInt
    max_cost_units: NonNegativeInt
    max_attempts_per_command: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    timeout_ms: Annotated[StrictInt, Field(ge=10, le=30_000)] = 1_000
    base_backoff_ms: Annotated[StrictInt, Field(ge=0, le=10_000)] = 50
    lease_ms: Annotated[StrictInt, Field(ge=100, le=300_000)] = 10_000

    @field_validator("allowed_capabilities")
    @classmethod
    def capability_allowlist_is_not_empty(
        cls,
        capabilities: frozenset[str],
    ) -> frozenset[str]:
        """空白名单会让 Demo 所有命令都失败，通常说明策略配置遗漏。"""

        if not capabilities:
            raise ValueError("allowed_capabilities must not be empty")
        return capabilities

    @model_validator(mode="after")
    def lease_outlives_one_attempt(self) -> GovernancePolicy:
        """租约至少覆盖一次超时与一次基础退避，降低正常重试时误过期的概率。"""

        if self.lease_ms <= self.timeout_ms + self.base_backoff_ms:
            raise ValueError("lease_ms must exceed timeout_ms + base_backoff_ms")
        return self


class GovernedCommand(StrictContract):
    """交给中间件执行的稳定命令；callable 和连接不属于可持久命令数据。"""

    schema_version: Literal[1] = 1
    command_id: Identifier
    session_id: Identifier
    thread_id: Identifier
    capability: Capability
    category: CommandCategory
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    cost_units_per_attempt: NonNegativeInt = 0

    @model_validator(mode="after")
    def thread_matches_session(self) -> GovernedCommand:
        """沿用课程 MVP：一个 TaskSession 只绑定一个同名 LangGraph thread。"""

        if self.thread_id != self.session_id:
            raise ValueError("thread_id must equal session_id in the course MVP")
        return self


class GovernedExecutionResult(StrictContract):
    """中间件返回的结构化结果；cached=True 表示没有再次调用外部 operation。"""

    command_id: Identifier
    value: JsonValue
    cached: bool
    attempts: NonNegativeInt


class BudgetSnapshot(StrictContract):
    """当前会话预算快照，显示上限与已消费值。"""

    session_id: Identifier
    max_commands: PositiveInt
    consumed_commands: NonNegativeInt
    max_observations: NonNegativeInt
    consumed_observations: NonNegativeInt
    max_external_attempts: PositiveInt
    consumed_external_attempts: NonNegativeInt
    max_cost_units: NonNegativeInt
    consumed_cost_units: NonNegativeInt


class GovernanceAuditEvent(StrictContract):
    """从 append-only audit_events 表读取的一条治理事实。"""

    audit_id: PositiveInt
    session_id: Identifier
    sequence: PositiveInt
    thread_id: Identifier
    command_id: Identifier | None = None
    event_type: AuditEventType
    data: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: AwareDatetime


class GovernanceError(RuntimeError):
    """第 7 章所有可分类治理错误的共同基类。"""


class PermissionDenied(GovernanceError):
    """capability 不在当前 session 白名单中。"""


class ThreadBusy(GovernanceError):
    """另一个 owner 正持有同一 thread 的未过期租约。"""


class LeaseLost(GovernanceError):
    """执行期间租约已过期或被其他 owner 接管，当前执行不得继续提交。"""


class IdempotencyConflict(GovernanceError):
    """相同 command_id 被用于不同请求内容。"""


class PreviouslyFailed(GovernanceError):
    """相同命令已经进入失败/取消终态，不自动重新执行副作用。"""


class BudgetExceeded(GovernanceError):
    """逻辑命令、观察、外部尝试或成本单位超过策略上限。"""


class RetryExhausted(GovernanceError):
    """显式可重试错误已达到 max_attempts_per_command。"""


class TransientOperationError(RuntimeError):
    """服务明确声明的瞬态错误；只有这类业务异常可以自动重试。"""


class ClaimDisposition(str, Enum):
    """Repository 对 command_id 的持久判定，供中间件选择后续路径。"""

    NEW = "new"
    RECOVERED = "recovered"
    CACHED = "cached"
    CONFLICT = "conflict"
    PREVIOUSLY_FAILED = "previously_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class ClaimResult:
    """claim_command 的内部结果；不直接暴露到 API。"""

    disposition: ClaimDisposition
    attempts: int = 0
    result: JsonValue | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AttemptReservation:
    """一次外部调用预算的原子预留结果。"""

    allowed: bool
    attempt_number: int
    reason: str | None = None


@dataclass(frozen=True)
class OperationAttempt:
    """传给外部 operation 的只读上下文；下游可使用 command_id 做幂等键。"""

    command: GovernedCommand
    attempt_number: int


GovernedOperation = Callable[[OperationAttempt], Awaitable[JsonValue]]


def canonical_json(value: Any) -> str:
    """生成稳定 JSON，确保 dict 键顺序不同不会改变请求指纹。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def command_fingerprint(command: GovernedCommand) -> str:
    """对完整版本化命令计算 SHA-256；同 ID 换 capability/payload 都会冲突。"""

    serialized = canonical_json(command.model_dump(mode="json"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def canonical_policy_json(policy: GovernancePolicy) -> str:
    """稳定序列化无序权限集合，避免不同进程因 set 遍历顺序误判策略变化。"""

    payload = policy.model_dump(mode="json")
    payload["allowed_capabilities"] = sorted(policy.allowed_capabilities)
    return canonical_json(payload)


class GovernanceRepository:
    """持久治理仓库：策略、预算、命令、thread lease 和审计日志的唯一写入口。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """打开显式 autocommit 连接；每个具体方法自行决定读事务或 IMMEDIATE 写事务。"""

        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 提前取得写事务，避免预算检查后被另一写者插队。"""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def initialize_schema(self) -> None:
        """创建第 7 章 schema；数据均为结构化 TEXT/INTEGER，不保存 Python callable。"""

        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS governance_sessions (
                    session_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    policy_id TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    max_commands INTEGER NOT NULL,
                    consumed_commands INTEGER NOT NULL DEFAULT 0,
                    max_observations INTEGER NOT NULL,
                    consumed_observations INTEGER NOT NULL DEFAULT 0,
                    max_external_attempts INTEGER NOT NULL,
                    consumed_external_attempts INTEGER NOT NULL DEFAULT 0,
                    max_cost_units INTEGER NOT NULL,
                    consumed_cost_units INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    CHECK (consumed_commands BETWEEN 0 AND max_commands),
                    CHECK (consumed_observations BETWEEN 0 AND max_observations),
                    CHECK (consumed_external_attempts BETWEEN 0 AND max_external_attempts),
                    CHECK (consumed_cost_units BETWEEN 0 AND max_cost_units)
                );

                CREATE TABLE IF NOT EXISTS thread_leases (
                    thread_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    acquired_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES governance_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS commands (
                    session_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, command_id),
                    FOREIGN KEY (session_id) REFERENCES governance_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    thread_id TEXT NOT NULL,
                    command_id TEXT,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (session_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES governance_sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_commands_session_status
                    ON commands(session_id, status);
                CREATE INDEX IF NOT EXISTS idx_audit_session_command
                    ON audit_events(session_id, command_id, sequence);
                """
            )
            connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        command_id: str | None,
        event_type: AuditEventType,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        """在调用者事务内追加严格递增审计事件，保证状态修改与日志共同提交。"""

        next_sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM audit_events WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO audit_events
                (session_id, sequence, thread_id, command_id,
                 event_type, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                next_sequence,
                thread_id,
                command_id,
                event_type.value,
                canonical_json({} if data is None else data),
                utc_now().isoformat(),
            ),
        )

    def append_event(
        self,
        *,
        session_id: str,
        thread_id: str,
        command_id: str | None,
        event_type: AuditEventType,
        data: dict[str, JsonValue] | None = None,
    ) -> None:
        """为不伴随其他表修改的拒绝/重试事实开启独立短事务。"""

        with self.immediate_transaction() as connection:
            self._append_event(
                connection,
                session_id=session_id,
                thread_id=thread_id,
                command_id=command_id,
                event_type=event_type,
                data=data,
            )

    def register_session(
        self,
        *,
        session_id: str,
        thread_id: str,
        policy: GovernancePolicy,
    ) -> None:
        """幂等登记不可变策略；同 session 改 policy 或 thread 会立即失败。"""

        policy_json = canonical_policy_json(policy)
        policy_hash = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                """
                SELECT thread_id, policy_hash FROM governance_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["thread_id"] != thread_id
                    or existing["policy_hash"] != policy_hash
                ):
                    raise IdempotencyConflict(
                        "session governance policy or thread cannot change in place"
                    )
                return
            connection.execute(
                """
                INSERT INTO governance_sessions (
                    session_id, thread_id, policy_id, policy_hash, policy_json,
                    max_commands, max_observations, max_external_attempts,
                    max_cost_units, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    thread_id,
                    policy.policy_id,
                    policy_hash,
                    policy_json,
                    policy.max_commands,
                    policy.max_observations,
                    policy.max_external_attempts,
                    policy.max_cost_units,
                    utc_now().isoformat(),
                ),
            )

    def acquire_lease(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
        lease_ms: int,
    ) -> bool:
        """获取或接管已过期 thread lease；活动 lease 会记录 THREAD_BUSY 并拒绝。"""

        now = time.time()
        expires_at = now + lease_ms / 1000.0
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                "SELECT owner_token, expires_at FROM thread_leases WHERE thread_id = ?",
                (command.thread_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["owner_token"] != owner_token
                and float(existing["expires_at"]) > now
            ):
                self._append_event(
                    connection,
                    session_id=command.session_id,
                    thread_id=command.thread_id,
                    command_id=command.command_id,
                    event_type=AuditEventType.THREAD_BUSY,
                    data={"lease_remaining_ms": int((existing["expires_at"] - now) * 1000)},
                )
                return False

            replaced_expired = existing is not None and existing["owner_token"] != owner_token
            connection.execute(
                """
                INSERT INTO thread_leases
                    (thread_id, session_id, owner_token, expires_at, acquired_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    owner_token = excluded.owner_token,
                    expires_at = excluded.expires_at,
                    acquired_at = excluded.acquired_at
                """,
                (
                    command.thread_id,
                    command.session_id,
                    owner_token,
                    expires_at,
                    utc_now().isoformat(),
                ),
            )
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.LEASE_ACQUIRED,
                data={"replaced_expired_lease": replaced_expired},
            )
            return True

    def renew_lease(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
        lease_ms: int,
    ) -> None:
        """每次外部尝试前续租；owner 不匹配时拒绝继续提交结果。"""

        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE thread_leases SET expires_at = ?
                WHERE thread_id = ? AND session_id = ? AND owner_token = ?
                """,
                (
                    time.time() + lease_ms / 1000.0,
                    command.thread_id,
                    command.session_id,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("thread lease is no longer owned by this execution")

    def release_lease(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
    ) -> bool:
        """只允许 owner 释放自己的 lease；释放与审计事件在同一事务。"""

        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM thread_leases
                WHERE thread_id = ? AND session_id = ? AND owner_token = ?
                """,
                (command.thread_id, command.session_id, owner_token),
            )
            if cursor.rowcount != 1:
                return False
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.LEASE_RELEASED,
            )
            return True

    def claim_command(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
    ) -> ClaimResult:
        """原子判断幂等状态，并只为全新命令预留逻辑命令/观察预算。"""

        request_hash = command_fingerprint(command)
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                """
                SELECT request_hash, status, owner_token, attempt_count,
                       result_json, error_type, error_message
                FROM commands WHERE session_id = ? AND command_id = ?
                """,
                (command.session_id, command.command_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    self._append_event(
                        connection,
                        session_id=command.session_id,
                        thread_id=command.thread_id,
                        command_id=command.command_id,
                        event_type=AuditEventType.IDEMPOTENCY_CONFLICT,
                    )
                    return ClaimResult(ClaimDisposition.CONFLICT)

                status = CommandStatus(existing["status"])
                attempts = int(existing["attempt_count"])
                if status == CommandStatus.SUCCEEDED:
                    self._append_event(
                        connection,
                        session_id=command.session_id,
                        thread_id=command.thread_id,
                        command_id=command.command_id,
                        event_type=AuditEventType.IDEMPOTENCY_REPLAYED,
                        data={"attempts": attempts},
                    )
                    return ClaimResult(
                        ClaimDisposition.CACHED,
                        attempts=attempts,
                        result=json.loads(existing["result_json"]),
                    )
                if status in {CommandStatus.FAILED, CommandStatus.CANCELLED}:
                    self._append_event(
                        connection,
                        session_id=command.session_id,
                        thread_id=command.thread_id,
                        command_id=command.command_id,
                        event_type=AuditEventType.PREVIOUS_FAILURE_REPLAYED,
                        data={"status": status.value, "error_type": existing["error_type"]},
                    )
                    return ClaimResult(
                        ClaimDisposition.PREVIOUSLY_FAILED,
                        attempts=attempts,
                        reason=existing["error_message"] or status.value,
                    )

                # 只有旧 lease 已过期、新 owner 成功接管后，RUNNING 命令才会走到这里。
                connection.execute(
                    """
                    UPDATE commands SET owner_token = ?, updated_at = ?
                    WHERE session_id = ? AND command_id = ?
                    """,
                    (
                        owner_token,
                        utc_now().isoformat(),
                        command.session_id,
                        command.command_id,
                    ),
                )
                self._append_event(
                    connection,
                    session_id=command.session_id,
                    thread_id=command.thread_id,
                    command_id=command.command_id,
                    event_type=AuditEventType.STALE_COMMAND_RECOVERED,
                    data={"previous_attempts": attempts},
                )
                return ClaimResult(ClaimDisposition.RECOVERED, attempts=attempts)

            budget = connection.execute(
                """
                SELECT max_commands, consumed_commands,
                       max_observations, consumed_observations
                FROM governance_sessions WHERE session_id = ?
                """,
                (command.session_id,),
            ).fetchone()
            if budget is None:
                raise GovernanceError("governance session must be registered first")

            reason: str | None = None
            if budget["consumed_commands"] + 1 > budget["max_commands"]:
                reason = "command budget exhausted"
            elif (
                command.category == CommandCategory.OBSERVATION
                and budget["consumed_observations"] + 1 > budget["max_observations"]
            ):
                reason = "observation budget exhausted"
            if reason is not None:
                self._append_event(
                    connection,
                    session_id=command.session_id,
                    thread_id=command.thread_id,
                    command_id=command.command_id,
                    event_type=AuditEventType.BUDGET_EXHAUSTED,
                    data={"stage": "command_claim", "reason": reason},
                )
                return ClaimResult(
                    ClaimDisposition.BUDGET_EXHAUSTED,
                    reason=reason,
                )

            observation_increment = int(
                command.category == CommandCategory.OBSERVATION
            )
            connection.execute(
                """
                UPDATE governance_sessions SET
                    consumed_commands = consumed_commands + 1,
                    consumed_observations = consumed_observations + ?
                WHERE session_id = ?
                """,
                (observation_increment, command.session_id),
            )
            now = utc_now().isoformat()
            connection.execute(
                """
                INSERT INTO commands (
                    session_id, command_id, thread_id, request_hash,
                    capability, category, status, owner_token,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.session_id,
                    command.command_id,
                    command.thread_id,
                    request_hash,
                    command.capability,
                    command.category.value,
                    CommandStatus.RUNNING.value,
                    owner_token,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.COMMAND_CLAIMED,
                data={
                    "category": command.category.value,
                    "capability": command.capability,
                    "observation_charged": bool(observation_increment),
                },
            )
            return ClaimResult(ClaimDisposition.NEW)

    def reserve_attempt(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
    ) -> AttemptReservation:
        """原子扣减一次外部尝试和成本；预算不足时 operation 尚未被调用。"""

        with self.immediate_transaction() as connection:
            self._require_live_lease(connection, command, owner_token)
            command_row = connection.execute(
                """
                SELECT status, owner_token, attempt_count FROM commands
                WHERE session_id = ? AND command_id = ?
                """,
                (command.session_id, command.command_id),
            ).fetchone()
            if command_row is None:
                raise GovernanceError("command must be claimed before reserving attempt")
            if (
                command_row["status"] != CommandStatus.RUNNING.value
                or command_row["owner_token"] != owner_token
            ):
                raise LeaseLost("running command is no longer owned by this execution")

            budget = connection.execute(
                """
                SELECT max_external_attempts, consumed_external_attempts,
                       max_cost_units, consumed_cost_units
                FROM governance_sessions WHERE session_id = ?
                """,
                (command.session_id,),
            ).fetchone()
            next_attempt = int(command_row["attempt_count"]) + 1
            reason: str | None = None
            if (
                budget["consumed_external_attempts"] + 1
                > budget["max_external_attempts"]
            ):
                reason = "external attempt budget exhausted"
            elif (
                budget["consumed_cost_units"] + command.cost_units_per_attempt
                > budget["max_cost_units"]
            ):
                reason = "cost budget exhausted"

            if reason is not None:
                self._append_event(
                    connection,
                    session_id=command.session_id,
                    thread_id=command.thread_id,
                    command_id=command.command_id,
                    event_type=AuditEventType.BUDGET_EXHAUSTED,
                    data={"stage": "attempt_reservation", "reason": reason},
                )
                return AttemptReservation(False, next_attempt, reason)

            connection.execute(
                """
                UPDATE governance_sessions SET
                    consumed_external_attempts = consumed_external_attempts + 1,
                    consumed_cost_units = consumed_cost_units + ?
                WHERE session_id = ?
                """,
                (command.cost_units_per_attempt, command.session_id),
            )
            connection.execute(
                """
                UPDATE commands SET attempt_count = attempt_count + 1, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_token = ?
                """,
                (
                    utc_now().isoformat(),
                    command.session_id,
                    command.command_id,
                    owner_token,
                ),
            )
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.ATTEMPT_STARTED,
                data={
                    "attempt": next_attempt,
                    "cost_units": command.cost_units_per_attempt,
                },
            )
            return AttemptReservation(True, next_attempt)

    @staticmethod
    def _require_live_lease(
        connection: sqlite3.Connection,
        command: GovernedCommand,
        owner_token: str,
    ) -> None:
        """在提交命令状态前执行租约栅栏检查，拒绝过期 owner 的晚到结果。"""

        lease = connection.execute(
            """
            SELECT owner_token, expires_at FROM thread_leases
            WHERE thread_id = ? AND session_id = ?
            """,
            (command.thread_id, command.session_id),
        ).fetchone()
        if (
            lease is None
            or lease["owner_token"] != owner_token
            or float(lease["expires_at"]) <= time.time()
        ):
            raise LeaseLost("thread lease expired or belongs to another execution")

    def record_attempt_outcome(
        self,
        command: GovernedCommand,
        *,
        event_type: AuditEventType,
        attempt_number: int,
        error_type: str,
        duration_ms: int,
    ) -> None:
        """记录一次失败/超时尝试；命令是否继续由中间件重试策略决定。"""

        if event_type not in {
            AuditEventType.ATTEMPT_FAILED,
            AuditEventType.ATTEMPT_TIMED_OUT,
        }:
            raise ValueError("attempt outcome event type is invalid")
        self.append_event(
            session_id=command.session_id,
            thread_id=command.thread_id,
            command_id=command.command_id,
            event_type=event_type,
            data={
                "attempt": attempt_number,
                "error_type": error_type,
                "duration_ms": duration_ms,
            },
        )

    def record_retry(
        self,
        command: GovernedCommand,
        *,
        attempt_number: int,
        backoff_ms: int,
        reason: str,
    ) -> None:
        """在真正 sleep 前记录重试计划，便于区分“允许重试”和“已经开始下一次”。"""

        self.append_event(
            session_id=command.session_id,
            thread_id=command.thread_id,
            command_id=command.command_id,
            event_type=AuditEventType.RETRY_SCHEDULED,
            data={
                "after_attempt": attempt_number,
                "backoff_ms": backoff_ms,
                "reason": reason,
            },
        )

    def mark_succeeded(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
        result: JsonValue,
        duration_ms: int,
    ) -> None:
        """把 RUNNING 命令原子更新为 SUCCEEDED，并保存可 JSON 化结果。"""

        result_json = canonical_json(result)
        with self.immediate_transaction() as connection:
            self._require_live_lease(connection, command, owner_token)
            cursor = connection.execute(
                """
                UPDATE commands SET status = ?, result_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                  AND status = ? AND owner_token = ?
                """,
                (
                    CommandStatus.SUCCEEDED.value,
                    result_json,
                    utc_now().isoformat(),
                    command.session_id,
                    command.command_id,
                    CommandStatus.RUNNING.value,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("cannot commit success without owning running command")
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.COMMAND_SUCCEEDED,
                data={"duration_ms": duration_ms},
            )

    def mark_failed(
        self,
        command: GovernedCommand,
        *,
        owner_token: str,
        status: Literal[CommandStatus.FAILED, CommandStatus.CANCELLED],
        error_type: str,
        error_message: str,
    ) -> None:
        """把命令写入不可重放的失败/取消终态，并追加对应审计事件。"""

        event_type = (
            AuditEventType.COMMAND_CANCELLED
            if status == CommandStatus.CANCELLED
            else AuditEventType.COMMAND_FAILED
        )
        with self.immediate_transaction() as connection:
            self._require_live_lease(connection, command, owner_token)
            cursor = connection.execute(
                """
                UPDATE commands SET
                    status = ?, error_type = ?, error_message = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                  AND status = ? AND owner_token = ?
                """,
                (
                    status.value,
                    error_type,
                    error_message[:1024],
                    utc_now().isoformat(),
                    command.session_id,
                    command.command_id,
                    CommandStatus.RUNNING.value,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("cannot commit failure without owning running command")
            self._append_event(
                connection,
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=event_type,
                data={"error_type": error_type, "message": error_message[:256]},
            )

    def budget_snapshot(self, session_id: str) -> BudgetSnapshot:
        """读取当前预算；不存在的 session 不能返回全零假数据。"""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM governance_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise GovernanceError(f"unknown governance session: {session_id}")
        return BudgetSnapshot(
            session_id=session_id,
            max_commands=row["max_commands"],
            consumed_commands=row["consumed_commands"],
            max_observations=row["max_observations"],
            consumed_observations=row["consumed_observations"],
            max_external_attempts=row["max_external_attempts"],
            consumed_external_attempts=row["consumed_external_attempts"],
            max_cost_units=row["max_cost_units"],
            consumed_cost_units=row["consumed_cost_units"],
        )

    def command_status(self, session_id: str, command_id: str) -> CommandStatus | None:
        """读取命令状态，供测试、恢复和第 14 章 API 查询。"""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT status FROM commands
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
        return None if row is None else CommandStatus(row["status"])

    def command_count(self, session_id: str) -> int:
        """返回真正被 claim 的持久命令数；拒绝、busy 和缓存重放不增加。"""

        with self.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM commands WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )

    def active_lease_count(self, thread_id: str) -> int:
        """检查 finally 是否释放 lease；过期但未清理的行仍会计数，便于暴露问题。"""

        with self.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM thread_leases WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()[0]
            )

    def audit_events(self, session_id: str) -> tuple[GovernanceAuditEvent, ...]:
        """按 session sequence 返回 append-only 治理事件。"""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE session_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        return tuple(
            GovernanceAuditEvent(
                audit_id=row["audit_id"],
                session_id=row["session_id"],
                sequence=row["sequence"],
                thread_id=row["thread_id"],
                command_id=row["command_id"],
                event_type=AuditEventType(row["event_type"]),
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        )


class GovernanceMiddleware:
    """围绕任意异步 operation 执行固定、可测试、与模型无关的治理策略。"""

    def __init__(
        self,
        repository: GovernanceRepository,
        policy: GovernancePolicy,
    ) -> None:
        self.repository = repository
        self.policy = policy

    async def execute(
        self,
        command: GovernedCommand,
        operation: GovernedOperation,
    ) -> GovernedExecutionResult:
        """按 permission -> lease -> idempotency -> budget -> timeout/retry 执行命令。"""

        self.repository.register_session(
            session_id=command.session_id,
            thread_id=command.thread_id,
            policy=self.policy,
        )

        # 权限检查必须发生在 callable 执行前；拒绝不会占用 thread lease 或预算。
        if command.capability not in self.policy.allowed_capabilities:
            self.repository.append_event(
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.PERMISSION_DENIED,
                data={"capability": command.capability},
            )
            raise PermissionDenied(
                f"capability is not allowed: {command.capability}"
            )

        owner_token = f"pid-{os.getpid()}-{uuid.uuid4().hex}"
        lease_acquired = self.repository.acquire_lease(
            command,
            owner_token=owner_token,
            lease_ms=self.policy.lease_ms,
        )
        if not lease_acquired:
            raise ThreadBusy(f"thread already has an active command: {command.thread_id}")

        active_command = False
        finalized = False
        command_started = time.perf_counter()
        try:
            claim = self.repository.claim_command(command, owner_token=owner_token)
            if claim.disposition == ClaimDisposition.CACHED:
                return GovernedExecutionResult(
                    command_id=command.command_id,
                    value=claim.result,
                    cached=True,
                    attempts=claim.attempts,
                )
            if claim.disposition == ClaimDisposition.CONFLICT:
                raise IdempotencyConflict(
                    f"command_id already represents another request: {command.command_id}"
                )
            if claim.disposition == ClaimDisposition.PREVIOUSLY_FAILED:
                raise PreviouslyFailed(
                    f"command already ended unsuccessfully: {claim.reason}"
                )
            if claim.disposition == ClaimDisposition.BUDGET_EXHAUSTED:
                raise BudgetExceeded(claim.reason or "command budget exhausted")

            active_command = True
            start_attempt = claim.attempts + 1
            if start_attempt > self.policy.max_attempts_per_command:
                message = "recovered command already exhausted its attempt limit"
                self.repository.mark_failed(
                    command,
                    owner_token=owner_token,
                    status=CommandStatus.FAILED,
                    error_type=RetryExhausted.__name__,
                    error_message=message,
                )
                finalized = True
                raise RetryExhausted(message)
            for requested_attempt in range(
                start_attempt,
                self.policy.max_attempts_per_command + 1,
            ):
                self.repository.renew_lease(
                    command,
                    owner_token=owner_token,
                    lease_ms=self.policy.lease_ms,
                )
                reservation = self.repository.reserve_attempt(
                    command,
                    owner_token=owner_token,
                )
                if not reservation.allowed:
                    message = reservation.reason or "attempt budget exhausted"
                    self.repository.mark_failed(
                        command,
                        owner_token=owner_token,
                        status=CommandStatus.FAILED,
                        error_type=BudgetExceeded.__name__,
                        error_message=message,
                    )
                    finalized = True
                    raise BudgetExceeded(message)

                # Repository 的持久 attempt_count 是最终编号；requested_attempt 只控制循环上限。
                attempt_number = reservation.attempt_number
                if attempt_number != requested_attempt:
                    raise GovernanceError("persistent attempt counter is inconsistent")
                attempt_started = time.perf_counter()
                try:
                    async with asyncio.timeout(self.policy.timeout_ms / 1000.0):
                        raw_result = await operation(
                            OperationAttempt(command, attempt_number)
                        )
                    result = JSON_ADAPTER.validate_python(raw_result)
                except TimeoutError as error:
                    duration_ms = int((time.perf_counter() - attempt_started) * 1000)
                    self.repository.record_attempt_outcome(
                        command,
                        event_type=AuditEventType.ATTEMPT_TIMED_OUT,
                        attempt_number=attempt_number,
                        error_type=type(error).__name__,
                        duration_ms=duration_ms,
                    )
                    if attempt_number >= self.policy.max_attempts_per_command:
                        message = f"timeout after {attempt_number} attempts"
                        self.repository.mark_failed(
                            command,
                            owner_token=owner_token,
                            status=CommandStatus.FAILED,
                            error_type=RetryExhausted.__name__,
                            error_message=message,
                        )
                        finalized = True
                        raise RetryExhausted(message) from error
                    await self._backoff(
                        command,
                        attempt_number=attempt_number,
                        reason="timeout",
                    )
                    continue
                except TransientOperationError as error:
                    duration_ms = int((time.perf_counter() - attempt_started) * 1000)
                    self.repository.record_attempt_outcome(
                        command,
                        event_type=AuditEventType.ATTEMPT_FAILED,
                        attempt_number=attempt_number,
                        error_type=type(error).__name__,
                        duration_ms=duration_ms,
                    )
                    if attempt_number >= self.policy.max_attempts_per_command:
                        message = f"transient error after {attempt_number} attempts"
                        self.repository.mark_failed(
                            command,
                            owner_token=owner_token,
                            status=CommandStatus.FAILED,
                            error_type=RetryExhausted.__name__,
                            error_message=message,
                        )
                        finalized = True
                        raise RetryExhausted(message) from error
                    await self._backoff(
                        command,
                        attempt_number=attempt_number,
                        reason=type(error).__name__,
                    )
                    continue
                except asyncio.CancelledError:
                    # 交给外层统一落 CANCELLED 终态，随后必须重新传播取消。
                    raise
                except Exception as error:
                    duration_ms = int((time.perf_counter() - attempt_started) * 1000)
                    self.repository.record_attempt_outcome(
                        command,
                        event_type=AuditEventType.ATTEMPT_FAILED,
                        attempt_number=attempt_number,
                        error_type=type(error).__name__,
                        duration_ms=duration_ms,
                    )
                    self.repository.mark_failed(
                        command,
                        owner_token=owner_token,
                        status=CommandStatus.FAILED,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    finalized = True
                    # 未标记为瞬态的异常不自动重试，原异常交给调用者分类。
                    raise

                total_duration_ms = int(
                    (time.perf_counter() - command_started) * 1000
                )
                self.repository.mark_succeeded(
                    command,
                    owner_token=owner_token,
                    result=result,
                    duration_ms=total_duration_ms,
                )
                finalized = True
                return GovernedExecutionResult(
                    command_id=command.command_id,
                    value=result,
                    cached=False,
                    attempts=attempt_number,
                )

            # 正常情况下，最后一次失败会在上方抛 RetryExhausted，不应到达这里。
            raise GovernanceError("command loop ended without a terminal result")
        except asyncio.CancelledError:
            if active_command and not finalized:
                try:
                    self.repository.mark_failed(
                        command,
                        owner_token=owner_token,
                        status=CommandStatus.CANCELLED,
                        error_type="CancelledError",
                        error_message="caller cancelled governed command",
                    )
                    finalized = True
                except LeaseLost:
                    # 不能用过期 owner 修改 command，但仍需保留本进程观察到的治理事实。
                    self.repository.append_event(
                        session_id=command.session_id,
                        thread_id=command.thread_id,
                        command_id=command.command_id,
                        event_type=AuditEventType.LEASE_LOST,
                        data={"stage": "cancellation_finalize"},
                    )
            raise
        except LeaseLost:
            self.repository.append_event(
                session_id=command.session_id,
                thread_id=command.thread_id,
                command_id=command.command_id,
                event_type=AuditEventType.LEASE_LOST,
                data={"stage": "command_execution"},
            )
            raise
        finally:
            # 即使命令被拒绝、超时、抛异常或取消，也不能把 thread 永久锁住。
            self.repository.release_lease(command, owner_token=owner_token)

    async def _backoff(
        self,
        command: GovernedCommand,
        *,
        attempt_number: int,
        reason: str,
    ) -> None:
        """记录并执行指数退避；取消发生在 sleep 中也会传播到 execute 外层。"""

        backoff_ms = self.policy.base_backoff_ms * (2 ** (attempt_number - 1))
        self.repository.record_retry(
            command,
            attempt_number=attempt_number,
            backoff_ms=backoff_ms,
            reason=reason,
        )
        await asyncio.sleep(backoff_ms / 1000.0)


class FlakyOperation:
    """前若干次显式瞬态失败，随后成功；用于证明有限重试和成本扣减。"""

    def __init__(self, failures_before_success: int) -> None:
        self.failures_before_success = failures_before_success
        self.calls = 0

    async def __call__(self, attempt: OperationAttempt) -> JsonValue:
        self.calls += 1
        await asyncio.sleep(0.002)
        if self.calls <= self.failures_before_success:
            raise TransientOperationError(f"camera warming up: {self.calls}")
        return {
            "observation_id": "observation-governed-001",
            "attempt": attempt.attempt_number,
        }


class ControlledOperation:
    """由 Event 控制结束时间，用于在 lease 活跃时发起第二个并发命令。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, attempt: OperationAttempt) -> JsonValue:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return {"rule_ready": True, "attempt": attempt.attempt_number}


class HangingOperation:
    """每次都超过 timeout，并在取消时清理；用于验证 timeout 真正向下取消。"""

    def __init__(self) -> None:
        self.calls = 0
        self.cancellations = 0

    async def __call__(self, attempt: OperationAttempt) -> JsonValue:
        self.calls += 1
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        return {"unexpected": True}


class CountingOperation:
    """立即成功并统计调用次数；被权限/预算/幂等拦截时 calls 必须保持不变。"""

    def __init__(self, value: JsonValue | None = None) -> None:
        self.value = {"ok": True} if value is None else value
        self.calls = 0

    async def __call__(self, attempt: OperationAttempt) -> JsonValue:
        self.calls += 1
        await asyncio.sleep(0)
        return self.value


def demo_policy() -> GovernancePolicy:
    """创建能依次展示重试、超时、观察上限和成本耗尽的确定性策略。"""

    return GovernancePolicy(
        policy_id="policy-ch07-demo-v1",
        allowed_capabilities=frozenset(
            {
                "perception.observe",
                "rules.evaluate",
                "spec.retrieve",
            }
        ),
        max_commands=4,
        max_observations=1,
        max_external_attempts=10,
        max_cost_units=6,
        max_attempts_per_command=3,
        timeout_ms=30,
        base_backoff_ms=2,
        lease_ms=2_000,
    )


def make_demo_command(
    command_id: str,
    *,
    capability: str,
    category: CommandCategory,
    cost_units: int,
    payload: dict[str, JsonValue] | None = None,
) -> GovernedCommand:
    """为同一课程 session 构造短小命令，避免 Demo 重复身份字段。"""

    return GovernedCommand(
        command_id=command_id,
        session_id="session-ch07-001",
        thread_id="session-ch07-001",
        capability=capability,
        category=category,
        payload={} if payload is None else payload,
        cost_units_per_attempt=cost_units,
    )


async def run_demo_async(root: str | Path | None = None) -> dict[str, Any]:
    """运行所有治理场景并返回可自动验证的 JSON 摘要。"""

    if root is None:
        root = tempfile.mkdtemp(prefix="realsight-ch07-")
    root_path = Path(root).expanduser().resolve()
    repository = GovernanceRepository(root_path / "governance.sqlite3")
    policy = demo_policy()
    middleware = GovernanceMiddleware(repository, policy)
    outcomes: dict[str, JsonValue] = {}

    # 1. 权限拒绝：operation 不能被调用，预算也不应变化。
    denied_operation = CountingOperation()
    denied = make_demo_command(
        "command-denied",
        capability="admin.delete",
        category=CommandCategory.TOOL,
        cost_units=1,
    )
    try:
        await middleware.execute(denied, denied_operation)
    except PermissionDenied as error:
        outcomes["permission_denied"] = type(error).__name__

    # 2. 瞬态失败两次，第三次成功；每次真实外部调用都收费。
    flaky_operation = FlakyOperation(failures_before_success=2)
    flaky_command = make_demo_command(
        "command-flaky-observation",
        capability="perception.observe",
        category=CommandCategory.OBSERVATION,
        cost_units=1,
        payload={"view_type": "back_label"},
    )
    flaky_result = await middleware.execute(flaky_command, flaky_operation)
    outcomes["flaky_result"] = flaky_result.model_dump(mode="json")

    # 3. 完全相同的命令重放直接返回缓存；传入的新 operation 不会运行。
    replay_operation = CountingOperation({"should_not_run": True})
    replay_result = await middleware.execute(flaky_command, replay_operation)
    outcomes["replay_result"] = replay_result.model_dump(mode="json")

    # 4. 一个受控慢命令持有租约时，第二命令必须在 operation 前被拒绝。
    controlled_operation = ControlledOperation()
    controlled_command = make_demo_command(
        "command-controlled",
        capability="rules.evaluate",
        category=CommandCategory.RULE,
        cost_units=0,
    )
    controlled_task = asyncio.create_task(
        middleware.execute(controlled_command, controlled_operation)
    )
    await controlled_operation.started.wait()
    busy_operation = CountingOperation()
    busy_command = make_demo_command(
        "command-while-busy",
        capability="spec.retrieve",
        category=CommandCategory.TOOL,
        cost_units=0,
    )
    try:
        await middleware.execute(busy_command, busy_operation)
    except ThreadBusy as error:
        outcomes["thread_busy"] = type(error).__name__
    controlled_operation.release.set()
    outcomes["controlled_result"] = (
        await controlled_task
    ).model_dump(mode="json")

    # 5. 每次 timeout 都取消底层协程；三次后命令进入 FAILED 终态。
    hanging_operation = HangingOperation()
    timeout_command = make_demo_command(
        "command-timeout",
        capability="spec.retrieve",
        category=CommandCategory.TOOL,
        cost_units=1,
    )
    try:
        await middleware.execute(timeout_command, hanging_operation)
    except RetryExhausted as error:
        outcomes["timeout_result"] = type(error).__name__

    # 6. observation 上限已经被第一条观察占用，第二条不会登记为命令。
    quota_operation = CountingOperation()
    quota_command = make_demo_command(
        "command-observation-quota",
        capability="perception.observe",
        category=CommandCategory.OBSERVATION,
        cost_units=0,
        payload={"view_type": "port_closeup"},
    )
    try:
        await middleware.execute(quota_command, quota_operation)
    except BudgetExceeded as error:
        outcomes["observation_quota"] = str(error)

    # 7. 逻辑命令仍有额度，但 6 个成本单位已用完；外部 operation 不运行。
    costly_operation = CountingOperation()
    costly_command = make_demo_command(
        "command-cost-budget",
        capability="spec.retrieve",
        category=CommandCategory.TOOL,
        cost_units=1,
    )
    try:
        await middleware.execute(costly_command, costly_operation)
    except BudgetExceeded as error:
        outcomes["cost_budget"] = str(error)

    events = repository.audit_events("session-ch07-001")
    event_counts = Counter(event.event_type.value for event in events)
    budget = repository.budget_snapshot("session-ch07-001")
    return {
        "demo": "RealSight Chapter 7 governance middleware",
        "storage_root": str(root_path),
        "outcomes": outcomes,
        "service_calls": {
            "denied": denied_operation.calls,
            "flaky": flaky_operation.calls,
            "replay": replay_operation.calls,
            "controlled": controlled_operation.calls,
            "busy": busy_operation.calls,
            "hanging": hanging_operation.calls,
            "hanging_cancellations": hanging_operation.cancellations,
            "observation_quota": quota_operation.calls,
            "cost_budget": costly_operation.calls,
        },
        "budget": budget.model_dump(mode="json"),
        "command_count": repository.command_count("session-ch07-001"),
        "active_leases": repository.active_lease_count("session-ch07-001"),
        "command_statuses": {
            command_id: (
                status.value
                if (status := repository.command_status("session-ch07-001", command_id))
                else None
            )
            for command_id in (
                "command-flaky-observation",
                "command-controlled",
                "command-timeout",
                "command-observation-quota",
                "command-cost-budget",
            )
        },
        "audit_event_count": len(events),
        "audit_event_counts": dict(sorted(event_counts.items())),
        "audit_sequence_is_strict": [event.sequence for event in events]
        == list(range(1, len(events) + 1)),
    }


def build_parser() -> argparse.ArgumentParser:
    """第 7 章只有一个聚合 Demo 命令；测试会直接调用更细的类和方法。"""

    parser = argparse.ArgumentParser(
        description="RealSight Chapter 7 governance middleware demo"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo")
    demo.add_argument("--root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """同步 CLI 入口用 asyncio.run 驱动异步中间件，并打印可检查 JSON。"""

    arguments = build_parser().parse_args(argv)
    result = asyncio.run(run_demo_async(arguments.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
