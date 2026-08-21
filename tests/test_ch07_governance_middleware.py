"""
第 7 章自动化测试：验证治理中间件在成功、拒绝、并发、超时和取消路径都能收口。

这个文件的整体逻辑
------------------
每个测试使用独立临时 governance.sqlite3，并通过真实 GovernanceMiddleware.execute
执行异步 operation。测试不调用 LLM 或摄像头，而是使用可计数、可阻塞、可瞬态失败、
可永久失败的确定性服务替身。这样能够准确回答“operation 到底运行了几次”，而不是只看
中间件最终抛了什么异常。

测试覆盖四层不变量：
1. 执行前：权限白名单、thread lease、command_id 请求哈希和逻辑预算。
2. 执行中：每次尝试扣费、单次 timeout、有限重试、永久错误不重试。
3. 执行后：成功缓存、失败不可自动重放、取消传播、lease 必须释放。
4. 可审计性：事件 sequence 严格递增，拒绝/重试/超时/终态都有持久记录。

使用的技术栈
------------
- unittest.IsolatedAsyncioTestCase：每项异步测试使用隔离事件循环。
- tempfile：为每项测试提供独立 SQLite 文件并在连接关闭后清理。
- asyncio.Task/Event：制造同一 thread 并发、外部取消和合作式服务清理。
- sqlite3：直接模拟租约过期和 stale RUNNING 命令恢复。
- Pydantic 正式模型：所有 policy、command、result 和 audit event 仍经过契约校验。

测试调用流程
------------
python -m unittest discover -s tests -p "test_ch07*.py" -v
    -> asyncSetUp 创建 repository、policy、middleware
    -> 构造 GovernedCommand 与确定性 operation
    -> await middleware.execute(...)
    -> 检查服务调用次数、预算、命令状态、lease 和 audit event
    -> asyncTearDown 后临时目录自动清理

这些测试证明单机 SQLite 治理逻辑，不证明网络分区、多节点时钟一致性或真正分布式锁。
生产副作用服务仍必须把 command_id 作为下游幂等键。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples"
for import_path in (PROJECT_ROOT, EXAMPLES_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from ch07_governance_middleware import (  # noqa: E402
    AuditEventType,
    BudgetExceeded,
    CommandCategory,
    CommandStatus,
    ControlledOperation,
    CountingOperation,
    FlakyOperation,
    GovernedCommand,
    GovernanceMiddleware,
    GovernancePolicy,
    GovernanceRepository,
    HangingOperation,
    IdempotencyConflict,
    LeaseLost,
    OperationAttempt,
    PermissionDenied,
    PreviouslyFailed,
    RetryExhausted,
    ThreadBusy,
    TransientOperationError,
    canonical_policy_json,
)


class PermanentFailureOperation:
    """总是抛普通 ValueError；治理层不得把未知业务错误当瞬态错误重试。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, attempt: OperationAttempt) -> object:
        self.calls += 1
        await asyncio.sleep(0)
        raise ValueError("invalid device specification")


def build_policy(**updates: object) -> GovernancePolicy:
    """创建测试默认策略；每项用例只覆盖自己关心的上限。"""

    values: dict[str, object] = {
        "policy_id": "policy-ch07-tests-v1",
        "allowed_capabilities": frozenset(
            {"perception.observe", "rules.evaluate", "spec.retrieve"}
        ),
        "max_commands": 20,
        "max_observations": 5,
        "max_external_attempts": 30,
        "max_cost_units": 30,
        "max_attempts_per_command": 3,
        # 成功路径会 sleep 2ms。给 Windows/CI 调度留出足够余量，避免把调度抖动
        # 当成业务 timeout；HangingOperation 仍由专门测试验证真实超时和取消。
        "timeout_ms": 100,
        "base_backoff_ms": 1,
        "lease_ms": 1_000,
    }
    values.update(updates)
    return GovernancePolicy.model_validate(values)


class Chapter07GovernanceTestCase(unittest.IsolatedAsyncioTestCase):
    """为每项异步测试建立全新的数据库、session 和中间件。"""

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="realsight-ch07-test-"
        )
        self.database = Path(self.temporary_directory.name) / "governance.sqlite3"
        self.repository = GovernanceRepository(self.database)
        self.policy = build_policy()
        self.middleware = GovernanceMiddleware(self.repository, self.policy)
        self.session_id = "session-ch07-test"

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    def command(
        self,
        command_id: str,
        *,
        capability: str = "spec.retrieve",
        category: CommandCategory = CommandCategory.TOOL,
        cost_units: int = 1,
        payload: dict[str, object] | None = None,
    ) -> GovernedCommand:
        """构造当前测试 session 的命令，减少重复身份字段。"""

        return GovernedCommand(
            command_id=command_id,
            session_id=self.session_id,
            thread_id=self.session_id,
            capability=capability,
            category=category,
            cost_units_per_attempt=cost_units,
            payload={} if payload is None else payload,
        )

    def event_types(self) -> list[AuditEventType]:
        """读取当前 session 审计类型，保持断言简洁。"""

        return [
            event.event_type
            for event in self.repository.audit_events(self.session_id)
        ]


class PermissionAndIdempotencyTests(Chapter07GovernanceTestCase):
    """验证 operation 之前的权限与稳定 command_id 边界。"""

    async def test_permission_denied_does_not_call_or_charge(self) -> None:
        operation = CountingOperation()
        command = self.command(
            "command-denied",
            capability="admin.delete",
        )

        with self.assertRaises(PermissionDenied):
            await self.middleware.execute(command, operation)

        snapshot = self.repository.budget_snapshot(self.session_id)
        self.assertEqual(operation.calls, 0)
        self.assertEqual(snapshot.consumed_commands, 0)
        self.assertEqual(snapshot.consumed_external_attempts, 0)
        self.assertEqual(self.repository.command_count(self.session_id), 0)
        self.assertIn(AuditEventType.PERMISSION_DENIED, self.event_types())

    async def test_successful_replay_uses_persistent_cache_without_charge(self) -> None:
        command = self.command("command-replay", payload={"model": "Book-14"})
        first_operation = CountingOperation({"found": True})
        first = await self.middleware.execute(command, first_operation)
        before = self.repository.budget_snapshot(self.session_id)

        # 新建 Repository/Middleware 对象，证明缓存不在 Python 实例字段中。
        second_repository = GovernanceRepository(self.database)
        second_middleware = GovernanceMiddleware(second_repository, self.policy)
        replay_operation = CountingOperation({"should_not_run": True})
        replay = await second_middleware.execute(command, replay_operation)
        after = second_repository.budget_snapshot(self.session_id)

        self.assertFalse(first.cached)
        self.assertTrue(replay.cached)
        self.assertEqual(replay.value, {"found": True})
        self.assertEqual(first_operation.calls, 1)
        self.assertEqual(replay_operation.calls, 0)
        self.assertEqual(before, after)
        self.assertIn(AuditEventType.IDEMPOTENCY_REPLAYED, self.event_types())

    async def test_same_command_id_with_changed_payload_is_rejected(self) -> None:
        original = self.command("command-conflict", payload={"query": "A"})
        changed = self.command("command-conflict", payload={"query": "B"})
        await self.middleware.execute(original, CountingOperation())
        operation = CountingOperation()

        with self.assertRaises(IdempotencyConflict):
            await self.middleware.execute(changed, operation)

        self.assertEqual(operation.calls, 0)
        self.assertEqual(self.repository.command_count(self.session_id), 1)
        self.assertIn(AuditEventType.IDEMPOTENCY_CONFLICT, self.event_types())

    async def test_failed_command_replay_does_not_repeat_side_effect(self) -> None:
        command = self.command("command-failed-replay")
        operation = PermanentFailureOperation()
        with self.assertRaises(ValueError):
            await self.middleware.execute(command, operation)

        with self.assertRaises(PreviouslyFailed):
            await self.middleware.execute(command, operation)

        self.assertEqual(operation.calls, 1)
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.FAILED,
        )
        self.assertIn(AuditEventType.PREVIOUS_FAILURE_REPLAYED, self.event_types())


class RetryTimeoutAndBudgetTests(Chapter07GovernanceTestCase):
    """验证每次真实外部尝试的预算、超时和有限重试。"""

    async def test_transient_error_retries_then_succeeds(self) -> None:
        command = self.command(
            "command-flaky",
            category=CommandCategory.OBSERVATION,
            capability="perception.observe",
            cost_units=2,
        )
        operation = FlakyOperation(failures_before_success=2)

        result = await self.middleware.execute(command, operation)
        snapshot = self.repository.budget_snapshot(self.session_id)

        self.assertEqual(result.attempts, 3)
        self.assertEqual(operation.calls, 3)
        self.assertEqual(snapshot.consumed_commands, 1)
        self.assertEqual(snapshot.consumed_observations, 1)
        self.assertEqual(snapshot.consumed_external_attempts, 3)
        self.assertEqual(snapshot.consumed_cost_units, 6)
        self.assertEqual(self.event_types().count(AuditEventType.RETRY_SCHEDULED), 2)

    async def test_timeout_cancels_each_attempt_then_exhausts_retry(self) -> None:
        command = self.command("command-timeout", cost_units=1)
        operation = HangingOperation()

        with self.assertRaises(RetryExhausted):
            await self.middleware.execute(command, operation)

        self.assertEqual(operation.calls, 3)
        self.assertEqual(operation.cancellations, 3)
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.FAILED,
        )
        self.assertEqual(self.repository.active_lease_count(self.session_id), 0)
        self.assertEqual(
            self.event_types().count(AuditEventType.ATTEMPT_TIMED_OUT),
            3,
        )

    async def test_permanent_error_is_not_retried(self) -> None:
        command = self.command("command-permanent")
        operation = PermanentFailureOperation()

        with self.assertRaisesRegex(ValueError, "invalid device"):
            await self.middleware.execute(command, operation)

        snapshot = self.repository.budget_snapshot(self.session_id)
        self.assertEqual(operation.calls, 1)
        self.assertEqual(snapshot.consumed_external_attempts, 1)
        self.assertNotIn(AuditEventType.RETRY_SCHEDULED, self.event_types())

    async def test_observation_quota_rejects_before_command_is_inserted(self) -> None:
        self.policy = build_policy(max_observations=1)
        self.middleware = GovernanceMiddleware(self.repository, self.policy)
        first = self.command(
            "command-observation-first",
            capability="perception.observe",
            category=CommandCategory.OBSERVATION,
            cost_units=0,
        )
        second = self.command(
            "command-observation-second",
            capability="perception.observe",
            category=CommandCategory.OBSERVATION,
            cost_units=0,
        )
        await self.middleware.execute(first, CountingOperation())
        second_operation = CountingOperation()

        with self.assertRaisesRegex(BudgetExceeded, "observation"):
            await self.middleware.execute(second, second_operation)

        self.assertEqual(second_operation.calls, 0)
        self.assertIsNone(
            self.repository.command_status(self.session_id, second.command_id)
        )
        self.assertEqual(self.repository.command_count(self.session_id), 1)

    async def test_cost_budget_fails_after_claim_but_before_operation(self) -> None:
        self.policy = build_policy(max_cost_units=1)
        self.middleware = GovernanceMiddleware(self.repository, self.policy)
        command = self.command("command-too-costly", cost_units=2)
        operation = CountingOperation()

        with self.assertRaisesRegex(BudgetExceeded, "cost"):
            await self.middleware.execute(command, operation)

        snapshot = self.repository.budget_snapshot(self.session_id)
        self.assertEqual(operation.calls, 0)
        self.assertEqual(snapshot.consumed_commands, 1)
        self.assertEqual(snapshot.consumed_external_attempts, 0)
        self.assertEqual(snapshot.consumed_cost_units, 0)
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.FAILED,
        )

    async def test_global_attempt_budget_can_stop_a_retry_loop(self) -> None:
        self.policy = build_policy(max_external_attempts=1)
        self.middleware = GovernanceMiddleware(self.repository, self.policy)
        command = self.command("command-attempt-budget", cost_units=0)
        operation = FlakyOperation(failures_before_success=5)

        with self.assertRaisesRegex(BudgetExceeded, "attempt"):
            await self.middleware.execute(command, operation)

        self.assertEqual(operation.calls, 1)
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.FAILED,
        )


class ConcurrencyCancellationAndLeaseTests(Chapter07GovernanceTestCase):
    """验证同 thread 串行、取消清理和过期 owner 的提交栅栏。"""

    async def test_second_command_is_busy_while_first_holds_thread_lease(self) -> None:
        controlled = ControlledOperation()
        first = self.command("command-holds-lease", cost_units=0)
        first_task = asyncio.create_task(self.middleware.execute(first, controlled))
        await controlled.started.wait()
        second_operation = CountingOperation()

        with self.assertRaises(ThreadBusy):
            await self.middleware.execute(
                self.command("command-busy", cost_units=0),
                second_operation,
            )
        controlled.release.set()
        await first_task

        self.assertEqual(second_operation.calls, 0)
        self.assertEqual(self.repository.active_lease_count(self.session_id), 0)
        self.assertIn(AuditEventType.THREAD_BUSY, self.event_types())

    async def test_external_cancellation_marks_command_and_releases_lease(self) -> None:
        controlled = ControlledOperation()
        command = self.command("command-cancelled", cost_units=0)
        task = asyncio.create_task(self.middleware.execute(command, controlled))
        await controlled.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.CANCELLED,
        )
        self.assertEqual(self.repository.active_lease_count(self.session_id), 0)
        # 取消后新命令能立即获取同一 thread，证明 finally 没有漏掉清理。
        follow_up = await self.middleware.execute(
            self.command("command-after-cancel", cost_units=0),
            CountingOperation({"recovered": True}),
        )
        self.assertEqual(follow_up.value, {"recovered": True})
        self.assertIn(AuditEventType.COMMAND_CANCELLED, self.event_types())

    async def test_expired_running_command_is_recovered_without_double_claim_charge(self) -> None:
        command = self.command("command-stale", cost_units=0)
        self.repository.register_session(
            session_id=self.session_id,
            thread_id=self.session_id,
            policy=self.policy,
        )
        old_owner = "old-owner"
        self.assertTrue(
            self.repository.acquire_lease(
                command,
                owner_token=old_owner,
                lease_ms=1,
            )
        )
        claim = self.repository.claim_command(command, owner_token=old_owner)
        self.assertEqual(claim.disposition.value, "new")
        await asyncio.sleep(0.01)

        result = await self.middleware.execute(command, CountingOperation())
        snapshot = self.repository.budget_snapshot(self.session_id)

        self.assertEqual(result.value, {"ok": True})
        self.assertEqual(snapshot.consumed_commands, 1)
        self.assertIn(AuditEventType.STALE_COMMAND_RECOVERED, self.event_types())

    async def test_lost_lease_blocks_late_success_commit(self) -> None:
        command = self.command("command-lost-lease", cost_units=0)
        self.repository.register_session(
            session_id=self.session_id,
            thread_id=self.session_id,
            policy=self.policy,
        )
        owner = "owner-that-expires"
        # 先给足时间完成 claim/reserve，再等待它明确过期，避免 Windows 调度抖动。
        self.repository.acquire_lease(command, owner_token=owner, lease_ms=50)
        self.repository.claim_command(command, owner_token=owner)
        reservation = self.repository.reserve_attempt(command, owner_token=owner)
        self.assertTrue(reservation.allowed)
        await asyncio.sleep(0.07)

        with self.assertRaises(LeaseLost):
            self.repository.mark_succeeded(
                command,
                owner_token=owner,
                result={"late": True},
                duration_ms=10,
            )
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.RUNNING,
        )

    async def test_middleware_audits_lease_lost_before_late_result(self) -> None:
        """operation 返回前让 lease 过期，execute 应拒绝结果并留下 LEASE_LOST。"""

        command = self.command("command-audited-lease-loss", cost_units=0)

        async def expire_own_lease(attempt: OperationAttempt) -> object:
            with self.repository.connection() as connection:
                connection.execute(
                    "UPDATE thread_leases SET expires_at = 0 WHERE thread_id = ?",
                    (attempt.command.thread_id,),
                )
            return {"late": True}

        with self.assertRaises(LeaseLost):
            await self.middleware.execute(command, expire_own_lease)

        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.RUNNING,
        )
        self.assertIn(AuditEventType.LEASE_LOST, self.event_types())
        self.assertEqual(self.repository.active_lease_count(self.session_id), 0)

    async def test_recovered_command_with_no_attempts_left_fails_without_call(self) -> None:
        """模拟三次尝试后进程崩溃；新 owner 不能偷偷执行第四次。"""

        command = self.command("command-stale-exhausted", cost_units=0)
        self.repository.register_session(
            session_id=self.session_id,
            thread_id=self.session_id,
            policy=self.policy,
        )
        owner = "owner-before-crash"
        self.repository.acquire_lease(command, owner_token=owner, lease_ms=1_000)
        self.repository.claim_command(command, owner_token=owner)
        for _ in range(self.policy.max_attempts_per_command):
            reservation = self.repository.reserve_attempt(
                command,
                owner_token=owner,
            )
            self.assertTrue(reservation.allowed)

        # 直接把 lease 设为过期，模拟旧进程消失而 RUNNING 命令留在数据库。
        with self.repository.connection() as connection:
            connection.execute(
                "UPDATE thread_leases SET expires_at = 0 WHERE thread_id = ?",
                (self.session_id,),
            )
        operation = CountingOperation()

        with self.assertRaisesRegex(RetryExhausted, "already exhausted"):
            await self.middleware.execute(command, operation)

        self.assertEqual(operation.calls, 0)
        self.assertEqual(
            self.repository.command_status(self.session_id, command.command_id),
            CommandStatus.FAILED,
        )


class AuditAndPolicyTests(Chapter07GovernanceTestCase):
    """验证治理配置不可中途改变，审计记录可稳定读取。"""

    async def test_audit_sequence_is_strict_and_data_is_json_round_trippable(self) -> None:
        command = self.command("command-audit", cost_units=0)
        await self.middleware.execute(command, CountingOperation({"value": 7}))
        events = self.repository.audit_events(self.session_id)

        self.assertEqual(
            [event.sequence for event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[0].event_type, AuditEventType.LEASE_ACQUIRED)
        self.assertIn(AuditEventType.COMMAND_SUCCEEDED, self.event_types())
        self.assertEqual(events[-1].event_type, AuditEventType.LEASE_RELEASED)

    async def test_policy_cannot_change_inside_existing_session(self) -> None:
        await self.middleware.execute(
            self.command("command-policy-original", cost_units=0),
            CountingOperation(),
        )
        changed_policy = build_policy(max_commands=19)
        changed_middleware = GovernanceMiddleware(self.repository, changed_policy)
        operation = CountingOperation()

        with self.assertRaisesRegex(IdempotencyConflict, "policy"):
            await changed_middleware.execute(
                self.command("command-policy-changed", cost_units=0),
                operation,
            )
        self.assertEqual(operation.calls, 0)

    async def test_policy_fingerprint_sorts_unordered_capabilities(self) -> None:
        """权限集合的输入顺序不应让两个等价 policy 产生不同持久 JSON。"""

        first = build_policy(
            allowed_capabilities=frozenset(
                ["spec.retrieve", "perception.observe", "rules.evaluate"]
            )
        )
        second = build_policy(
            allowed_capabilities=frozenset(
                ["rules.evaluate", "spec.retrieve", "perception.observe"]
            )
        )

        self.assertEqual(canonical_policy_json(first), canonical_policy_json(second))
        self.assertIn(
            '"allowed_capabilities":["perception.observe","rules.evaluate","spec.retrieve"]',
            canonical_policy_json(first),
        )


if __name__ == "__main__":
    unittest.main()
