from __future__ import annotations

import csv
import json
from pathlib import Path

from src.app import App
from src.executor.ssh_executor import SSHExecutionOptions, SSHExecutor
from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.out.collector import write_final_result_csv
from src.out.evidence_audit import audit_plan_evidence
from src.out.summary import write_failure_csv
from src.out.timing import write_execution_summary


class _VRPChannel:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.queue = [b"<SW>"]
        self.sent: list[str] = []

    def settimeout(self, value):
        self.timeout = value

    def get_pty(self, **kwargs):
        return None

    def invoke_shell(self):
        return None

    def recv_ready(self):
        return bool(self.queue)

    def recv(self, size):
        if not self.queue:
            return b""
        return self.queue.pop(0)

    def send(self, data):
        cmd = data.strip()
        self.sent.append(cmd)
        response = self.responses.get(cmd, f"<SW>{cmd}\n<SW>")
        self.queue.append(response.encode("utf-8"))
        return len(data)

    def close(self):
        return None


class _VRPTransport:
    def __init__(self, channel: _VRPChannel):
        self.channel = channel

    def is_active(self):
        return True

    def open_session(self):
        return self.channel


class _VRPClient:
    def __init__(self, responses: dict[str, str]):
        self.channel = _VRPChannel(responses)

    def get_transport(self):
        return _VRPTransport(self.channel)


def _ssh_context_task(display_response: str):
    responses = {
        "screen-length 0 temporary": "<SW>screen-length 0 temporary\n<SW>",
        "system-view": "<SW>system-view\nEnter system view, return user view with Ctrl+Z.\n[SW]",
        "interface MEth0/0/0": "[SW]interface MEth0/0/0\n[SW-MEth0/0/0]",
        "display this": display_response,
    }
    task = Task(
        1, 1, "L2 management port", "SSH", "SSH_CMD",
        command_or_url="\n".join([
            "screen-length 0 temporary",
            "system-view",
            "interface MEth0/0/0",
            "display this",
        ]),
    )
    spec = SSHExecutor()._parse_command_spec(task, task.command_or_url)
    device = Device(1, "redacted-device", "L2", "", "", "", inband_ip="192.0.2.20")
    return responses, spec, device


def test_ssh_multi_command_context_echo_does_not_fail_when_evidence_has_output():
    display_response = (
        "[SW-MEth0/0/0]display this\n"
        "#\ninterface MEth0/0/0\n description mgmt\n#\n"
        "[SW-MEth0/0/0]"
    )
    responses, spec, device = _ssh_context_task(display_response)
    executor = SSHExecutor(command_timeout=1, idle_timeout=0.01)

    outputs, has_failure, has_timeout, reasons, cmd_outputs, steps = executor._execute_interactive_shell(
        _VRPClient(responses), device, spec["commands"], spec,
        SSHExecutionOptions(command_timeout=1, idle_timeout=0.01, retry_count=0),
    )

    assert has_failure is False
    assert has_timeout is False
    assert reasons == []
    assert executor._evidence_output_failure(spec["commands"], cmd_outputs, spec, "interactive_shell") == ""
    assert any("interface MEth0/0/0" in output for output in outputs)
    assert all(step.status == "SUCCESS" for step in steps)


def test_ssh_multi_command_evidence_echo_failure_points_to_display_command():
    display_response = "[SW-MEth0/0/0]display this\n[SW-MEth0/0/0]"
    responses, spec, device = _ssh_context_task(display_response)
    executor = SSHExecutor(command_timeout=1, idle_timeout=0.01)

    _, has_failure, _, reasons, cmd_outputs, _ = executor._execute_interactive_shell(
        _VRPClient(responses), device, spec["commands"], spec,
        SSHExecutionOptions(command_timeout=1, idle_timeout=0.01, retry_count=0),
    )

    assert has_failure is True
    assert any("display this" in reason for reason in reasons)
    assert all("interface MEth0/0/0" not in reason for reason in reasons)
    evidence_failure = executor._evidence_output_failure(
        spec["commands"], cmd_outputs, spec, "interactive_shell",
    )
    assert "display this" in evidence_failure


def test_bmc_failure_reason_mapping_reaches_final_and_failure_csv(tmp_path):
    results = [
        ExecutionResult(
            "p1", "redacted-device", task_name="bmc session", task_type="BMC",
            execution_status="EXEC_FAILED",
            execution_failure_reason="BMC_SESSION_EXPIRED: session expired",
        ),
        ExecutionResult(
            "p2", "redacted-device", task_name="bmc dom", task_type="BMC",
            execution_status="EXEC_FAILED",
            execution_failure_reason="BMC_PAGE_HEALTH_FAILED [BMC_EMPTY_DOM]: HTML length 0",
        ),
        ExecutionResult(
            "p3", "redacted-device", task_name="bmc goto", task_type="BMC",
            execution_status="EXEC_TIMEOUT",
            execution_failure_reason="Page.goto Timeout 5000ms exceeded",
        ),
    ]

    final_path = Path(write_final_result_csv(results, str(tmp_path)))
    failure_path = Path(write_failure_csv(results, str(tmp_path)))

    final_text = final_path.read_text(encoding="utf-8-sig")
    assert "BMC_SESSION_EXPIRED" in final_text
    assert "BMC_EMPTY_DOM" in final_text
    assert "BMC_PAGE_GOTO_TIMEOUT" in final_text

    with failure_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    categories = {row["任务名称"]: row["失败分类"] for row in rows}
    assert categories["bmc session"] == "BMC_SESSION_EXPIRED"
    assert categories["bmc dom"] == "BMC_EMPTY_DOM"
    assert categories["bmc goto"] == "BMC_PAGE_GOTO_TIMEOUT"


def test_execution_summary_writes_route_guard_stop_reason_and_bmc_expiry_does_not_stop(tmp_path):
    result = ExecutionResult(
        "p-route", "redacted-device", task_name="ssh", task_type="SSH",
        execution_status="EXEC_SKIPPED_ROUTE_CHANGED",
        execution_failure_reason="调度停止: ROUTE_GUARD_STOPPED",
    )
    path = Path(write_execution_summary(
        [result], str(tmp_path),
        stop_metadata={
            "stopReason": "ROUTE_GUARD_STOPPED",
            "stopTriggeredBy": "RouteGuard",
            "stoppedAt": 1.0,
            "affectedPendingCount": 1,
        },
    ))
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["stopReason"] == "ROUTE_GUARD_STOPPED"
    assert summary["stopTriggeredBy"] == "RouteGuard"
    assert summary["stoppedAt"]
    assert summary["affectedPendingCount"] == 1

    no_stop_path = Path(write_execution_summary([
        ExecutionResult(
            "p-bmc", "redacted-device", task_name="bmc", task_type="BMC",
            execution_status="EXEC_FAILED",
            execution_failure_reason="BMC_SESSION_EXPIRED: isolated device session",
        )
    ], str(tmp_path / "nostop")))
    no_stop = json.loads(no_stop_path.read_text(encoding="utf-8"))
    assert no_stop["stopReason"] == ""
    assert no_stop["affectedPendingCount"] == 0

    cfg = AppConfig()
    cfg.route_guard_stop_threshold = 100
    app = App(cfg)
    app._on_route_change(["one-device-observed-change"])
    assert app._stop_event.is_set() is False


def test_evidence_audit_explains_missing_txt_and_html_sources(tmp_path):
    ssh_result = ExecutionResult(
        "p-ssh", "redacted-device", task_name="ssh", task_type="SSH",
        execution_status="EXEC_SKIPPED_STOPPED",
        execution_failure_reason="调度停止: USER_STOPPED",
    )
    ssh_result.output_dir = str(tmp_path / "ssh")
    ssh_audit = audit_plan_evidence(ssh_result)
    assert ssh_audit["evidence_status"] == "TXT_LOG_MISSING"
    assert "result_status=EXEC_SKIPPED_STOPPED" in ssh_audit["evidence_reason"]

    bmc_result = ExecutionResult(
        "p-bmc", "redacted-device", task_name="bmc", task_type="BMC",
        execution_status="EXEC_FAILED",
        execution_failure_reason="BMC_PAGE_GOTO_TIMEOUT target_type=business_page timeout_ms=20000",
    )
    bmc_result.output_dir = str(tmp_path / "bmc")
    bmc_audit = audit_plan_evidence(bmc_result)
    assert bmc_audit["evidence_status"] == "HTML_MISSING"
    assert "result_status=EXEC_FAILED" in bmc_audit["evidence_reason"]
