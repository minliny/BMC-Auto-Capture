from __future__ import annotations

import csv
from pathlib import Path

from src.checks import CheckResult, CheckStage, check_result_from_execution_status
from src.executor.bmc_executor import BMCExecutor
from src.executor.bmc_health_check import HealthResult
from src.executor.ssh_executor import SSHExecutor
from src.executor_api_server.schemas import PlanItemsResponse
from src.loader.schema_validator import (
    ValidationMessage,
    ValidationReport as LoaderValidationReport,
)
from src.models.execution_result import ExecutionResult
from src.models.verdict import compute_verdict
from src.out.collector import compute_summary, write_final_result_csv
from src.out.summary import write_failure_csv
from src.plan_catalog.models import (
    ValidationError,
    ValidationReport as PlanCatalogValidationReport,
)
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


def test_failure_detail_csv_flattens_structured_result_rule_details(tmp_path):
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
        message="port status rule failed",
        details={
            "failures": [{
                "details": {
                    "interface": "100GE1/0/1",
                    "field": "protocol",
                    "value": "down",
                    "raw_line": "100GE1/0/1                  up    down     uplink",
                },
            }],
        },
    ))

    path = Path(write_failure_csv([result], str(tmp_path)))
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    detail = rows[0]["检查失败明细"]

    assert "interface=100GE1/0/1" in detail
    assert "field=protocol" in detail
    assert "value='down'" in detail
    assert "raw_line='100GE1/0/1" in detail


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


def test_bmc_health_failure_records_session_check_once():
    result = ExecutionResult("plan-1", "node-1", task_id="task.010")
    health = HealthResult("before_screenshot")
    health.healthy = False
    health.status = "BMC_SESSION_EXPIRED"
    health.details = "session expired"

    BMCExecutor._record_health_check(result, health)
    BMCExecutor._mark_health_failure(result, health)

    assert result.execution_status == "EXEC_FAILED"
    assert compute_verdict(result) == "FAIL"
    assert len(result.check_results) == 1
    check = result.check_results[0]
    assert check.stage == "SESSION_CHECK"
    assert check.check_id == "bmc.health.before_screenshot"
    assert check.status == "FAIL"


def test_artifact_status_records_unified_artifact_check():
    result = ExecutionResult("plan-1", "node-1", task_id="task.010")
    result.artifact_status = "ARTIFACT_PARTIAL"
    result.artifact_failure_reason = "html: timeout"
    result.screenshots = ("/tmp/evidence.png",)

    BMCExecutor._record_artifact_check(result)

    assert compute_verdict(result) == "WARN"
    check = result.check_results[0]
    assert check.stage == "ARTIFACT_CHECK"
    assert check.status == "WARN"
    assert check.evidence_ref == "/tmp/evidence.png"


def test_ssh_artifact_failure_records_unified_artifact_check():
    result = ExecutionResult("plan-1", "node-1", task_id="task.ssh")
    result.artifact_status = "ARTIFACT_FAILED"
    result.artifact_failure_reason = "SSH execution failed"

    SSHExecutor._record_artifact_check(result)

    assert compute_verdict(result) == "FAIL"
    check = result.check_results[0]
    assert check.stage == "ARTIFACT_CHECK"
    assert check.check_id == "ssh.artifact.artifact_failed"
    assert check.status == "FAIL"


def test_precheck_skip_check_result_does_not_override_skipped_verdict():
    result = ExecutionResult(
        "plan-1",
        "node-1",
        task_id="task.ssh",
        execution_status="EXEC_SKIPPED_PRECHECK_FAILED",
        execution_failure_reason="SSH预检失败",
    )
    result.add_check_result(check_result_from_execution_status(
        result.execution_status,
        result.execution_failure_reason,
        stage=CheckStage.PRECHECK,
        check_id="connectivity.preflight",
    ))

    assert compute_verdict(result) == "SKIPPED"
    assert result.check_results[0].stage == "PRECHECK"
    assert result.check_results[0].status == "SKIP"


def test_rule_failed_execution_status_check_result_is_fail():
    check = check_result_from_execution_status(
        "EXEC_SUCCESS_RULE_FAILED",
        "规则检查失败: interface=100GE1/0/1 field=protocol value='down'",
    )

    assert check.status == "FAIL"
    assert check.details["execution_status"] == "EXEC_SUCCESS_RULE_FAILED"


def test_loader_validation_report_exports_config_check_results():
    report = LoaderValidationReport(messages=[
        ValidationMessage("ERROR", "task", 7, "任务ID", "任务ID重复: task.001"),
        ValidationMessage("WARNING", "device", 3, "带外管理IP", "启用设备的BMC IP为空"),
    ])

    checks = report.check_results()

    assert len(checks) == 2
    assert checks[0].stage == "CONFIG_CHECK"
    assert checks[0].check_id == "loader.validation.task"
    assert checks[0].status == "FAIL"
    assert checks[0].details["row"] == 7
    assert checks[0].details["field"] == "任务ID"
    assert checks[1].status == "WARN"


def test_plan_catalog_validation_report_exports_config_check_results_in_to_dict():
    report = PlanCatalogValidationReport(
        errors=[ValidationError("MISSING_SHEET", "Required sheet missing", "validation.json", "error")],
        warnings=[ValidationError("MISSING_OOB_IP", "Device missing oob_ip", "设备信息:row=2", "warning")],
    )

    data = report.to_dict()
    checks = data["check_results"]

    assert len(checks) == 2
    assert checks[0]["stage"] == "CONFIG_CHECK"
    assert checks[0]["check_id"] == "plan_catalog.validation.MISSING_SHEET"
    assert checks[0]["status"] == "FAIL"
    assert checks[0]["details"]["row_ref"] == "validation.json"
    assert checks[1]["status"] == "WARN"


def test_plan_items_response_schema_preserves_unified_check_results():
    response = PlanItemsResponse(
        planId="plan-1",
        status="COMPLETED",
        items=[{
            "taskId": "task.010",
            "planItemId": "plan-1:node-1:task.010",
            "deviceName": "node-1",
            "taskName": "BMC 首页",
            "status": "FAILED",
            "executionStatus": "EXEC_SUCCESS",
            "finalVerdict": "FAIL",
            "checkResults": [{
                "stage": "RESULT_CHECK",
                "checkId": "ssh.result_rules",
                "status": "FAIL",
                "severity": "ERROR",
                "message": "field=protocol value=down",
                "details": {"field": "protocol"},
            }],
        }],
    )

    data = response.model_dump() if hasattr(response, "model_dump") else response.dict()
    item = data["items"][0]

    assert item["taskId"] == "task.010"
    assert item["planItemId"] == "plan-1:node-1:task.010"
    assert item["checkResults"][0]["checkId"] == "ssh.result_rules"
    assert item["checkResults"][0]["details"]["field"] == "protocol"
