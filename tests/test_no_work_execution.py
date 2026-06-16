from __future__ import annotations

from src.app import App
from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.connectivity.preflight import PreflightReport, PreflightResult


def _device(group: str = "G1") -> Device:
    return Device(
        row_index=0,
        device_name="D1",
        device_group=group,
        bmc_ip="",
        bmc_username="",
        bmc_password="",
        inband_ip="",
        inband_username="",
        inband_password="",
        enabled=True,
    )


def _task(*, enabled: bool, match_group: str = "G1", task_type: str = "SSH") -> Task:
    return Task(
        row_index=0,
        sequence=1,
        task_name="task-1",
        task_type=task_type,
        execution_mode="SSH_CMD",
        match_group=match_group,
        command_or_url="display version",
        enabled=enabled,
    )


def test_app_returns_no_work_when_all_tasks_disabled_before_validation(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    disabled_invalid_task = _task(enabled=False, task_type="INVALID")
    monkeypatch.setattr("src.app.load_all", lambda path: ([_device()], [disabled_invalid_task]))

    def fail_if_validate_runs(devices, tasks):
        raise AssertionError("disabled tasks should not force execution validation")

    monkeypatch.setattr("src.app.validate", fail_if_validate_runs)

    results = app.run("unused.xlsx", mode="sequential")

    assert results == []
    status = app.current_no_work_status()
    assert status["reason"] == "NO_ENABLED_TASKS"
    assert "无可用任务" in status["message"]


def test_app_empty_plan_from_group_mismatch_is_not_no_work(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    monkeypatch.setattr("src.app.load_all", lambda path: ([_device("G1")], [_task(enabled=True, match_group="G2")]))

    results = app.run("unused.xlsx", mode="sequential")

    assert results == []
    assert app.current_no_work_status()["reason"] == ""
    assert app.current_batch_error_status()["reason"] == "NO_EXECUTABLE_PLANS"


def test_no_work_status_clears_on_next_run_with_plans(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    monkeypatch.setattr("src.app.load_all", lambda path: ([_device()], [_task(enabled=False)]))

    assert app.run("unused.xlsx", mode="sequential") == []
    assert app.current_no_work_status()["reason"] == "NO_ENABLED_TASKS"

    app.run_with_plans([], mode="sequential")

    assert app.current_no_work_status()["reason"] == ""


def test_app_records_batch_error_when_all_plans_precheck_timeout(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=True))
    task = _task(enabled=True, match_group="G1")
    monkeypatch.setattr("src.app.load_all", lambda path: ([_device()], [task]))

    def fake_preflight(devices, timeout=5.0, max_workers=1):
        return PreflightReport(
            results=[
                PreflightResult(
                    device_name="D1",
                    ssh_status="TIMEOUT",
                    ssh_error="连接超时",
                )
            ],
            total=1,
        )

    monkeypatch.setattr("src.app.preflight_check_all", fake_preflight)

    results = app.run("unused.xlsx", mode="sequential")

    assert len(results) == 1
    assert results[0].execution_status == "EXEC_SKIPPED_PRECHECK_FAILED"
    assert "TIMEOUT" in results[0].execution_failure_reason
    batch_error = app.current_batch_error_status()
    assert batch_error["reason"] == "ALL_PLANS_PRECHECK_FAILED"
    assert "所有需执行任务均因网络预检失败被跳过" in batch_error["message"]


def test_run_with_plans_records_batch_error_when_all_precheck_timeout(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=True))
    plan = TaskPlan(plan_id="plan-1", device=_device(), task=_task(enabled=True))

    def fake_preflight(devices, timeout=5.0, max_workers=1):
        return PreflightReport(
            results=[
                PreflightResult(
                    device_name="D1",
                    ssh_status="TIMEOUT",
                    ssh_error="连接超时",
                )
            ],
            total=1,
        )

    monkeypatch.setattr("src.app.preflight_check_all", fake_preflight)

    results = app.run_with_plans([plan], mode="sequential")

    assert len(results) == 1
    assert results[0].execution_status == "EXEC_SKIPPED_PRECHECK_FAILED"
    assert app.current_batch_error_status()["reason"] == "ALL_PLANS_PRECHECK_FAILED"
