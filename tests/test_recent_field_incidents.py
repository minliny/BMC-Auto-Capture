from __future__ import annotations

import threading
import time
from pathlib import Path

from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.plan_item_status_callback_client import FakeCallbackTransport, validate_callback_url
from src.plan_run_service.service import PlanRunService
from src.scheduler import dynamic_scheduler
from src.scheduler.dynamic_scheduler import DynamicScheduler


EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


class RecordingEventBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs):
        self.events.append((event, kwargs))


def test_field_callback_intranet_urls_are_not_blocked():
    """Recent field issue A: intranet callback URLs must not be rejected."""
    for url in (
        "http://10.0.0.1/api/plans/items/status",
        "http://127.0.0.1:18080/api/plans/items/status",
        "http://192.168.1.10/api/plans/items/status",
        "http://172.16.0.5:6003/api/plans/items/status",
    ):
        ok, reason = validate_callback_url(url)
        assert ok is True, f"{url} rejected as {reason}"
        assert reason == ""


def test_field_callback_contract_does_not_document_private_ip_blocking():
    """Recent field issue A: public contract must not advertise private-IP blocking."""
    from src.executor_api_server.contracts import PLAN_ITEM_STATUS_CALLBACK_CONTRACT

    policy = PLAN_ITEM_STATUS_CALLBACK_CONTRACT["transportPreconditions"]["urlPolicy"]
    assert "CALLBACK_PRIVATE_IP_FORBIDDEN" not in policy
    assert "EXECUTOR_CALLBACK_ALLOWED_HOSTS" not in policy
    assert "Private/link-local literal IPs require" not in policy


def test_field_runtime_bundle_collects_playwright_submodules():
    """Recent field issue C: packaged runtime must include playwright.async_api."""
    project_root = Path(__file__).resolve().parent.parent
    spec_text = (project_root / "scripts" / "build.spec").read_text(encoding="utf-8")
    workflow_text = (project_root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert '"playwright"' in spec_text
    assert "--collect-submodules playwright" in workflow_text


def test_field_bmc_dependency_missing_blocks_bmc_but_not_ssh(monkeypatch, tmp_path):
    """Recent field issue C/E: missing Playwright must not become global dispatch stop."""
    monkeypatch.setattr(
        dynamic_scheduler,
        "check_playwright_runtime_dependency",
        lambda: (
            False,
            "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME: "
            "No module named 'playwright.async_api'",
        ),
    )

    class FakeSSHExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, plan, output_root):
            now = time.time()
            return ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                device_group=plan.device.device_group,
                bmc_ip=plan.device.bmc_ip,
                inband_ip=plan.device.inband_ip,
                task_name=plan.task.task_name,
                task_type=plan.task.task_type,
                execution_mode=plan.task.execution_mode,
                execution_status="EXEC_SUCCESS",
                started_at=now,
                ended_at=now,
                duration_seconds=0.001,
                endpoint_key=plan.endpoint_key,
                endpoint_type=plan.endpoint_type,
            )

    monkeypatch.setattr(dynamic_scheduler, "SSHExecutor", FakeSSHExecutor)

    plans = [
        TaskPlan(
            device=Device(1, "redacted-bmc", "A3", "10.0.0.1", "", ""),
            task=Task(1, 1, "BMC dependency gate", "BMC", "BMC_URL"),
            plan_id="field-bmc-dep",
        ),
        TaskPlan(
            device=Device(2, "redacted-ssh", "L2", "", "", "", inband_ip="192.0.2.20"),
            task=Task(2, 2, "SSH still dispatches", "SSH", "SSH_CMD", command_or_url="display version"),
            plan_id="field-ssh-ok",
        ),
    ]

    cfg = AppConfig(output_root=str(tmp_path), base_bmc_workers=1, max_bmc_workers=1,
                    base_ssh_workers=1, max_ssh_workers=1, resource_check_interval=0.01)
    bus = RecordingEventBus()
    scheduler = DynamicScheduler(cfg, event_bus=bus)

    results = scheduler.run(plans)

    by_task = {r.task_name: r for r in results}
    assert by_task["BMC dependency gate"].execution_status == "EXEC_BLOCKED"
    assert "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME" in by_task["BMC dependency gate"].execution_failure_reason
    assert by_task["SSH still dispatches"].execution_status == "EXEC_SUCCESS"
    assert scheduler.stop_metadata["stopReason"] == ""
    assert scheduler.stop_metadata["affectedPendingCount"] == 0
    completed = [event for event, _payload in bus.events if event == "plan_completed"]
    assert len(completed) == 2


def test_field_run_by_plan_id_waits_for_background_run_instead_of_reexecuting(monkeypatch, tmp_path):
    """Recent field issue J: synchronous test helper must not double-run a live plan."""
    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def fake_execute_run(self, run, cb):
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(timeout=2)
        run.status = "COMPLETED"
        run.finished_at = time.time()

    monkeypatch.setattr(PlanRunService, "_execute_run", fake_execute_run)

    svc = PlanRunService(workspace_root=str(tmp_path), callback_transport=FakeCallbackTransport())
    svc.set_latest_excel(EXCEL_FILE)
    accepted = svc.start_plan_run(
        1,
        {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}},
    )

    assert accepted["accepted"] is True
    assert started.wait(timeout=1)

    waiter = threading.Thread(target=lambda: svc.run_by_plan_id(accepted["planId"]))
    waiter.start()
    time.sleep(0.1)
    assert call_count == 1

    release.set()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert call_count == 1


def test_field_ssh_screenshot_code_has_no_tool_truncation_notice():
    """Recent field issue K: SSH screenshot source must not contain tool-authored notices."""
    forbidden = (
        "只显示",
        "完整请看",
        "输出已截断",
        "请查看日志",
        "[TRUNCATED:",
        "full transcript saved",
        "showing first",
    )
    project_root = Path(__file__).resolve().parent.parent
    for rel in ("src/executor/ssh_executor.py", "src/out/screenshot.py"):
        text = (project_root / rel).read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), rel
