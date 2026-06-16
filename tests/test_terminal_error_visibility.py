from __future__ import annotations

from io import StringIO

import pytest

from src.cli.fatal import run_with_terminal_fault_guard
from src.executor.retry import execute_with_retry
from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.scheduler.dynamic_scheduler import DynamicScheduler


def _plan() -> TaskPlan:
    return TaskPlan(
        plan_id="plan-1",
        device=Device(
            row_index=0,
            device_name="D1",
            device_group="G1",
            bmc_ip="",
            bmc_username="",
            bmc_password="",
            inband_ip="",
            inband_username="",
            inband_password="",
            enabled=True,
        ),
        task=Task(
            row_index=0,
            sequence=1,
            task_name="T1",
            task_type="SSH",
            execution_mode="SSH_CMD",
            match_group="G1",
            command_or_url="display version",
            enabled=True,
        ),
    )


def test_terminal_fault_guard_prints_visible_redacted_error():
    stream = StringIO()

    def crash():
        raise RuntimeError("boom password=plain-secret")

    code = run_with_terminal_fault_guard(crash, stream=stream)
    output = stream.getvalue()

    assert code == 1
    assert "[FATAL]" in output
    assert "RuntimeError" in output
    assert "boom" in output
    assert "plain-secret" not in output


def test_terminal_fault_guard_reports_keyboard_interrupt():
    stream = StringIO()

    def interrupt():
        raise KeyboardInterrupt()

    code = run_with_terminal_fault_guard(interrupt, stream=stream)

    assert code == 130
    assert "用户中断" in stream.getvalue()


def test_retry_wrapper_does_not_swallow_keyboard_interrupt():
    class Executor:
        def execute(self, plan, output_root):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        execute_with_retry(Executor(), _plan(), "output")


def test_dynamic_scheduler_execute_plan_does_not_swallow_keyboard_interrupt(monkeypatch, tmp_path):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("src.scheduler.dynamic_scheduler.execute_with_retry", interrupt)
    scheduler = DynamicScheduler(AppConfig(output_root=str(tmp_path), preflight_enabled=False))

    with pytest.raises(KeyboardInterrupt):
        scheduler._execute_plan(_plan())
