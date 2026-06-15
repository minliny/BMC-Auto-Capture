from __future__ import annotations

from src.plan_run_service.state_codec import PlanRunStateCodec
from src.plan_run_service.service import PlanRun, PlanRunItem


def test_state_codec_redacts_item_error_messages():
    run = PlanRun(
        plan_id="plan-1",
        run_id="run-1",
        excel_hash="hash-1",
        status="COMPLETED",
        runner_mode="real",
        output_root="/tmp/out",
        items=[
            PlanRunItem(
                plan_id="plan-1",
                device_name="device-1",
                task_name="task-1",
                status="FAILED",
                error_message="token=secret-value",
            )
        ],
    )

    state = PlanRunStateCodec().run_to_state(run)

    error_message = state["items"][0]["errorMessage"]
    assert "secret-value" not in error_message
    assert "REDACTED" in error_message


def test_state_codec_restores_interrupted_running_state():
    state = {
        "planId": "plan-1",
        "runId": "run-1",
        "excelHash": "hash-1",
        "status": "RUNNING",
        "items": [
            {
                "deviceName": "device-1",
                "taskName": "task-1",
                "status": "IN_PROGRESS",
                "startedAt": 1.0,
                "infoEvents": [{"level": "INFO", "message": "started"}],
            },
            "bad-item-is-ignored",
        ],
    }

    run = PlanRunStateCodec().state_to_run(state)

    assert run is not None
    assert run.status == "INTERRUPTED"
    assert len(run.items) == 1
    assert run.items[0].status == "FAILED"
    assert run.items[0].info_events == [{"level": "INFO", "message": "started"}]


def test_state_codec_rejects_missing_identity():
    assert PlanRunStateCodec().state_to_run({"planId": "plan-1"}) is None
    assert PlanRunStateCodec().state_to_run({"runId": "run-1"}) is None
