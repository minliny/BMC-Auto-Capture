from __future__ import annotations

from src.plan_run_service import PlanRunService
from src.plan_run_service.models import PlanRun, PlanRunItem
from src.plan_run_service.query_projector import PlanRunQueryProjector
from src.plan_run_service.state_codec import PlanRunStateCodec
from src.plan_item_status_callback_client import FakeCallbackTransport, PlanItemStatusCallbackClient


def _item(status: str, error: str = "") -> PlanRunItem:
    return PlanRunItem(
        plan_id="plan-1",
        device_name="D1",
        task_name="T1",
        status=status,
        error_message=error or None,
    )


def test_plan_run_status_failed_when_all_items_timeout():
    svc = PlanRunService()
    run = PlanRun(
        plan_id="plan-1",
        items=[
            _item("FAILED", "TIMEOUT: SSH连接超时"),
            _item("FAILED", "TIMEOUT: BMC连接超时"),
        ],
    )

    svc._finalize_run_status(run)

    assert run.status == "FAILED"
    assert "ALL_ITEMS_FAILED" in run.error_message
    assert "TIMEOUT" in run.error_message


def test_plan_run_status_completed_when_any_item_succeeds():
    svc = PlanRunService()
    run = PlanRun(
        plan_id="plan-1",
        items=[
            _item("SUCCESS"),
            _item("FAILED", "TIMEOUT: SSH连接超时"),
        ],
    )

    svc._finalize_run_status(run)

    assert run.status == "COMPLETED"
    assert run.error_message == ""


def test_plan_run_status_failed_when_no_items():
    svc = PlanRunService()
    run = PlanRun(plan_id="plan-1", items=[])

    svc._finalize_run_status(run)

    assert run.status == "FAILED"
    assert run.error_message.startswith("NO_RUN_ITEMS")


def test_failed_plan_query_exposes_error_message():
    run = PlanRun(
        plan_id="plan-1",
        status="FAILED",
        error_message="ALL_ITEMS_FAILED: token=secret-value",
        items=[_item("FAILED", "TIMEOUT")],
    )

    data = PlanRunQueryProjector().plan(run)

    assert data["status"] == "FAILED"
    assert "ALL_ITEMS_FAILED" in data["errorMessage"]
    assert "secret-value" not in data["errorMessage"]


def test_state_codec_preserves_failed_run_error_message():
    run = PlanRun(
        plan_id="plan-1",
        run_id="run-1",
        status="FAILED",
        error_message="ALL_ITEMS_FAILED: timeout",
        items=[_item("FAILED", "TIMEOUT")],
    )

    state = PlanRunStateCodec().run_to_state(run)
    restored = PlanRunStateCodec().state_to_run(state)

    assert restored is not None
    assert restored.status == "FAILED"
    assert restored.error_message == "ALL_ITEMS_FAILED: timeout"


def test_plan_run_query_failed_when_real_runner_all_items_timeout(monkeypatch):
    def fake_run_job(self_ignored, job_payload):
        from src.job_runner_adapter import JobResult

        return JobResult(
            status="TIMEOUT",
            error={"message": "network timeout", "code": "TIMEOUT"},
            duration_ms=100,
        )

    monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", fake_run_job)

    svc = PlanRunService(allow_real_runner=True)
    device = type("Device", (), {
        "enabled": True,
        "device_name": "D1",
        "device_group": "G1",
        "bmc_ip": "",
        "inband_ip": "",
    })()
    task = type("Task", (), {
        "enabled": True,
        "task_name": "T1",
        "task_type": "SSH",
        "execution_mode": "SSH_CMD",
        "match_group": "G1",
        "command_or_url": "display version",
        "timeout_seconds": 1,
    })()
    svc._validate_and_snapshot_latest = lambda: {
        "ok": True,
        "snapshot": type("Snapshot", (), {"excel_hash": "hash-1"})(),
        "devices": [device],
        "tasks": [task],
    }

    accepted = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1"}})
    svc.run_by_plan_id(accepted["planId"])

    data = svc.get_plan(accepted["planId"])
    assert data["status"] == "FAILED"
    assert data["summary"]["failed"] == data["summary"]["total"]
    assert "ALL_ITEMS_FAILED" in data["errorMessage"]
    assert "network timeout" in data["errorMessage"]


def test_run_by_plan_id_does_not_reexecute_failed_terminal_run(monkeypatch):
    calls = []

    def fake_run_job(self_ignored, job_payload):
        calls.append(job_payload)
        from src.job_runner_adapter import JobResult

        return JobResult(status="TIMEOUT", error={"message": "network timeout"}, duration_ms=100)

    monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", fake_run_job)

    svc = PlanRunService(allow_real_runner=True)
    device = type("Device", (), {
        "enabled": True,
        "device_name": "D1",
        "device_group": "G1",
        "bmc_ip": "",
        "inband_ip": "",
    })()
    task = type("Task", (), {
        "enabled": True,
        "task_name": "T1",
        "task_type": "SSH",
        "execution_mode": "SSH_CMD",
        "match_group": "G1",
        "command_or_url": "display version",
        "timeout_seconds": 1,
    })()
    svc._validate_and_snapshot_latest = lambda: {
        "ok": True,
        "snapshot": type("Snapshot", (), {"excel_hash": "hash-1"})(),
        "devices": [device],
        "tasks": [task],
    }

    accepted = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1"}})
    svc.run_by_plan_id(accepted["planId"])
    svc.run_by_plan_id(accepted["planId"])

    assert len(calls) == 1


def test_plan_run_outer_crash_marks_run_failed_and_query_visible(monkeypatch, tmp_path):
    svc = PlanRunService(workspace_root=str(tmp_path))
    run = PlanRun(
        plan_id="plan-1",
        run_id="run-1",
        status="RUNNING",
        items=[_item("PENDING")],
    )
    svc._runs[str(run.plan_id)] = run
    svc._runs_by_run_id[run.run_id] = run

    def crash(*args, **kwargs):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(svc, "_execute_run_item", crash)
    cb = PlanItemStatusCallbackClient(transport=FakeCallbackTransport())

    svc._execute_run(run, cb)

    data = svc.get_plan("plan-1")
    assert run.status == "FAILED"
    assert data["status"] == "FAILED"
    assert "RUN_EXECUTOR_CRASH" in data["errorMessage"]
    assert data["summary"]["failed"] == 1
    item = svc.get_plan_items("plan-1")["items"][0]
    assert item["status"] == "FAILED"
    assert "RUN_EXECUTOR_CRASH" in item["errorMessage"]
