from __future__ import annotations

import asyncio
import threading

import pytest

from src.app import App
from src.executor.bmc_executor import BMCExecutor
from src.executor.ssh_executor import SSHExecutor
from src.models.execution_result import ExecutionResult
from src.models.task import resolve_task_timeout_seconds
from src.out.collector import compute_summary


class _Task:
    timeout_seconds = 0
    retry_count = 0


def test_ssh_timeout_options_are_task_scoped():
    executor = SSHExecutor(command_timeout=60, idle_timeout=5)

    short = _Task()
    short.timeout_seconds = 5
    long = _Task()
    long.timeout_seconds = 900
    fallback = _Task()
    fallback.timeout_seconds = 0

    assert executor._resolve_execution_options(short).command_timeout == 5
    assert executor._resolve_execution_options(long).command_timeout == 900
    assert executor._resolve_execution_options(fallback).command_timeout == 60
    assert executor.command_timeout == 60


def test_ssh_timeout_options_can_be_overridden_by_device_group():
    executor = SSHExecutor(command_timeout=60, idle_timeout=5)
    task = _Task()
    task.timeout_seconds = 900
    object.__setattr__(task, "_per_group_timeout_seconds", {"A3": 900, "L1": 60, "L2": "45"})

    assert resolve_task_timeout_seconds(task, "A3", fallback=60) == 900
    assert resolve_task_timeout_seconds(task, "L1", fallback=60) == 60
    assert resolve_task_timeout_seconds(task, "L2", fallback=60) == 45
    assert resolve_task_timeout_seconds(task, "UNKNOWN", fallback=60) == 900
    assert executor._resolve_execution_options(task, "A3").command_timeout == 900
    assert executor._resolve_execution_options(task, "L1").command_timeout == 60
    assert executor._resolve_execution_options(task, "L2").command_timeout == 45


def test_ssh_timeout_options_do_not_cross_contaminate():
    executor = SSHExecutor(command_timeout=60, idle_timeout=5)
    barrier = threading.Barrier(2)
    observed = []

    def resolve(value):
        task = _Task()
        task.timeout_seconds = value
        barrier.wait()
        observed.append(executor._resolve_execution_options(task).command_timeout)

    threads = [threading.Thread(target=resolve, args=(5,)), threading.Thread(target=resolve, args=(900,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(observed) == [5, 900]
    assert executor.command_timeout == 60


def test_app_rejects_concurrent_run():
    class Config:
        output_root = "/tmp/test"

    app = App(Config())
    assert app._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="RUN_ALREADY_ACTIVE"):
            app.run_with_plans([])
    finally:
        app._run_lock.release()


def test_summary_tracks_unknown_statuses():
    result = ExecutionResult(plan_id="p", device_name="d", execution_status="EXEC_NEW_STATE")
    summary = compute_summary([result])
    assert summary["total"] == 1
    assert summary["unknown"] == 1
    assert summary["unknown_statuses"] == {"EXEC_NEW_STATE": 1}


def test_invalid_bmc_actions_json_is_failed():
    class Task:
        task_name = "invalid"
        actions_json = "{invalid"

    result = ExecutionResult(plan_id="p", device_name="d", execution_status="EXEC_SUCCESS")
    executor = object.__new__(BMCExecutor)
    asyncio.run(executor._run_bmc_actions(None, Task(), "", "", result))

    assert result.execution_status == "EXEC_FAILED"
    assert "JSON" in result.execution_failure_reason
