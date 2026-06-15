from __future__ import annotations

import json
from pathlib import Path

from src.models.execution_result import ExecutionResult
from src.out.result_writer import ResultWriter


def test_result_writer_writes_core_reports_and_stop_metadata(tmp_path, capsys):
    result = ExecutionResult(
        "p1",
        "redacted-device",
        task_name="bmc",
        task_type="BMC",
        execution_status="EXEC_SKIPPED_ROUTE_CHANGED",
        execution_failure_reason="调度停止: ROUTE_GUARD_STOPPED",
    )

    summary = ResultWriter().write(
        [result],
        str(tmp_path),
        execution_started_at=1.0,
        stop_metadata={
            "stopReason": "ROUTE_GUARD_STOPPED",
            "stopTriggeredBy": "RouteGuard",
            "stoppedAt": 2.0,
            "affectedPendingCount": 1,
        },
    )

    assert summary["total"] == 1
    for name in (
        "result.csv",
        "final_result.csv",
        "summary_pivot.csv",
        "failure_detail.csv",
        "plan_timing.csv",
        "device_timing.csv",
        "endpoint_timing.csv",
        "execution_summary.json",
        "execution_summary.csv",
        "evidence_audit.csv",
    ):
        assert (tmp_path / name).exists(), name

    execution_summary = json.loads((tmp_path / "execution_summary.json").read_text(encoding="utf-8"))
    assert execution_summary["stopReason"] == "ROUTE_GUARD_STOPPED"
    assert execution_summary["stopTriggeredBy"] == "RouteGuard"
    assert execution_summary["affectedPendingCount"] == 1
    assert "执行完成" in capsys.readouterr().out


def test_result_writer_keeps_primary_csv_when_optional_report_fails(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("pivot failed")

    monkeypatch.setattr("src.out.result_writer.build_pivot_csv", _boom)
    ResultWriter().write([
        ExecutionResult("p1", "redacted-device", task_name="ssh", execution_status="EXEC_SUCCESS")
    ], str(tmp_path))

    assert Path(tmp_path / "result.csv").exists()
    assert Path(tmp_path / "final_result.csv").exists()
