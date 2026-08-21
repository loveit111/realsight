"""
第 6 章 Demo：用 SQLite 检查点、证据账本和文件产物实现跨进程恢复。

这个文件做什么
------------
第 5 章已经用 LangGraph interrupt 做到了“暂停后继续”，但使用的是 InMemorySaver：
一旦 Python 进程退出，检查点也会消失。本文件把同一张主动观察图接到 SqliteSaver，
并故意让 start、resume-label、resume-port、resume-user 分别由独立 Python 进程执行，
从而证明恢复依赖磁盘中的 thread checkpoint，而不是依赖某个仍活着的 Python 对象。

本章还把容易混在一起的四类数据明确拆开：
1. checkpoints.sqlite3：LangGraph 的执行状态，例如当前节点、interrupt 和 BeliefState。
2. evidence.sqlite3：应用自己的不可变证据、观察元数据和“已被工作流接纳”关系。
3. artifacts/<session_id>/：图片等大文件；数据库只保存路径、哈希和大小。
4. 长期知识：跨任务复用的规格资料，不属于本章 session，因此本文件不创建 knowledge 表。

使用的技术栈
------------
- Python 标准库 sqlite3：实现应用证据账本、事务、外键、WAL 和完整性检查。
- LangGraph SqliteSaver：把第 5 章的图检查点持久化，支持进程退出后的 interrupt 恢复。
- Pydantic v2：继续校验 SessionManifest、ArtifactRecord、Observation 和 Evidence。
- hashlib SHA-256：给图片产物做内容寻址与篡改检测。
- pathlib、os.replace：构造跨平台目录，并通过同目录临时文件完成原子替换。
- subprocess：Demo 主进程启动四个短命子进程，真实演示“停掉再恢复”。

调用流程
--------
python examples/ch06_persistent_storage.py demo
    -> 子进程 1：start
       -> 创建 session.json 与两个 SQLite 数据库
       -> 用 SqliteSaver 运行图，暂停在 BACK_LABEL
       -> 把 checkpoint 中已有 Evidence 对账到 evidence.sqlite3
    -> 子进程 2：resume-label
       -> 从同一 checkpoint 读取当前 interrupt
       -> 原子保存标签 PPM 图片，登记 Observation 与候选 Evidence
       -> 定向恢复图，接纳证据，暂停在 PORT_CLOSEUP
       -> 把 checkpoint 中已接纳的 evidence_id 写入接纳关系表
    -> 子进程 3：resume-port
       -> 重复产物、候选证据、恢复和对账，暂停等待 laptop_model
    -> 子进程 4：resume-user
       -> 提交用户型号，图走到 RUN_RULES 并结束
       -> 对账用户 Evidence
    -> 子进程 5：inspect
       -> 重新打开数据库，校验文件 SHA-256，并输出最终存储摘要

为什么有“候选证据”和“接纳关系”两步
--------------------------------
LangGraph checkpoint 和应用 evidence ledger 位于两个 SQLite 数据库，不能用一个普通
事务同时提交。如果进程恰好在两次写入之间崩溃，就可能暂时只完成一边。本 Demo 先把
外部观察记为不可变候选，再恢复工作流；恢复成功后写入 evidence_acceptances。每次打开
会话还会用 checkpoint 中的 BeliefState.ledger 补做幂等对账，所以“图已接纳、关系未写”
可以在下次启动时修复。第 7 章会在此基础上增加并发锁、调用预算、超时和审计治理。

阅读建议
--------
先读 SessionLayout 和 SessionManifest，理解目录边界；再读 EvidenceStore 的 schema、
record_* 与 reconcile_state；随后读 open_durable_graph；最后顺着 phase_start、
phase_resume_observation、phase_resume_user 和 run_cross_process_demo 阅读完整调用链。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Interrupt
from pydantic import AwareDatetime, field_validator


# 直接运行 examples 下的文件时，把项目根加入导入路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.models import (  # noqa: E402
    ActiveObservationState,
    Evidence,
    EvidenceStatus,
    Identifier,
    NonEmptyText,
    Observation,
    ObservationStatus,
    QualitySignals,
    StrictContract,
    ViewType,
    utc_now,
)

# 本章复用第 5 章已经通过测试的状态图和安全恢复入口，只替换 checkpointer。
try:  # 作为 examples 包导入时使用相对导入。
    from .ch05_active_observation_interrupts import (
        CHECKPOINT_ALLOWED_TYPES,
        PAUSE_ADAPTER,
        ObservationResume,
        UserResume,
        build_active_observation_graph,
        build_initial_state,
        extract_pause,
        graph_config,
        load_checkpoint_state,
        resume_active_observation,
    )
except ImportError:  # 直接执行本文件时，examples 已加入 sys.path。
    from ch05_active_observation_interrupts import (
        CHECKPOINT_ALLOWED_TYPES,
        PAUSE_ADAPTER,
        ObservationResume,
        UserResume,
        build_active_observation_graph,
        build_initial_state,
        extract_pause,
        graph_config,
        load_checkpoint_state,
        resume_active_observation,
    )


SCHEMA_VERSION = 1
DEFAULT_SESSION_ID = "session-ch06-001"
DEFAULT_TARGET_ID = "charger-ch06-001"


class StorageError(RuntimeError):
    """第 6 章存储错误的共同基类，便于 API 层统一转换成错误响应。"""


class ImmutableRecordConflict(StorageError):
    """同一不可变 ID 已经存在，但新内容与原内容不同。"""


class ArtifactIntegrityError(StorageError):
    """图片文件缺失、越界、大小不符或 SHA-256 不匹配。"""


class SessionAlreadyExists(StorageError):
    """阻止 start 命令静默覆盖已经存在的任务目录。"""


class SessionManifest(StrictContract):
    """会话目录中的稳定索引；它只描述身份和布局，不复制动态 Graph State。"""

    schema_version: Literal[1] = 1
    session_id: Identifier
    target_id: Identifier
    created_at: AwareDatetime
    checkpoint_database: Literal["checkpoints.sqlite3"] = "checkpoints.sqlite3"
    evidence_database: Literal["evidence.sqlite3"] = "evidence.sqlite3"
    artifact_directory: Literal["artifacts"] = "artifacts"


class ArtifactRecord(StrictContract):
    """数据库中保存的文件元数据；真正的二进制内容只存在于文件系统。"""

    schema_version: Literal[1] = 1
    artifact_id: Identifier
    session_id: Identifier
    relative_path: NonEmptyText
    sha256: NonEmptyText
    byte_size: int
    mime_type: NonEmptyText
    created_at: AwareDatetime

    @field_validator("sha256")
    @classmethod
    def sha256_is_lowercase_hex(cls, value: str) -> str:
        """SHA-256 必须是 64 位小写十六进制，防止把任意字符串当完整性摘要。"""

        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("byte_size")
    @classmethod
    def byte_size_is_positive(cls, value: int) -> int:
        """本章只登记非空文件；空文件通常意味着采集或写盘失败。"""

        if isinstance(value, bool) or value <= 0:
            raise ValueError("byte_size must be a positive integer")
        return value


@dataclass(frozen=True)
class SessionLayout:
    """集中计算一个任务的所有路径，避免业务代码到处拼接文件名。"""

    root: Path

    @classmethod
    def from_root(cls, root: str | Path) -> SessionLayout:
        """把用户提供的根目录转换成绝对路径，后续越界检查都以它为准。"""

        return cls(root=Path(root).expanduser().resolve())

    @property
    def manifest_path(self) -> Path:
        return self.root / "session.json"

    @property
    def checkpoint_database(self) -> Path:
        return self.root / "checkpoints.sqlite3"

    @property
    def evidence_database(self) -> Path:
        return self.root / "evidence.sqlite3"

    @property
    def artifact_root(self) -> Path:
        return self.root / "artifacts"

    def artifact_session_directory(self, session_id: str) -> Path:
        """Identifier 契约不允许斜杠，因此 session_id 不会逃出 artifacts 目录。"""

        # SessionManifest 会在调用本方法前验证 ID；这里再做一层轻量路径防线。
        if not session_id or any(character in session_id for character in ("/", "\\")):
            raise ValueError("session_id is not safe for an artifact directory")
        return self.artifact_root / session_id


def canonical_json(value: Any) -> str:
    """生成稳定 JSON；字典键顺序不同不应被误判为两份不同记录。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ensure_path_inside(path: Path, parent: Path) -> None:
    """解析路径后检查父子关系，阻止数据库中的恶意相对路径读取任意文件。"""

    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    if not resolved_path.is_relative_to(resolved_parent):
        raise ArtifactIntegrityError(f"artifact path escapes storage root: {path}")


def write_manifest(layout: SessionLayout, manifest: SessionManifest) -> None:
    """通过同目录临时文件 + os.replace 原子写 session.json。"""

    layout.root.mkdir(parents=True, exist_ok=True)
    temporary = layout.manifest_path.with_suffix(f".json.{os.getpid()}.tmp")
    temporary.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, layout.manifest_path)


def load_manifest(layout: SessionLayout) -> SessionManifest:
    """读取并校验清单；损坏 JSON 或未知字段都不会悄悄进入后续流程。"""

    if not layout.manifest_path.is_file():
        raise StorageError(f"session manifest does not exist: {layout.manifest_path}")
    return SessionManifest.model_validate_json(
        layout.manifest_path.read_text(encoding="utf-8")
    )


class EvidenceStore:
    """应用自己的 SQLite 证据仓库，与 LangGraph 内部 checkpoint 表完全分离。"""

    def __init__(self, layout: SessionLayout) -> None:
        self.layout = layout
        self.layout.root.mkdir(parents=True, exist_ok=True)
        self.initialize_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作使用短连接；异常回滚，成功提交，并始终关闭文件句柄。"""

        connection = sqlite3.connect(self.layout.evidence_database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        """创建版本 1 schema；CREATE IF NOT EXISTS 让每次进程启动都可安全调用。"""

        with self.connection() as connection:
            # WAL 允许一个写者与多个读者更平稳地共存；多写者治理留到第 7 章。
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    session_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
                    mime_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, artifact_id),
                    UNIQUE (session_id, sha256),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS observations (
                    session_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    view_type TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, observation_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (session_id, artifact_id)
                        REFERENCES artifacts(session_id, artifact_id)
                );

                CREATE TABLE IF NOT EXISTS evidence_records (
                    session_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, evidence_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS evidence_acceptances (
                    session_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, evidence_id, thread_id),
                    FOREIGN KEY (session_id, evidence_id)
                        REFERENCES evidence_records(session_id, evidence_id)
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_session_field
                    ON evidence_records(session_id, field_name);
                CREATE INDEX IF NOT EXISTS idx_observations_session_request
                    ON observations(session_id, request_id);
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _insert_immutable(
        connection: sqlite3.Connection,
        *,
        table: str,
        key_columns: dict[str, Any],
        row: dict[str, Any],
        payload_json: str,
    ) -> bool:
        """插入不可变行；相同主键同内容幂等，不同内容立即报冲突。"""

        where_clause = " AND ".join(f"{column} = ?" for column in key_columns)
        existing = connection.execute(
            f"SELECT payload_json FROM {table} WHERE {where_clause}",
            tuple(key_columns.values()),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload_json:
                identity = ", ".join(
                    f"{column}={value}" for column, value in key_columns.items()
                )
                raise ImmutableRecordConflict(
                    f"{table} immutable record contains different data: {identity}"
                )
            return False

        columns = tuple(row)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )
        return True

    def register_session(self, manifest: SessionManifest) -> None:
        """登记会话身份；同一个 session_id 以后不能被换到另一个 target。"""

        payload = canonical_json(manifest.model_dump(mode="json"))
        row = {
            "session_id": manifest.session_id,
            "target_id": manifest.target_id,
            "created_at": manifest.created_at.isoformat(),
            "payload_json": payload,
        }
        with self.connection() as connection:
            self._insert_immutable(
                connection,
                table="sessions",
                key_columns={"session_id": manifest.session_id},
                row=row,
                payload_json=payload,
            )

    def _load_artifact(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        artifact_id: str,
    ) -> ArtifactRecord | None:
        """按复合主键读取 ArtifactRecord，供内容寻址写入做幂等判断。"""

        row = connection.execute(
            """
            SELECT payload_json FROM artifacts
            WHERE session_id = ? AND artifact_id = ?
            """,
            (session_id, artifact_id),
        ).fetchone()
        if row is None:
            return None
        return ArtifactRecord.model_validate_json(row["payload_json"])

    def store_artifact(
        self,
        manifest: SessionManifest,
        *,
        content: bytes,
        suffix: str,
        mime_type: str,
    ) -> ArtifactRecord:
        """原子保存内容寻址文件，并把路径、哈希和大小登记到 SQLite。"""

        if not content:
            raise ValueError("artifact content must not be empty")
        if (
            not suffix.startswith(".")
            or len(suffix) > 12
            or not suffix[1:].isalnum()
        ):
            raise ValueError("artifact suffix must look like .ppm or .jpg")

        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"artifact-{digest}"
        session_directory = self.layout.artifact_session_directory(
            manifest.session_id
        )
        session_directory.mkdir(parents=True, exist_ok=True)
        destination = session_directory / f"{digest}{suffix.lower()}"
        _ensure_path_inside(destination, self.layout.artifact_root)

        with self.connection() as connection:
            existing = self._load_artifact(
                connection,
                manifest.session_id,
                artifact_id,
            )
            if existing is not None:
                if (
                    existing.sha256 != digest
                    or existing.byte_size != len(content)
                    or existing.mime_type != mime_type
                    or existing.relative_path
                    != destination.relative_to(self.layout.root).as_posix()
                ):
                    raise ImmutableRecordConflict(
                        f"artifact metadata changed: {artifact_id}"
                    )
                self._verify_one_artifact(existing)
                return existing

            # 临时文件和最终文件位于同一目录，os.replace 才能提供可靠的原子替换语义。
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.tmp"
            )
            temporary.write_bytes(content)
            os.replace(temporary, destination)

            record = ArtifactRecord(
                artifact_id=artifact_id,
                session_id=manifest.session_id,
                relative_path=destination.relative_to(self.layout.root).as_posix(),
                sha256=digest,
                byte_size=len(content),
                mime_type=mime_type,
                created_at=utc_now(),
            )
            payload = canonical_json(record.model_dump(mode="json"))
            row = {
                "session_id": record.session_id,
                "artifact_id": record.artifact_id,
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "byte_size": record.byte_size,
                "mime_type": record.mime_type,
                "created_at": record.created_at.isoformat(),
                "payload_json": payload,
            }
            self._insert_immutable(
                connection,
                table="artifacts",
                key_columns={
                    "session_id": record.session_id,
                    "artifact_id": record.artifact_id,
                },
                row=row,
                payload_json=payload,
            )
            return record

    def record_observation_candidate(
        self,
        manifest: SessionManifest,
        observation: Observation,
        artifact: ArtifactRecord,
    ) -> None:
        """记录外部服务确实返回过的 Observation；这不等于工作流已接纳它。"""

        if observation.target_id != manifest.target_id:
            raise ValueError("observation target does not match session manifest")
        if artifact.session_id != manifest.session_id:
            raise ValueError("artifact session does not match observation session")
        if observation.image_path != artifact.relative_path:
            raise ValueError("observation image_path must reference the stored artifact")

        payload = canonical_json(observation.model_dump(mode="json"))
        row = {
            "session_id": manifest.session_id,
            "observation_id": observation.observation_id,
            "request_id": observation.request_id,
            "target_id": observation.target_id,
            "view_type": observation.view_type.value,
            "artifact_id": artifact.artifact_id,
            "captured_at": observation.captured_at.isoformat(),
            "payload_json": payload,
        }
        with self.connection() as connection:
            self._insert_immutable(
                connection,
                table="observations",
                key_columns={
                    "session_id": manifest.session_id,
                    "observation_id": observation.observation_id,
                },
                row=row,
                payload_json=payload,
            )

    def _record_evidence_with_connection(
        self,
        connection: sqlite3.Connection,
        manifest: SessionManifest,
        evidence: Evidence,
    ) -> bool:
        """在已有事务中登记 Evidence，供单条写入和批量对账共同复用。"""

        if evidence.target_id != manifest.target_id:
            raise ValueError("evidence target does not match session manifest")
        payload = canonical_json(evidence.model_dump(mode="json"))
        row = {
            "session_id": manifest.session_id,
            "evidence_id": evidence.evidence_id,
            "target_id": evidence.target_id,
            "field_name": evidence.field,
            "source_type": evidence.source_type,
            "source_id": evidence.source_id,
            "status": evidence.status.value,
            "created_at": utc_now().isoformat(),
            "payload_json": payload,
        }
        return self._insert_immutable(
            connection,
            table="evidence_records",
            key_columns={
                "session_id": manifest.session_id,
                "evidence_id": evidence.evidence_id,
            },
            row=row,
            payload_json=payload,
        )

    def record_evidence_candidate(
        self,
        manifest: SessionManifest,
        evidence: Evidence,
    ) -> bool:
        """保存候选 Evidence；返回 True 表示新插入，False 表示完全相同的幂等重试。"""

        with self.connection() as connection:
            return self._record_evidence_with_connection(
                connection,
                manifest,
                evidence,
            )

    def reconcile_state(
        self,
        manifest: SessionManifest,
        state: ActiveObservationState,
    ) -> int:
        """以 checkpoint 的 BeliefState.ledger 为准，幂等补齐 Evidence 与接纳关系。"""

        if state.session.session_id != manifest.session_id:
            raise ValueError("checkpoint session does not match manifest")
        if state.target.target_id != manifest.target_id:
            raise ValueError("checkpoint target does not match manifest")

        inserted_acceptances = 0
        with self.connection() as connection:
            for evidence in state.belief.ledger.values():
                self._record_evidence_with_connection(
                    connection,
                    manifest,
                    evidence,
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence_acceptances
                        (session_id, evidence_id, thread_id, accepted_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        manifest.session_id,
                        evidence.evidence_id,
                        state.session.thread_id,
                        utc_now().isoformat(),
                    ),
                )
                inserted_acceptances += cursor.rowcount
        return inserted_acceptances

    def accepted_evidence(self, manifest: SessionManifest) -> tuple[Evidence, ...]:
        """读取已接纳 Evidence；候选但未接纳的记录不会出现在结果中。"""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.payload_json
                FROM evidence_records AS e
                INNER JOIN evidence_acceptances AS a
                    ON a.session_id = e.session_id
                    AND a.evidence_id = e.evidence_id
                WHERE e.session_id = ?
                ORDER BY e.evidence_id
                """,
                (manifest.session_id,),
            ).fetchall()
        return tuple(
            Evidence.model_validate_json(row["payload_json"])
            for row in rows
        )

    def counts(self, manifest: SessionManifest) -> dict[str, int]:
        """返回教学用表计数，便于快速确认每类数据写到了正确位置。"""

        table_names = (
            "sessions",
            "artifacts",
            "observations",
            "evidence_records",
            "evidence_acceptances",
        )
        result: dict[str, int] = {}
        with self.connection() as connection:
            for table_name in table_names:
                if table_name == "sessions":
                    query = "SELECT COUNT(*) FROM sessions WHERE session_id = ?"
                else:
                    query = f"SELECT COUNT(*) FROM {table_name} WHERE session_id = ?"
                result[table_name] = int(
                    connection.execute(
                        query,
                        (manifest.session_id,),
                    ).fetchone()[0]
                )
        return result

    def _verify_one_artifact(self, record: ArtifactRecord) -> None:
        """对一项 ArtifactRecord 做路径、存在性、大小和哈希四重检查。"""

        path = self.layout.root / record.relative_path
        _ensure_path_inside(path, self.layout.artifact_root)
        if not path.is_file():
            raise ArtifactIntegrityError(f"artifact file is missing: {path}")
        content = path.read_bytes()
        if len(content) != record.byte_size:
            raise ArtifactIntegrityError(
                f"artifact byte size changed: {record.artifact_id}"
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest != record.sha256:
            raise ArtifactIntegrityError(
                f"artifact sha256 changed: {record.artifact_id}"
            )

    def verify_artifacts(self, manifest: SessionManifest) -> int:
        """验证会话中的所有产物，并返回通过检查的文件数量。"""

        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM artifacts WHERE session_id = ?",
                (manifest.session_id,),
            ).fetchall()
        records = tuple(
            ArtifactRecord.model_validate_json(row["payload_json"])
            for row in rows
        )
        for record in records:
            self._verify_one_artifact(record)
        return len(records)


def create_session(
    layout: SessionLayout,
    *,
    session_id: str = DEFAULT_SESSION_ID,
    target_id: str = DEFAULT_TARGET_ID,
) -> tuple[SessionManifest, EvidenceStore]:
    """创建新会话目录；已有 manifest 时拒绝覆盖，恢复必须使用 load_session。"""

    if layout.manifest_path.exists():
        raise SessionAlreadyExists(
            f"session already exists; use a resume command: {layout.root}"
        )
    manifest = SessionManifest(
        session_id=session_id,
        target_id=target_id,
        created_at=utc_now(),
    )
    write_manifest(layout, manifest)
    layout.artifact_root.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(layout)
    store.register_session(manifest)
    return manifest, store


def load_session(
    layout: SessionLayout,
) -> tuple[SessionManifest, EvidenceStore]:
    """恢复命令的统一入口：先验证 manifest，再打开应用证据数据库。"""

    manifest = load_manifest(layout)
    store = EvidenceStore(layout)
    store.register_session(manifest)
    return manifest, store


@contextmanager
def open_durable_graph(layout: SessionLayout) -> Iterator[Any]:
    """用显式安全序列化器和 SQLite 连接构建第 5 章状态图。"""

    layout.root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        layout.checkpoint_database,
        timeout=5.0,
        check_same_thread=False,
    )
    connection.execute("PRAGMA busy_timeout = 5000")
    serializer = JsonPlusSerializer(
        allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES,
    )
    saver = SqliteSaver(connection, serde=serializer)
    graph, _ = build_active_observation_graph(saver)
    try:
        yield graph
    finally:
        connection.close()


def current_interrupt(
    graph: Any,
    config: dict[str, Any],
) -> tuple[Interrupt, Any]:
    """从持久化 snapshot 读取唯一活动 interrupt，而不是依赖上一个进程的返回值。"""

    snapshot = graph.get_state(config)
    interrupts = [
        interrupt_info
        for task in snapshot.tasks
        for interrupt_info in task.interrupts
    ]
    if len(interrupts) != 1:
        raise StorageError(
            f"expected one active interrupt in checkpoint, got {len(interrupts)}"
        )
    interrupt_info = interrupts[0]
    return interrupt_info, PAUSE_ADAPTER.validate_python(interrupt_info.value)


def demo_ppm(field_name: str) -> bytes:
    """只用标准库生成一个真实可解码的 4x2 PPM 图片，避免伪造 JPG 文件。"""

    palettes = {
        "charger_label": ((230, 230, 230), (30, 30, 30)),
        "charger_port_type": ((20, 90, 180), (220, 220, 220)),
    }
    if field_name not in palettes:
        raise ValueError(f"no demo image palette for field: {field_name}")
    first, second = palettes[field_name]
    pixels = bytes(first + second + first + second + second + first + second + first)
    return b"P6\n4 2\n255\n" + pixels


def make_stored_observation_resume(
    request: Any,
    artifact: ArtifactRecord,
    *,
    observation_id: str,
    field_name: str,
    value: Any,
) -> tuple[Observation, Evidence, dict[str, Any]]:
    """创建引用真实产物路径的 Observation、Evidence 和 JSON 恢复载荷。"""

    observation = Observation(
        observation_id=observation_id,
        request_id=request.request_id,
        target_id=request.target_id,
        view_type=request.view_type,
        captured_at=utc_now(),
        quality=QualitySignals(
            overall_score=0.94,
            sharpness=0.93,
            exposure=0.90,
            glare=0.92,
            target_ratio=0.76,
        ),
        local_features=(field_name,),
        image_path=artifact.relative_path,
        status=ObservationStatus.ACCEPTED,
    )
    evidence = Evidence(
        evidence_id=f"{observation_id}-{field_name}",
        target_id=request.target_id,
        field=field_name,
        value=value,
        source_type="vision_extraction",
        source_id=observation_id,
        confidence=0.98,
        status=EvidenceStatus.CONFIRMED,
    )
    resume = ObservationResume(observation=observation, evidence=(evidence,))
    return observation, evidence, resume.model_dump(mode="json")


def phase_payload(
    *,
    phase: str,
    manifest: SessionManifest,
    state: ActiveObservationState,
    interrupt_info: Interrupt | None,
    store: EvidenceStore,
) -> dict[str, Any]:
    """把每个短命进程的结果整理为稳定 JSON，供人阅读和自动化测试。"""

    return {
        "phase": phase,
        "pid": os.getpid(),
        "session_id": manifest.session_id,
        "thread_id": state.session.thread_id,
        "session_status": state.session.status.value,
        "route": state.route.value if state.route is not None else None,
        "interrupt_id": interrupt_info.id if interrupt_info is not None else None,
        "missing_fields": list(state.missing_fields),
        "accepted_fields": sorted(
            evidence.field for evidence in store.accepted_evidence(manifest)
        ),
        "counts": store.counts(manifest),
    }


def phase_start(layout: SessionLayout) -> dict[str, Any]:
    """新建会话，运行到第一次标签中断，然后同步初始 evidence ledger。"""

    manifest, store = create_session(layout)
    initial_state = build_initial_state(
        session_id=manifest.session_id,
        target_id=manifest.target_id,
    )
    config = graph_config(manifest.session_id)
    with open_durable_graph(layout) as graph:
        result = graph.invoke(initial_state, config=config)
        interrupt_info, _ = extract_pause(result)
        state = load_checkpoint_state(graph, config)
        store.reconcile_state(manifest, state)
    return phase_payload(
        phase="start",
        manifest=manifest,
        state=state,
        interrupt_info=interrupt_info,
        store=store,
    )


def phase_resume_observation(
    layout: SessionLayout,
    *,
    phase: str,
    expected_view: ViewType,
    field_name: str,
    value: Any,
    observation_id: str,
) -> dict[str, Any]:
    """在新进程中恢复一次视觉中断，并把外部事实与工作流接纳分两步落盘。"""

    manifest, store = load_session(layout)
    config = graph_config(manifest.session_id)
    with open_durable_graph(layout) as graph:
        state_before = load_checkpoint_state(graph, config)
        # 每次先对账，可修复“上次图已提交、接纳关系尚未提交”时发生的崩溃窗口。
        store.reconcile_state(manifest, state_before)
        interrupt_info, _ = current_interrupt(graph, config)
        request = state_before.pending_request
        if request is None:
            raise StorageError("checkpoint is not waiting for an observation")
        if request.view_type != expected_view:
            raise StorageError(
                f"expected {expected_view.value}, got {request.view_type.value}"
            )
        if request.required_features != (field_name,):
            raise StorageError(
                f"checkpoint requested {request.required_features}, not {field_name}"
            )

        artifact = store.store_artifact(
            manifest,
            content=demo_ppm(field_name),
            suffix=".ppm",
            mime_type="image/x-portable-pixmap",
        )
        observation, evidence, resume_payload = make_stored_observation_resume(
            request,
            artifact,
            observation_id=observation_id,
            field_name=field_name,
            value=value,
        )
        # 这两项是“外部服务返回过什么”；即使工作流拒绝，它们也仍是可审计候选事实。
        store.record_observation_candidate(manifest, observation, artifact)
        store.record_evidence_candidate(manifest, evidence)

        resume_active_observation(
            graph,
            config,
            resume_payload,
            interrupt_id=interrupt_info.id,
        )
        state_after = load_checkpoint_state(graph, config)
        store.reconcile_state(manifest, state_after)
        next_interrupt, _ = current_interrupt(graph, config)

    return phase_payload(
        phase=phase,
        manifest=manifest,
        state=state_after,
        interrupt_info=next_interrupt,
        store=store,
    )


def phase_resume_label(layout: SessionLayout) -> dict[str, Any]:
    """恢复 BACK_LABEL：保存标签图片并提交 charger_label Evidence。"""

    return phase_resume_observation(
        layout,
        phase="resume-label",
        expected_view=ViewType.BACK_LABEL,
        field_name="charger_label",
        value="USB-C PD 65W",
        observation_id="observation-ch06-label-001",
    )


def phase_resume_port(layout: SessionLayout) -> dict[str, Any]:
    """恢复 PORT_CLOSEUP：保存接口图片并提交 charger_port_type Evidence。"""

    return phase_resume_observation(
        layout,
        phase="resume-port",
        expected_view=ViewType.PORT_CLOSEUP,
        field_name="charger_port_type",
        value="usb_c",
        observation_id="observation-ch06-port-001",
    )


def phase_resume_user(layout: SessionLayout) -> dict[str, Any]:
    """在新进程中恢复用户输入中断，随后图到达 RUN_RULES 正常结束。"""

    manifest, store = load_session(layout)
    config = graph_config(manifest.session_id)
    with open_durable_graph(layout) as graph:
        state_before = load_checkpoint_state(graph, config)
        store.reconcile_state(manifest, state_before)
        interrupt_info, _ = current_interrupt(graph, config)
        if state_before.expected_user_field != "laptop_model":
            raise StorageError("checkpoint is not waiting for laptop_model")
        resume_payload = UserResume(
            target_id=manifest.target_id,
            field="laptop_model",
            value="ExampleBook Pro 14",
            source_id="user-message-ch06-001",
        ).model_dump(mode="json")
        resume_active_observation(
            graph,
            config,
            resume_payload,
            interrupt_id=interrupt_info.id,
        )
        state_after = load_checkpoint_state(graph, config)
        store.reconcile_state(manifest, state_after)
        if graph.get_state(config).next:
            raise StorageError("completed graph unexpectedly has another node")

    return phase_payload(
        phase="resume-user",
        manifest=manifest,
        state=state_after,
        interrupt_info=None,
        store=store,
    )


def phase_inspect(layout: SessionLayout) -> dict[str, Any]:
    """重新打开全部持久化资源，补做对账并校验产物完整性。"""

    manifest, store = load_session(layout)
    config = graph_config(manifest.session_id)
    with open_durable_graph(layout) as graph:
        state = load_checkpoint_state(graph, config)
        repaired_acceptances = store.reconcile_state(manifest, state)
        snapshot = graph.get_state(config)
        interrupt_info = None
        if snapshot.next:
            interrupt_info, _ = current_interrupt(graph, config)

    result = phase_payload(
        phase="inspect",
        manifest=manifest,
        state=state,
        interrupt_info=interrupt_info,
        store=store,
    )
    result.update(
        {
            "verified_artifacts": store.verify_artifacts(manifest),
            "repaired_acceptances": repaired_acceptances,
            "checkpoint_database_exists": layout.checkpoint_database.is_file(),
            "evidence_database_exists": layout.evidence_database.is_file(),
            "long_term_knowledge_stored": False,
            "storage_root": str(layout.root),
        }
    )
    return result


def _run_child_phase(script: Path, phase: str, layout: SessionLayout) -> dict[str, Any]:
    """运行一个独立解释器进程，并把它输出的单行 JSON 解析回来。"""

    environment = dict(os.environ)
    environment["LANGGRAPH_STRICT_MSGPACK"] = "true"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            phase,
            "--root",
            str(layout.root),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(output_lines) != 1:
        raise StorageError(
            f"child phase {phase} must print exactly one JSON line; "
            f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
        )
    return json.loads(output_lines[0])


def run_cross_process_demo(root: str | Path | None = None) -> dict[str, Any]:
    """依次启动五个独立进程，返回 PID、状态演进和最终存储摘要。"""

    if root is None:
        root = tempfile.mkdtemp(prefix="realsight-ch06-")
    layout = SessionLayout.from_root(root)
    script = Path(__file__).resolve()
    phases = ("start", "resume-label", "resume-port", "resume-user", "inspect")
    steps = [
        _run_child_phase(script, phase, layout)
        for phase in phases
    ]
    process_ids = [step["pid"] for step in steps]
    return {
        "demo": "RealSight Chapter 6 persistent storage",
        "storage_root": str(layout.root),
        "process_ids": process_ids,
        "distinct_process_count": len(set(process_ids)),
        "steps": steps,
        "final": steps[-1],
    }


def build_parser() -> argparse.ArgumentParser:
    """创建六个显式子命令，让每个进程只承担一个可观察阶段。"""

    parser = argparse.ArgumentParser(
        description="RealSight Chapter 6 durable checkpoint and evidence storage demo"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume-label", "resume-port", "resume-user", "inspect"):
        child = subparsers.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
    demo = subparsers.add_parser("demo")
    demo.add_argument("--root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口：阶段命令输出单行 JSON，demo 输出便于阅读的缩进 JSON。"""

    arguments = build_parser().parse_args(argv)
    if arguments.command == "demo":
        result = run_cross_process_demo(arguments.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    layout = SessionLayout.from_root(arguments.root)
    handlers = {
        "start": phase_start,
        "resume-label": phase_resume_label,
        "resume-port": phase_resume_port,
        "resume-user": phase_resume_user,
        "inspect": phase_inspect,
    }
    result = handlers[arguments.command](layout)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
