"""
第 6 章自动化测试：验证 SQLite 跨进程恢复、证据对账和图片完整性。

这个文件的整体逻辑
------------------
测试为每个用例创建独立临时目录，避免会话和数据库相互污染。最重要的测试不会在同一个
Python 进程里连续调用函数，而是依次启动 start、resume-label、resume-port、
resume-user、inspect 五个解释器进程。每个子进程都会关闭内存对象和 SQLite 连接，
下一个进程只能依靠 session.json 与 checkpoints.sqlite3 找到原 thread 并继续。

其他测试分别验证：
1. Evidence 相同 ID + 相同内容可以幂等重试，不同内容必须报冲突。
2. 图片字节不进入 SQLite，而是保存到 artifacts 目录并由 SHA-256 校验。
3. 人工删除一次“接纳关系”后，下次 inspect 能根据 checkpoint 自动补做对账。
4. checkpoint 数据库与 evidence 数据库表职责分离，不创建长期知识表。
5. start 不能覆盖已有 session，图片被篡改后必须被检测出来。

使用的技术栈
------------
- Python 标准库 unittest：组织可重复的单元测试和集成测试。
- tempfile：为每项测试创建、清理隔离的真实文件目录。
- subprocess：证明恢复跨越了操作系统进程边界。
- sqlite3：检查表结构、模拟崩溃窗口和验证不可变账本。
- LangGraph SqliteSaver：被测的持久化 checkpoint 实现。
- Pydantic：构造冲突 Evidence 时仍经过正式领域契约校验。

测试调用流程
------------
python -m unittest discover -s tests -p "test_ch06*.py" -v
    -> setUp() 创建临时 storage root
    -> 子进程或阶段函数创建/恢复持久化 session
    -> 重新打开 checkpoint 与 evidence ledger
    -> 检查状态、表计数、哈希、幂等性和对账结果
    -> tearDown() 在连接关闭后清理临时目录

这些测试不需要摄像头、LLM 或网络。它们验证的是第 6 章本地单用户 MVP；同一 thread
的并发写锁、超时、预算和审计策略属于第 7 章。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples"
for import_path in (PROJECT_ROOT, EXAMPLES_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from ch06_persistent_storage import (  # noqa: E402
    ArtifactIntegrityError,
    EvidenceStore,
    ImmutableRecordConflict,
    SessionAlreadyExists,
    SessionLayout,
    create_session,
    demo_ppm,
    load_session,
    phase_inspect,
    phase_resume_label,
    phase_resume_port,
    phase_resume_user,
    phase_start,
)
from contracts.models import Evidence  # noqa: E402


class Chapter06StorageTestCase(unittest.TestCase):
    """为每项测试准备一个会在用例结束时自动删除的会话根目录。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="realsight-ch06-test-"
        )
        self.layout = SessionLayout.from_root(self.temporary_directory.name)
        self.script = EXAMPLES_DIR / "ch06_persistent_storage.py"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_child(self, phase: str) -> dict[str, object]:
        """启动一个全新的 Python 进程，并读取该阶段输出的单行 JSON。"""

        environment = dict(os.environ)
        environment["LANGGRAPH_STRICT_MSGPACK"] = "true"
        completed = subprocess.run(
            [
                sys.executable,
                str(self.script),
                phase,
                "--root",
                str(self.layout.root),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.stderr, "")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def complete_in_current_process(self) -> None:
        """需要直接检查数据库的测试使用短连接阶段函数完成整条流程。"""

        phase_start(self.layout)
        phase_resume_label(self.layout)
        phase_resume_port(self.layout)
        phase_resume_user(self.layout)


class CrossProcessRecoveryTests(Chapter06StorageTestCase):
    """核心验收：五个独立进程沿同一持久化 thread 完成任务。"""

    def test_five_processes_resume_one_thread_to_rules(self) -> None:
        start = self.run_child("start")
        label = self.run_child("resume-label")
        port = self.run_child("resume-port")
        user = self.run_child("resume-user")
        inspection = self.run_child("inspect")

        process_ids = [
            start["pid"],
            label["pid"],
            port["pid"],
            user["pid"],
            inspection["pid"],
        ]
        self.assertEqual(len(set(process_ids)), 5)
        self.assertEqual(start["thread_id"], "session-ch06-001")
        self.assertEqual(label["thread_id"], start["thread_id"])
        self.assertEqual(port["session_status"], "waiting_user")
        self.assertEqual(user["route"], "run_rules")
        self.assertIsNone(user["interrupt_id"])
        self.assertEqual(inspection["verified_artifacts"], 2)
        self.assertFalse(inspection["long_term_knowledge_stored"])
        self.assertEqual(
            inspection["counts"],
            {
                "sessions": 1,
                "artifacts": 2,
                "observations": 2,
                "evidence_records": 5,
                "evidence_acceptances": 5,
            },
        )
        self.assertEqual(
            inspection["accepted_fields"],
            [
                "charger_label",
                "charger_max_power_w",
                "charger_port_type",
                "charger_protocol",
                "laptop_model",
            ],
        )

    def test_each_restart_reads_a_new_interrupt_from_checkpoint(self) -> None:
        start = self.run_child("start")
        label = self.run_child("resume-label")
        port = self.run_child("resume-port")

        self.assertNotEqual(start["interrupt_id"], label["interrupt_id"])
        self.assertNotEqual(label["interrupt_id"], port["interrupt_id"])
        self.assertEqual(
            start["missing_fields"],
            ["charger_label", "charger_port_type", "laptop_model"],
        )
        self.assertEqual(
            label["missing_fields"],
            ["charger_port_type", "laptop_model"],
        )
        self.assertEqual(port["missing_fields"], ["laptop_model"])


class EvidenceLedgerTests(Chapter06StorageTestCase):
    """验证不可变候选账本、接纳关系和崩溃后对账。"""

    def test_same_evidence_is_idempotent_but_changed_content_conflicts(self) -> None:
        phase_start(self.layout)
        manifest, store = load_session(self.layout)
        original = store.accepted_evidence(manifest)[0]

        # 初始对账已经写入该证据，再写一次同样内容应返回 False，而不是重复插入。
        self.assertFalse(store.record_evidence_candidate(manifest, original))

        changed_values = original.model_dump(mode="python")
        changed_values["value"] = "tampered-value"
        changed = Evidence.model_validate(changed_values)
        with self.assertRaises(ImmutableRecordConflict):
            store.record_evidence_candidate(manifest, changed)

        self.assertEqual(store.counts(manifest)["evidence_records"], 2)

    def test_reconcile_repairs_acceptance_missing_after_checkpoint_commit(self) -> None:
        self.complete_in_current_process()
        manifest, store = load_session(self.layout)
        laptop = next(
            evidence
            for evidence in store.accepted_evidence(manifest)
            if evidence.field == "laptop_model"
        )

        # 模拟进程在 checkpoint 已提交、acceptance 尚未提交时崩溃。
        with store.connection() as connection:
            connection.execute(
                """
                DELETE FROM evidence_acceptances
                WHERE session_id = ? AND evidence_id = ?
                """,
                (manifest.session_id, laptop.evidence_id),
            )
        self.assertEqual(store.counts(manifest)["evidence_acceptances"], 4)

        inspection = phase_inspect(self.layout)
        self.assertEqual(inspection["repaired_acceptances"], 1)
        self.assertEqual(inspection["counts"]["evidence_acceptances"], 5)

    def test_start_refuses_to_overwrite_existing_session(self) -> None:
        phase_start(self.layout)

        with self.assertRaises(SessionAlreadyExists):
            phase_start(self.layout)


class ArtifactStorageTests(Chapter06StorageTestCase):
    """验证图片在文件系统、数据库只存元数据，并可检测篡改。"""

    def test_artifacts_are_valid_ppm_files_outside_sqlite(self) -> None:
        self.complete_in_current_process()
        manifest, store = load_session(self.layout)
        files = sorted(
            self.layout.artifact_session_directory(manifest.session_id).glob("*.ppm")
        )

        self.assertEqual(len(files), 2)
        self.assertTrue(all(path.read_bytes().startswith(b"P6\n4 2\n255\n") for path in files))
        self.assertEqual(store.verify_artifacts(manifest), 2)

        # evidence DB 的 schema 没有 BLOB 列；图片只通过 relative_path 被引用。
        with store.connection() as connection:
            artifact_columns = connection.execute(
                "PRAGMA table_info(artifacts)"
            ).fetchall()
        self.assertNotIn("BLOB", {row["type"].upper() for row in artifact_columns})

    def test_same_content_is_content_addressed_and_idempotent(self) -> None:
        manifest, store = create_session(self.layout)
        content = demo_ppm("charger_label")

        first = store.store_artifact(
            manifest,
            content=content,
            suffix=".ppm",
            mime_type="image/x-portable-pixmap",
        )
        second = store.store_artifact(
            manifest,
            content=content,
            suffix=".ppm",
            mime_type="image/x-portable-pixmap",
        )

        self.assertEqual(first, second)
        self.assertEqual(store.counts(manifest)["artifacts"], 1)

    def test_tampered_artifact_is_detected(self) -> None:
        phase_start(self.layout)
        phase_resume_label(self.layout)
        manifest, store = load_session(self.layout)
        artifact = next(
            self.layout.artifact_session_directory(manifest.session_id).glob("*.ppm")
        )
        artifact.write_bytes(artifact.read_bytes() + b"tampered")

        with self.assertRaises(ArtifactIntegrityError):
            store.verify_artifacts(manifest)


class StorageBoundaryTests(Chapter06StorageTestCase):
    """检查 checkpoint、业务证据、产物和长期知识没有混成一个存储桶。"""

    @staticmethod
    def table_names(database: Path) -> set[str]:
        """读取 SQLite 用户表名，忽略 sqlite_* 内部表。"""

        # sqlite3.Connection.__exit__ 只负责提交/回滚，不会关闭 Windows 文件句柄。
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        return {row[0] for row in rows}

    def test_checkpoint_and_evidence_databases_have_separate_schemas(self) -> None:
        phase_start(self.layout)
        checkpoint_tables = self.table_names(self.layout.checkpoint_database)
        evidence_tables = self.table_names(self.layout.evidence_database)

        self.assertIn("checkpoints", checkpoint_tables)
        self.assertNotIn("evidence_records", checkpoint_tables)
        self.assertIn("evidence_records", evidence_tables)
        self.assertNotIn("checkpoints", evidence_tables)
        self.assertNotIn("knowledge", evidence_tables)
        self.assertFalse((self.layout.root / "knowledge").exists())


if __name__ == "__main__":
    unittest.main()
