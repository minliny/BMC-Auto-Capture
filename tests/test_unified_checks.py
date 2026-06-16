from __future__ import annotations

import csv
from pathlib import Path

from src.checks import CheckResult, CheckStage
from src.models.execution_result import ExecutionResult
from src.models.verdict import compute_verdict
from src.out.collector import compute_summary, write_final_result_csv
from src.out.summary import write_failure_csv
from src.plan_run_service.models import PlanRunItem
from src.plan_run_service.query_projector import PlanRunQueryProjector


def test_result_check_failure_drives_verdict_and_failure_detail(tmp_path):
    result = ExecutionResult(
        plan_id="plan-1",
        device_name="node-1",
        task_id="task.019",
        execution_status="EXEC_SUCCESS",
    )
    result.add_check_result(CheckResult(
        stage=CheckStage.RESULT,
        check_id="ssh.result_rules",
        status="FAIL",
        message="interface=GE1/0/1 field=protocol value='down'",
    ))

    assert compute_verdict(result) == "FAIL"

    path = Path(write_failure_csv([result], str(tmp_path)))
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))

    assert len(rows) == 1
    assert rows[0]["检查失败明细"].startswith("RESULT_CHECK/ssh.result_rules/FAIL")
    assert "field=protocol" in rows[0]["失败原因"]


def test_post_audit_warning_is_reported_but_not_final_verdict_failure(tmp_path):
    result = ExecutionResult(
        plan_id="plan-1",
        device_name="node-1",
        task_id="task.010",
        execution_status="EXEC_SUCCESS",
    )
    result.add_check_result(CheckResult(
        stage=CheckStage.POST_AUDIT,
        check_id="evidence_audit.HTML_MISSING",
        status="WARN",
        severity="WARNING",
        message="HTML missing in audit report",
    ))

    assert compute_verdict(result) == "PASS"
    summary = compute_summary([result])
    assert summary["check_warn"] == 1
    assert summary["check_post_audit_warn"] == 1

    csv_path = Path(write_final_result_csv([result], str(tmp_path)))
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "检查结果汇总" in text
    assert "POST_AUDIT:WARN" in text


def test_query_projector_exposes_redacted_check_results():
    execution_result = ExecutionResult("plan-1", "node-1", task_id="task.010")
    execution_result.add_check_result(CheckResult(
        stage=CheckStage.RESULT,
        check_id="bmc.rule.advanced.assert_text",
        status="FAIL",
        message="password=plain-secret",
        details={"selector": "#status", "token": "plain-secret"},
    ))
    item = PlanRunItem(
        plan_id="plan-1",
        device_name="node-1",
        task_name="BMC 首页",
        task_id="task.010",
        plan_item_id="plan-1:node-1:task.010",
        status="FAILED",
    )
    item._execution_result = execution_result

    data = PlanRunQueryProjector().item(item)
    check = data["checkResults"][0]

    assert check["stage"] == "RESULT_CHECK"
    assert check["checkId"] == "bmc.rule.advanced.assert_text"
    assert "plain-secret" not in check["message"]
    assert "plain-secret" not in check["details"]["token"]
