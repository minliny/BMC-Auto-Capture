from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.executor.bmc_executor import BMCExecutor
from src.executor.ssh_executor import SSHExecutor, StreamEvent, resolve_task_command, resolve_task_no_split
from src.models.device import Device
from src.models.execution_result import ExecutionResult, StepResult
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.verdict import compute_verdict
from src.out.collector import compute_summary, write_final_result_csv
from src.scheduler.bmc_session_runner import BMCEndpointSessionRunner


class _Healthy:
    healthy = True
    status = "OK"
    details = ""


class _Page:
    def set_default_timeout(self, value):
        self.timeout = value

    async def close(self):
        return None


class _Context:
    async def new_page(self):
        return _Page()


class _BrowserManager:
    async def get_context(self):
        return _Context()


def _bmc_plan(retry_count=1):
    return TaskPlan(
        plan_id="bmc-plan",
        device=Device(1, "BMC1", "A3", "10.0.0.1", "u", "p"),
        task=Task(
            1, 1, "BMC retry", "BMC", "BMC_URL",
            command_or_url="/test", timeout_seconds=5,
            retry_count=retry_count,
        ),
    )


def _patch_session(monkeypatch, tmp_path, capture):
    async def login(self, page, device, url):
        return True, ""

    async def logout(self, page, device):
        return None

    async def health(page, stage, target_url=""):
        return _Healthy()

    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_login", login)
    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_logout", logout)
    monkeypatch.setattr(
        "src.scheduler.bmc_session_runner.check_bmc_page_health", health,
    )
    monkeypatch.setattr(BMCExecutor, "_run_capture_flow", capture)
    monkeypatch.setattr(
        BMCExecutor, "_build_output_dir",
        lambda self, root, device, task: str(tmp_path / "bmc-result"),
    )


def test_bmc_retry_first_failure_then_success_isolated(monkeypatch, tmp_path):
    calls = []

    async def capture(self, page, task, device, ip, output_dir, result):
        calls.append((output_dir, result))
        attempt = len(calls)
        result.step_results.append(StepResult(0, f"attempt-{attempt}", "FAILED" if attempt == 1 else "SUCCESS"))
        artifact = str(Path(output_dir) / f"attempt-{attempt}.png")
        result.screenshots = (artifact,)
        if attempt == 1:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = "temporary navigation failure"
        else:
            result.execution_status = "EXEC_SUCCESS"

    _patch_session(monkeypatch, tmp_path, capture)
    plan = _bmc_plan(retry_count=1)
    runner = BMCEndpointSessionRunner(
        _BrowserManager(), plan.endpoint_key, [plan], str(tmp_path),
    )
    results = asyncio.run(runner._run_async())

    assert len(results) == 1
    result = results[0]
    assert result.execution_status == "EXEC_SUCCESS"
    assert result.attempt_count == 2
    assert result.final_attempt_index == 2
    assert result.max_attempts == 2
    assert [step.step_name for step in result.step_results] == ["attempt-2"]
    assert calls[0][0].endswith("attempt_1")
    assert calls[1][0].endswith("attempt_2")
    assert calls[0][0] != calls[1][0]
    assert result.screenshots[0].startswith(calls[1][0])
    assert len(result.attempt_records) == 2
    assert result.attempt_records[0].output_dir != result.attempt_records[1].output_dir


def test_bmc_retry_all_fail_and_nonretryable(monkeypatch, tmp_path):
    async def always_timeout(self, page, task, device, ip, output_dir, result):
        result.execution_status = "EXEC_TIMEOUT"
        result.execution_failure_reason = "temporary timeout"

    _patch_session(monkeypatch, tmp_path, always_timeout)
    plan = _bmc_plan(retry_count=2)
    result = asyncio.run(BMCEndpointSessionRunner(
        _BrowserManager(), plan.endpoint_key, [plan], str(tmp_path),
    )._run_async())[0]
    assert result.execution_status == "EXEC_TIMEOUT"
    assert result.attempt_count == 3
    assert len({a.output_dir for a in result.attempt_records}) == 3

    calls = 0

    async def invalid_json(self, page, task, device, ip, output_dir, result):
        nonlocal calls
        calls += 1
        result.execution_status = "EXEC_FAILED"
        result.execution_failure_reason = "BMC_ACTIONS JSON 解析失败"

    _patch_session(monkeypatch, tmp_path, invalid_json)
    plan2 = _bmc_plan(retry_count=3)
    result2 = asyncio.run(BMCEndpointSessionRunner(
        _BrowserManager(), plan2.endpoint_key, [plan2], str(tmp_path),
    )._run_async())[0]
    assert calls == 1
    assert result2.attempt_count == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("EXEC_SUCCESS", "PASS"),
        ("EXEC_FAILED", "FAIL"),
        ("EXEC_ERROR", "FAIL"),
        ("EXEC_TIMEOUT", "FAIL"),
        ("EXEC_PARTIAL", "WARN"),
        ("EXEC_SKIPPED_STOPPED", "SKIPPED"),
        ("EXEC_SKIPPED_ROUTE_CHANGED", "SKIPPED"),
        ("EXEC_SKIPPED_SESSION_FAILURE", "SKIPPED"),
        ("EXEC_BLOCKED", "BLOCKED"),
        ("SOME_NEW_STATUS", "FAIL"),
        ("", "FAIL"),
        (None, "FAIL"),
    ],
)
def test_verdict_status_mapping(status, expected):
    result = ExecutionResult("p", "d", execution_status=status)
    assert compute_verdict(result) == expected
    if status in ("SOME_NEW_STATUS", "", None):
        assert result.unknown_status is True
        assert result.raw_execution_status == ("" if status is None else status)


def test_unknown_summary_and_csv_are_closed(tmp_path):
    result = ExecutionResult("p", "d", execution_status="SOME_NEW_STATUS")
    summary = compute_summary([result])
    assert summary["unknown_statuses"] == {"SOME_NEW_STATUS": 1}
    csv_path = write_final_result_csv([result], str(tmp_path))
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    assert result.final_verdict == "FAIL"
    assert "FAIL" in text
    assert "SOME_NEW_STATUS" in text


def test_stderr_allowlist_fail_patterns_and_nonzero():
    executor = SSHExecutor()
    allowed = {
        "stderr_allow_patterns": [r"^WARNING:"],
        "stderr_ignore_patterns": [],
        "stderr_fail_patterns": [r"fatal|permission denied"],
        "allow_exit_codes": [],
    }
    assert executor._stderr_failure_reason("WARNING: harmless", allowed, 0) == ""
    assert "fail pattern" in executor._stderr_failure_reason("fatal: bad", allowed, 0)
    assert "not allowlisted" in executor._stderr_failure_reason("unexpected error", allowed, 0)

    # stderr allowlisting never implicitly permits a non-zero exit code.
    assert executor._exit_code_is_failure(1, allowed) is True
    allowed["allow_exit_codes"] = [1]
    assert executor._exit_code_is_failure(1, allowed) is False


def test_stream_events_preserve_interleaved_order():
    events = [
        StreamEvent("stdout", 1.0, b"out-1\n"),
        StreamEvent("stderr", 2.0, b"err-1\n"),
        StreamEvent("stdout", 3.0, b"out-2\n"),
    ]
    assert SSHExecutor._stream_events_to_text(events) == "out-1\nerr-1\nout-2\n"


class _TerminalChannel:
    def __init__(self):
        self.queue = [b"[root@a3 ~]# "]

    def settimeout(self, value):
        pass

    def recv_ready(self):
        return bool(self.queue)

    def recv(self, size):
        return self.queue.pop(0)

    def send(self, text):
        lines = "".join(f"==============> {i}\nvalue-{i}\n" for i in range(80))
        self.queue.append((text + lines + "[root@a3 ~]# ").encode())

    def close(self):
        pass


class _SSHClient:
    def __init__(self):
        self.channel = _TerminalChannel()

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        pass

    def invoke_shell(self, **kwargs):
        return self.channel

    def close(self):
        pass


def test_a3_terminal_strategy_full_txt_and_truncated_png(monkeypatch, tmp_path):
    command = 'for i in $(seq 0 15); do echo "==============> $i"; hccn_tool -i $i -optical -g; done'
    task = Task(
        1, 1, "计算节点光模块信息查询测试", "SSH", "SSH_CMD",
        command_or_url="display interface transceiver",
        timeout_seconds=1,
        image_name_template="a3",
    )
    object.__setattr__(task, "_per_group_commands", {"A3": command})
    object.__setattr__(task, "_per_group_no_split", {"A3": True})
    device = Device(
        1, "A3-1", "A3", "", "", "",
        inband_ip="10.0.0.2", inband_username="root", inband_password="p",
    )
    plan = TaskPlan(device=device, task=task)
    captured = {}

    monkeypatch.setattr("src.executor.ssh_executor.paramiko.SSHClient", _SSHClient)

    def write_text(output_dir, filename, content):
        captured["txt"] = content
        path = Path(output_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def render(text, output_dir, filename):
        captured["png_input"] = text
        return str(Path(output_dir) / filename)

    monkeypatch.setattr("src.executor.ssh_executor.write_text_file", write_text)
    monkeypatch.setattr("src.executor.ssh_executor.render_text_to_image", render)

    executor = SSHExecutor(command_timeout=1, idle_timeout=0.01)
    result = executor.execute(plan, str(tmp_path))

    assert resolve_task_command(task, "A3") == command
    assert resolve_task_no_split(task, "A3") is True
    assert executor._get_ssh_strategy(device, task) == "terminal_session"
    assert command in captured["txt"]
    assert "value-79" in captured["txt"]
    assert "[TRUNCATED:" in captured["png_input"]
    assert "value-79" not in captured["png_input"]
    assert json.loads(result.runtime_context)["ssh_strategy"] == "terminal_session"

    l1 = Device(1, "L1-1", "L1", "", "", "", inband_ip="10.0.0.3")
    assert executor._get_ssh_strategy(l1, task) == "interactive_shell"
    assert resolve_task_command(task, "L1") == "display interface transceiver"
