from __future__ import annotations

import time
from concurrent.futures import Future

import pytest

from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.out.collector import compute_summary
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.resource_registry import ResourceRegistry
from src.scheduler.worker_pool import (
    DispatchNotCommittedError,
    WorkerPool,
)


def _plan(plan_id: str = "plan-1") -> TaskPlan:
    return TaskPlan(
        plan_id=plan_id,
        device=Device(
            row_index=1,
            device_name="D1",
            device_group="A3",
            bmc_ip="10.0.0.1",
            bmc_username="admin",
            bmc_password="secret",
            inband_ip="10.0.0.2",
            inband_username="root",
            inband_password="secret",
        ),
        task=Task(
            row_index=1,
            sequence=1,
            task_name="T1",
            task_type="SSH",
            execution_mode="SSH_CMD",
            command_or_url="true",
        ),
    )


def _result(plan: TaskPlan, status: str) -> ExecutionResult:
    return ExecutionResult(
        plan_id=plan.plan_id,
        device_name=plan.device.device_name,
        task_name=plan.task.task_name,
        execution_status=status,
        started_at=time.time(),
        ended_at=time.time(),
    )


def _scheduler() -> DynamicScheduler:
    config = AppConfig()
    config.base_bmc_workers = 1
    config.max_bmc_workers = 1
    config.base_ssh_workers = 1
    config.max_ssh_workers = 1
    return DynamicScheduler(config)


@pytest.mark.parametrize(
    ("first_status", "second_status", "expected"),
    [
        ("EXEC_SUCCESS", "EXEC_SKIPPED_STOPPED", "EXEC_SUCCESS"),
        ("EXEC_SKIPPED_STOPPED", "EXEC_SUCCESS", "EXEC_SUCCESS"),
        ("EXEC_SKIPPED_ROUTE_CHANGED", "EXEC_FAILED", "EXEC_FAILED"),
        ("EXEC_TIMEOUT", "EXEC_ERROR", "EXEC_TIMEOUT"),
    ],
)
def test_result_guard_keeps_one_final_result(
    caplog, first_status, second_status, expected,
):
    scheduler = _scheduler()
    plan = _plan()

    assert scheduler._append_result_once(
        plan.plan_id, _result(plan, first_status), "first",
    )
    scheduler._append_result_once(
        plan.plan_id, _result(plan, second_status), "second",
    )

    assert len(scheduler.results) == 1
    assert scheduler.results[0].execution_status == expected
    assert "Duplicate final result" in caplog.text
    summary = compute_summary(scheduler.results)
    assert summary["total"] == 1


def test_result_guard_batch_closes_by_unique_plan_id():
    scheduler = _scheduler()
    plans = [_plan(f"plan-{index}") for index in range(4)]
    for plan in plans:
        scheduler._append_result_once(
            plan.plan_id, _result(plan, "EXEC_SKIPPED_STOPPED"), "stop",
        )
        scheduler._append_result_once(
            plan.plan_id, _result(plan, "EXEC_SUCCESS"), "late_callback",
        )

    assert len(scheduler.results) == len(plans)
    assert len({result.plan_id for result in scheduler.results}) == len(plans)
    assert compute_summary(scheduler.results)["total"] == len(plans)


class _SubmitFailExecutor:
    def submit(self, fn):
        raise RuntimeError("submit failed before commit")


class _CallbackRegistrationFailFuture(Future):
    def add_done_callback(self, fn):
        raise RuntimeError("callback registration failed after commit")


class _CommittedExecutor:
    def __init__(self, result):
        self.result = result
        self.submit_count = 0

    def submit(self, fn):
        self.submit_count += 1
        future = _CallbackRegistrationFailFuture()
        future.set_result(self.result)
        return future


def test_dispatch_submit_failure_is_not_committed_and_cleans_resource():
    pool = WorkerPool("precommit", 1, 1)
    pool._executor = _SubmitFailExecutor()

    with pytest.raises(DispatchNotCommittedError):
        pool.dispatch(lambda: None, "INBAND:10.0.0.2:22")

    assert not pool.resource_has_running_task("INBAND:10.0.0.2:22")
    assert not pool._active_futures


def test_scheduler_requeues_only_not_committed_dispatch():
    scheduler = _scheduler()
    plan = _plan()
    endpoint = plan.endpoint_key
    scheduler._execution_id = "exec-1"
    scheduler._registry._reset_for_test()
    scheduler._build_endpoint_queues([plan])
    scheduler._ssh_pool._executor = _SubmitFailExecutor()

    scheduler._dispatch()

    assert list(scheduler._endpoint_queues[endpoint]) == [plan]
    assert endpoint in scheduler._ready_endpoints
    assert not scheduler._registry.is_held(endpoint)
    assert scheduler.results == []


def test_committed_callback_registration_failure_recovers_without_requeue(caplog):
    scheduler = _scheduler()
    plan = _plan()
    endpoint = plan.endpoint_key
    result = _result(plan, "EXEC_SUCCESS")
    committed_executor = _CommittedExecutor(result)

    scheduler._execution_id = "exec-2"
    scheduler._registry._reset_for_test()
    scheduler._build_endpoint_queues([plan])
    scheduler._ssh_pool._executor = committed_executor

    scheduler._dispatch()

    deadline = time.time() + 2
    while len(scheduler.results) < 1 and time.time() < deadline:
        time.sleep(0.01)

    assert committed_executor.submit_count == 1
    assert list(scheduler._endpoint_queues[endpoint]) == []
    assert endpoint not in scheduler._ready_endpoints
    assert len(scheduler.results) == 1
    assert scheduler.results[0].execution_status == "EXEC_SUCCESS"
    assert not scheduler._registry.is_held(endpoint)
    assert not scheduler._ssh_pool.resource_has_running_task(endpoint)
    assert "DISPATCH_COMMITTED_CALLBACK_REGISTRATION_FAILED" in caplog.text


def test_normal_committed_dispatch_releases_worker_resource():
    pool = WorkerPool("normal", 1, 1)
    completed = []
    handle = pool.dispatch(
        lambda: "ok",
        "INBAND:10.0.0.2:22",
        on_complete=completed.append,
    )
    assert handle.committed is True
    assert handle.future is not None
    handle.future.result(timeout=2)

    deadline = time.time() + 2
    while not completed and time.time() < deadline:
        time.sleep(0.01)

    assert completed == ["ok"]
    assert not pool.resource_has_running_task("INBAND:10.0.0.2:22")
    pool.shutdown(wait=True)
