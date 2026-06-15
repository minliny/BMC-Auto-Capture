from __future__ import annotations

from src.plan_run_service.query_projector import PlanRunQueryProjector, format_timestamp
from src.plan_run_service.service import PlanRun, PlanRunItem


def test_query_projector_formats_plan_without_items():
    run = PlanRun(
        plan_id="plan-1",
        run_id="run-1",
        excel_hash="hash-1",
        status="COMPLETED",
        output_root="/tmp/out",
        started_at=1.0,
        finished_at=2.0,
        items=[],
    )

    data = PlanRunQueryProjector().plan(run)

    assert data["planId"] == "plan-1"
    assert data["status"] == "COMPLETED"
    assert data["excelHash"] == "hash-1"
    assert data["outputRoot"] == "/tmp/out"
    assert data["startedAt"] == format_timestamp(1.0)
    assert data["finishedAt"] == format_timestamp(2.0)
    assert "items" not in data
    assert [ev["level"] for ev in data["infoEvents"]] == ["INFO", "INFO"]


def test_query_projector_includes_items_and_redacts_errors():
    run = PlanRun(
        plan_id="plan-1",
        status="COMPLETED",
        started_at=1.0,
        items=[
            PlanRunItem(
                plan_id="plan-1",
                device_group="A3",
                device_name="D1",
                task_name="T1",
                status="FAILED",
                error_message="password=plain-secret",
                started_at=3.0,
                finished_at=4.0,
                info_events=[{"level": "ERROR", "message": "failed"}],
            )
        ],
    )

    data = PlanRunQueryProjector().plan(run, include_items=True)

    item = data["items"][0]
    assert item["deviceGroup"] == "A3"
    assert item["deviceName"] == "D1"
    assert item["taskName"] == "T1"
    assert item["status"] == "FAILED"
    assert "plain-secret" not in item["errorMessage"]
    assert item["startedAt"] == format_timestamp(3.0)
    assert item["finishedAt"] == format_timestamp(4.0)
    assert item["infoEvents"] == [{"level": "ERROR", "message": "failed"}]
