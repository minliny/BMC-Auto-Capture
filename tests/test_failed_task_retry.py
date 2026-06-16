from __future__ import annotations

from src.app import App
from src.cli.failed_retry import (
    failed_result_count,
    is_failed_result,
    is_retryable_failed_result,
    merge_retry_results,
    prompt_retry_failed_tasks,
)
from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.scheduler.dynamic_scheduler import DynamicScheduler


class _Tty:
    def __init__(self, value: bool):
        self._value = value

    def isatty(self) -> bool:
        return self._value


def _plan(plan_id: str, device_name: str, task_name: str) -> TaskPlan:
    device = Device(
        row_index=0,
        device_name=device_name,
        device_group="G1",
        bmc_ip="",
        bmc_username="",
        bmc_password="",
        inband_ip="192.0.2.10",
        inband_username="u",
        inband_password="p",
    )
    task = Task(
        row_index=0,
        sequence=1,
        task_name=task_name,
        task_type="SSH",
        execution_mode="SSH_CMD",
        command_or_url="display version",
    )
    return TaskPlan(device=device, task=task, plan_id=plan_id)


def _bmc_plan(plan_id: str, device_name: str, task_name: str) -> TaskPlan:
    device = Device(
        row_index=0,
        device_name=device_name,
        device_group="G1",
        bmc_ip="",
        bmc_username="",
        bmc_password="",
        inband_ip="",
        inband_username="",
        inband_password="",
    )
    task = Task(
        row_index=0,
        sequence=1,
        task_name=task_name,
        task_type="BMC",
        execution_mode="BMC_URL",
        command_or_url="/",
    )
    return TaskPlan(
        device=device,
        task=task,
        plan_id=plan_id,
        task_id=f"task-{plan_id}",
        client_task_id=f"client-{plan_id}",
    )


def _result(plan: TaskPlan, status: str) -> ExecutionResult:
    return ExecutionResult(
        plan_id=plan.plan_id,
        device_name=plan.device.device_name,
        device_group=plan.device.device_group,
        inband_ip=plan.device.inband_ip,
        task_name=plan.task.task_name,
        task_type=plan.task.task_type,
        execution_mode=plan.task.execution_mode,
        execution_status=status,
    )


def test_app_retries_only_failed_tasks_from_last_batch(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    ok_plan = _plan("p-ok", "D1", "ok-task")
    failed_plan = _plan("p-failed", "D2", "failed-task")
    precheck_plan = _plan("p-precheck", "D3", "precheck-task")
    app._remember_last_plans([ok_plan, failed_plan, precheck_plan])

    original = [
        _result(ok_plan, "EXEC_SUCCESS"),
        _result(failed_plan, "EXEC_FAILED"),
        _result(precheck_plan, "EXEC_SKIPPED_PRECHECK_FAILED"),
    ]
    captured: list[TaskPlan] = []

    def fake_run_with_plans(plans, mode="sequential"):
        captured.extend(plans)
        return [_result(plans[0], "EXEC_SUCCESS")]

    monkeypatch.setattr(app, "run_with_plans", fake_run_with_plans)

    retry_results = app.retry_failed_tasks(original, mode="full")

    assert [plan.device.device_name for plan in captured] == ["D2"]
    assert retry_results[0].execution_status == "EXEC_SUCCESS"
    assert captured[0] is not failed_plan


def test_prompt_retry_failed_tasks_runs_when_user_enters_zero():
    failed = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_FAILED")
    retry = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_SUCCESS")
    calls: list[str] = []
    report_calls: list[list[ExecutionResult]] = []

    class FakeApp:
        config = type("Config", (), {"output_root": "/tmp/redacted"})()
        merged_state: list[ExecutionResult] = []
        stop_metadata: dict = {}

        def failed_retry_candidates(self, results):
            return [object()]

        def retry_failed_tasks(self, results, mode):
            calls.append(mode)
            return [retry]

        def current_stop_metadata(self):
            return {"stopReason": "ROUTE_GUARD_STOPPED"}

        def replace_results_after_retry(self, merged_results):
            self.merged_state = list(merged_results)

        def write_retry_merged_reports(self, merged_results, output_dir="", stop_metadata=None):
            self.stop_metadata = dict(stop_metadata or {})
            report_calls.append(list(merged_results))
            return "/tmp/redacted/retry_merged"

    app = FakeApp()
    merged = prompt_retry_failed_tasks(
        app,
        [failed],
        mode="full",
        input_func=lambda prompt: "0",
        output_func=lambda text: None,
        stdin=_Tty(True),
    )

    assert calls == ["full"]
    assert failed_result_count(merged) == 0
    assert merged == [retry]
    assert report_calls == [[retry]]
    assert app.merged_state == [retry]
    assert app.stop_metadata == {"stopReason": "ROUTE_GUARD_STOPPED"}


def test_prompt_retry_failed_tasks_skips_non_interactive_input():
    failed = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_FAILED")

    class FakeApp:
        def failed_retry_candidates(self, results):
            raise AssertionError("non-interactive runs must not inspect retry input")

        def retry_failed_tasks(self, results, mode):
            raise AssertionError("non-interactive runs must not retry")

    returned = prompt_retry_failed_tasks(
        FakeApp(),
        [failed],
        mode="sequential",
        input_func=lambda prompt: (_ for _ in ()).throw(AssertionError("input called")),
        output_func=lambda text: None,
        stdin=_Tty(False),
    )

    assert returned == [failed]


def test_merge_retry_results_replaces_only_matching_failed_rows():
    ok = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_SUCCESS")
    failed = ExecutionResult("p2", "D2", task_name="T2", execution_status="EXEC_FAILED")
    retry = ExecutionResult("p2", "D2", task_name="T2", execution_status="EXEC_SUCCESS")

    merged = merge_retry_results([ok, failed], [retry])

    assert merged == [ok, retry]
    assert failed_result_count(merged) == 0


def test_merge_retry_results_does_not_match_on_bare_plan_id():
    failed_one = ExecutionResult("batch-1", "D1", task_name="T1", execution_status="EXEC_FAILED")
    failed_two = ExecutionResult("batch-1", "D2", task_name="T2", execution_status="EXEC_FAILED")
    retry_two = ExecutionResult("batch-1", "D2", task_name="T2", execution_status="EXEC_SUCCESS")

    merged = merge_retry_results([failed_one, failed_two], [retry_two])

    assert merged == [failed_one, retry_two]
    assert failed_result_count(merged) == 1


def test_merge_retry_results_does_not_append_unmatched_retry_rows():
    failed = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_FAILED")
    unrelated_retry = ExecutionResult("p2", "D2", task_name="T2", execution_status="EXEC_SUCCESS")

    merged = merge_retry_results([failed], [unrelated_retry])

    assert merged == [failed]
    assert failed_result_count(merged) == 1


def test_success_status_with_rule_checkpoint_artifact_or_verdict_failure_is_failed():
    cases = [
        ExecutionResult("p1", "D1", task_name="rule", execution_status="EXEC_SUCCESS",
                        rule_status="RULE_FAILED"),
        ExecutionResult("p2", "D2", task_name="check", execution_status="EXEC_SUCCESS",
                        checkpoint_status="CHECK_FAIL"),
        ExecutionResult("p3", "D3", task_name="artifact", execution_status="EXEC_SUCCESS",
                        artifact_status="ARTIFACT_FAILED"),
        ExecutionResult("p4", "D4", task_name="verdict", execution_status="EXEC_SUCCESS",
                        final_verdict="FAIL"),
    ]

    assert all(is_failed_result(result) for result in cases)
    assert all(is_retryable_failed_result(result) for result in cases)


def test_skipped_precheck_port_and_disabled_are_failed_but_not_retryable():
    statuses = [
        "EXEC_SKIPPED_PRECHECK_FAILED",
        "EXEC_SKIPPED_PORT_BLOCKED",
        "EXEC_SKIPPED_DISABLED",
        "EXEC_SKIPPED_ROUTE_CHANGED",
        "EXEC_SKIPPED_STOPPED",
    ]
    results = [
        ExecutionResult(f"p{i}", f"D{i}", task_name=f"T{i}", execution_status=status)
        for i, status in enumerate(statuses)
    ]

    assert failed_result_count(results) == len(statuses)
    assert not any(is_retryable_failed_result(result) for result in results)


def test_dynamic_scheduler_fills_worker_crash_result_identity(tmp_path):
    scheduler = DynamicScheduler(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    plan = _plan("p-worker", "D1", "ssh-task")
    plan.task_id = "task-worker"
    plan.client_task_id = "client-worker"
    synthetic = ExecutionResult(
        plan_id="",
        device_name=plan.endpoint_key,
        task_name="(crashed)",
        execution_status="EXEC_ERROR",
        execution_failure_reason="Worker exception",
    )

    scheduler._on_plan_done(plan, synthetic, plan.endpoint_key)

    stored = scheduler._results[0]
    assert stored.plan_id == "p-worker"
    assert stored.task_id == "task-worker"
    assert stored.client_task_id == "client-worker"
    assert stored.device_name == "D1"
    assert stored.task_name == "ssh-task"


def test_dynamic_scheduler_recovers_bmc_group_worker_crash_per_plan(tmp_path):
    scheduler = DynamicScheduler(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    first = _bmc_plan("p-bmc-1", "BMC-1", "health")
    second = _bmc_plan("p-bmc-2", "BMC-1", "inventory")
    worker_result = ExecutionResult(
        plan_id="",
        device_name=first.endpoint_key,
        task_name="(crashed)",
        execution_status="EXEC_ERROR",
        execution_failure_reason="Worker exception: browser crashed",
    )

    scheduler._on_bmc_group_done(worker_result, first.endpoint_key, [first, second])

    assert len(scheduler._results) == 2
    assert {result.plan_id for result in scheduler._results} == {"p-bmc-1", "p-bmc-2"}
    assert all(result.execution_status == "EXEC_ERROR" for result in scheduler._results)
    assert all(result.task_id.startswith("task-p-bmc-") for result in scheduler._results)
    assert all(result.client_task_id.startswith("client-p-bmc-") for result in scheduler._results)
    assert all(result.device_name == "BMC-1" for result in scheduler._results)


def test_dynamic_scheduler_fills_bmc_group_result_identity(tmp_path):
    scheduler = DynamicScheduler(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    plan = _bmc_plan("p-bmc-login", "BMC-2", "login-task")
    result = ExecutionResult(
        plan_id=plan.plan_id,
        device_name=plan.device.device_name,
        task_name=plan.task.task_name,
        execution_status="EXEC_FAILED",
        execution_failure_reason="BMC login failed",
    )

    scheduler._on_bmc_plan_in_group(plan, result)

    stored = scheduler._results[0]
    assert stored.task_id == "task-p-bmc-login"
    assert stored.client_task_id == "client-p-bmc-login"
    assert stored.task_type == "BMC"
    assert stored.endpoint_key == plan.endpoint_key


def test_app_retries_rule_failed_success_status(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    plan = _plan("p-rule", "D1", "rule-task")
    app._remember_last_plans([plan])
    original = _result(plan, "EXEC_SUCCESS")
    original.rule_status = "RULE_FAILED"
    captured: list[TaskPlan] = []

    def fake_run_with_plans(plans, mode="sequential"):
        captured.extend(plans)
        return [_result(plans[0], "EXEC_SUCCESS")]

    monkeypatch.setattr(app, "run_with_plans", fake_run_with_plans)

    app.retry_failed_tasks([original], mode="sequential")

    assert [plan.device.device_name for plan in captured] == ["D1"]


def test_app_restores_original_plan_lookup_after_retry(monkeypatch, tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    first = _plan("p-first", "D1", "first-task")
    second = _plan("p-second", "D2", "second-task")
    app._remember_last_plans([first, second])
    original = [
        _result(first, "EXEC_FAILED"),
        _result(second, "EXEC_FAILED"),
    ]

    def fake_run_with_plans(plans, mode="sequential"):
        app._remember_last_plans([plans[0]])
        return [_result(plans[0], "EXEC_SUCCESS")]

    monkeypatch.setattr(app, "run_with_plans", fake_run_with_plans)

    app.retry_failed_tasks(original, mode="sequential")

    assert [plan.plan_id for plan in app.failed_retry_candidates(original)] == [
        "p-first",
        "p-second",
    ]


def test_prompt_reports_non_retryable_failures_without_input():
    result = ExecutionResult(
        "p1",
        "D1",
        task_name="T1",
        execution_status="EXEC_SKIPPED_PORT_BLOCKED",
    )
    outputs: list[str] = []

    class FakeApp:
        def failed_retry_candidates(self, results):
            return []

        def retry_failed_tasks(self, results, mode):
            raise AssertionError("non-retryable failures must not retry")

    returned = prompt_retry_failed_tasks(
        FakeApp(),
        [result],
        mode="sequential",
        input_func=lambda prompt: (_ for _ in ()).throw(AssertionError("input called")),
        output_func=outputs.append,
        stdin=_Tty(True),
    )

    assert returned == [result]
    assert any("不可重试: 1" in line for line in outputs)
    assert any("存在失败但无可重试任务" in line for line in outputs)


def test_app_writes_retry_merged_reports(tmp_path):
    app = App(AppConfig(output_root=str(tmp_path), preflight_enabled=False))
    result = ExecutionResult("p1", "D1", task_name="T1", execution_status="EXEC_SUCCESS")

    report_dir = app.write_retry_merged_reports([result], output_dir=tmp_path)

    assert (tmp_path / "retry_merged" / "final_result.csv").exists()
    assert (tmp_path / "retry_merged" / "failure_detail.csv").exists()
    assert report_dir == str(tmp_path / "retry_merged")
