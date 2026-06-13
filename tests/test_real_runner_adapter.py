"""
Tests for P1-DIRECT-DISPATCH-CALLBACK-003:
  - secret_resolver
  - RealRunnerAdapter (device/task conversion, routing)
  - DirectDispatchService runner_mode
  - Startup script args

No real BMC/SSH — uses monkeypatch/fake executor for path verification.
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.secret_resolver import (
    resolve_secret,
    resolve_secrets,
    SecretError,
    SECRET_REF_MISSING,
    SECRET_NOT_FOUND,
    SECRET_RESOLVE_FAILED,
)
from src.job_runner_adapter import (
    FakeRunner,
    RealRunnerAdapter,
    JobResult,
    UnsupportedTaskTypeError,
)
from src.executor_api_server.service import DirectDispatchService
from src.server_callback_client import FakeCallbackTransport
from src.resource_lock_manager import ResourceLockManager


# ===========================================================================
# Secret resolver tests
# ===========================================================================

class TestSecretResolver:
    """Tests 1-4: secret_ref resolution."""

    def test_env_var_resolved(self, monkeypatch):
        """1. env:VAR_NAME resolves from environment."""
        monkeypatch.setenv("TEST_BMC_PASS", "my-secret-password")
        result = resolve_secret("env:TEST_BMC_PASS")
        assert result == "my-secret-password"

    def test_missing_env_returns_error(self):
        """2. Missing env var raises SECRET_NOT_FOUND."""
        with pytest.raises(SecretError) as exc:
            resolve_secret("env:NONEXISTENT_VAR_XYZ123")
        assert exc.value.code == SECRET_NOT_FOUND

    def test_empty_ref_returns_missing(self):
        """3. Empty secret_ref raises SECRET_REF_MISSING."""
        with pytest.raises(SecretError) as exc:
            resolve_secret("")
        assert exc.value.code == SECRET_REF_MISSING

    def test_env_prefix_no_var_name_returns_missing(self):
        """env: with no variable name raises SECRET_REF_MISSING."""
        with pytest.raises(SecretError) as exc:
            resolve_secret("env:")
        assert exc.value.code == SECRET_REF_MISSING

    def test_plaintext_placeholder_returned_as_is(self):
        """Plain string (no prefix) is returned as-is (v0.1 placeholder)."""
        result = resolve_secret("secret:bmc-001")
        assert result == "secret:bmc-001"

    def test_exception_does_not_contain_password(self):
        """4. Exception message does not leak the secret_ref value."""
        try:
            resolve_secret("env:NONEXISTENT_VAR_XYZ123")
        except SecretError as e:
            msg = str(e)
            assert "NONEXISTENT_VAR_XYZ123" in msg  # var name is OK
            # Should NOT contain any password value

    def test_resolve_secrets_both_refs(self, monkeypatch):
        """resolve_secrets handles oob + inband refs."""
        monkeypatch.setenv("OOB_PASS", "oob123")
        monkeypatch.setenv("INBAND_PASS", "inband456")
        result = resolve_secrets({
            "oob_password_ref": "env:OOB_PASS",
            "inband_password_ref": "env:INBAND_PASS",
        })
        assert result["oob_password"] == "oob123"
        assert result["inband_password"] == "inband456"


# ===========================================================================
# RealRunnerAdapter — model conversion tests
# ===========================================================================

class TestRealRunnerAdapterConversion:
    """Tests 5-8: Device/Task conversion and routing."""

    def _make_device_snapshot(self, **overrides) -> dict:
        d = {
            "device_id": "dev-001", "device_name": "Test-Switch",
            "device_group": "A3", "oob_ip": "10.0.0.1", "inband_ip": "10.0.1.1",
            "oob_username": "admin", "oob_password_ref": "plain:pw1",
            "inband_username": "root", "inband_password_ref": "plain:pw2",
        }
        d.update(overrides)
        return d

    def _make_task_snapshot(self, **overrides) -> dict:
        t = {
            "task_id": "task-001", "task_name": "Test Task",
            "task_type": "BMC", "execution_mode": "BMC_URL",
            "url": "https://10.0.0.1/test", "timeout_seconds": 60,
        }
        t.update(overrides)
        return t

    def test_bmc_url_device_from_snapshot(self):
        """5. BMC_URL: DeviceSnapshot → Device conversion."""
        adapter = RealRunnerAdapter()
        device = adapter._device_from_snapshot(
            self._make_device_snapshot(), "pw1", "pw2",
        )
        assert device.device_name == "Test-Switch"
        assert device.bmc_ip == "10.0.0.1"
        assert device.bmc_username == "admin"
        assert device.bmc_password == "pw1"

    def test_ssh_device_from_snapshot(self):
        """6. SSH_CMD: DeviceSnapshot → Device conversion."""
        adapter = RealRunnerAdapter()
        device = adapter._device_from_snapshot(
            self._make_device_snapshot(), "", "sshpass",
        )
        assert device.inband_ip == "10.0.1.1"
        assert device.inband_password == "sshpass"

    def test_bmc_task_from_snapshot(self):
        """BMC task uses url field."""
        adapter = RealRunnerAdapter()
        task = adapter._task_from_snapshot(self._make_task_snapshot(url="https://10.0.0.1/storage"))
        assert task.command_or_url == "https://10.0.0.1/storage"
        assert task.task_type == "BMC"
        assert task.execution_mode == "BMC_URL"

    def test_ssh_task_from_snapshot_uses_ssh_cmd(self):
        """SSH task uses ssh_cmd field."""
        adapter = RealRunnerAdapter()
        task = adapter._task_from_snapshot(self._make_task_snapshot(
            task_type="SSH", execution_mode="SSH_CMD",
            ssh_cmd="show version",
        ))
        assert task.command_or_url == "show version"

    def test_ssh_task_fallback_to_command_field(self):
        """SSH task falls back to command field."""
        adapter = RealRunnerAdapter()
        task = adapter._task_from_snapshot(self._make_task_snapshot(
            task_type="SSH", execution_mode="SSH_CMD",
            command="display device",
        ))
        assert task.command_or_url == "display device"

    def test_unsupported_task_type_in_run_job(self):
        """7. Unsupported task_type returns FAILED with UNSUPPORTED_TASK_TYPE."""
        adapter = RealRunnerAdapter()
        result = adapter.run_job({
            "device_snapshot": self._make_device_snapshot(),
            "task_snapshot": self._make_task_snapshot(
                task_type="UNKNOWN", execution_mode="UNKNOWN",
            ),
        })
        assert result.status == "FAILED"
        assert result.error["code"] == "UNSUPPORTED_TASK_TYPE"


# ===========================================================================
# RealRunnerAdapter — execute path (monkeypatched)
# ===========================================================================

class _FakeExecResult:
    """Mimics ExecutionResult for testing path mapping."""
    def __init__(self, status="EXEC_SUCCESS", reason="", screenshots=(),
                 html_file="", txt_file="", step_results=None, duration=1.5):
        self.execution_status = status
        self.execution_failure_reason = reason
        self.screenshots = screenshots
        self.html_file = html_file
        self.txt_file = txt_file
        self.step_results = step_results or []
        self.duration_seconds = duration


class TestRealRunnerAdapterExecute:
    """Tests 9-14: Execute path with monkeypatched executors."""

    def _adapter_with_patched_executors(self, monkeypatch, bmc_result=None, ssh_result=None):
        """Create adapter with BMC/SSH execute methods replaced."""
        adapter = RealRunnerAdapter()

        if bmc_result is not None:
            def _fake_bmc_execute(self_ignored, plan, output_root):
                return bmc_result
            monkeypatch.setattr(
                "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc_execute
            )

        if ssh_result is not None:
            def _fake_ssh_execute(self_ignored, plan, output_root):
                return ssh_result
            monkeypatch.setattr(
                "src.executor.ssh_executor.SSHExecutor.execute", _fake_ssh_execute
            )

        return adapter

    def test_bmc_success_maps_to_succeeded(self, monkeypatch):
        """9. BMC EXEC_SUCCESS → JobResult SUCCEEDED."""
        adapter = self._adapter_with_patched_executors(
            monkeypatch,
            bmc_result=_FakeExecResult(
                status="EXEC_SUCCESS",
                screenshots=("/tmp/out/test.png",),
                html_file="/tmp/out/test.html",
                step_results=[],
            ),
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "10.0.0.1",
                                "oob_username": "a", "oob_password_ref": "plain:x",
                                "inband_ip": "", "inband_username": "",
                                "inband_password_ref": ""},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "BMC", "execution_mode": "BMC_URL",
                              "url": "https://10.0.0.1/"},
        })
        assert result.status == "SUCCEEDED"
        assert result.duration_ms > 0
        assert len(result.artifacts) >= 1
        assert any(a["artifact_type"] == "PNG_SCREENSHOT" for a in result.artifacts)

    def test_ssh_failure_maps_to_failed(self, monkeypatch):
        """10. SSH failure → JobResult FAILED."""
        adapter = self._adapter_with_patched_executors(
            monkeypatch,
            ssh_result=_FakeExecResult(
                status="EXEC_FAILED",
                reason="SSH auth failed",
            ),
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "",
                                "inband_ip": "10.0.1.1", "inband_username": "u",
                                "oob_username": "", "oob_password_ref": "",
                                "inband_password_ref": "plain:x"},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "SSH", "execution_mode": "SSH_CMD",
                              "ssh_cmd": "show ver"},
        })
        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error["code"] != ""

    def test_bmc_crash_returns_failed(self, monkeypatch):
        """Executor crash → FAILED with error."""
        adapter = RealRunnerAdapter()
        # Make BrowserManager fail
        def _crash(self_ignored, plan, output_root):
            raise RuntimeError("Browser crashed")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _crash
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "10.0.0.1",
                                "oob_username": "a", "oob_password_ref": "plain:x",
                                "inband_ip": "", "inband_username": "",
                                "inband_password_ref": ""},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "BMC", "execution_mode": "BMC_URL",
                              "url": "https://10.0.0.1/"},
        })
        assert result.status == "FAILED"
        assert result.error["code"] == "BMC_EXECUTOR_CRASH"

    def test_job_result_has_duration_ms(self, monkeypatch):
        """13. JobResult includes duration_ms."""
        adapter = self._adapter_with_patched_executors(
            monkeypatch,
            bmc_result=_FakeExecResult(status="EXEC_SUCCESS", duration=2.5),
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "10.0.0.1",
                                "oob_username": "a", "oob_password_ref": "plain:x",
                                "inband_ip": "", "inband_username": "",
                                "inband_password_ref": ""},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "BMC", "execution_mode": "BMC_URL",
                              "url": "https://10.0.0.1/"},
        })
        assert result.duration_ms > 0

    def test_job_result_has_artifact_metadata(self, monkeypatch):
        """14. JobResult includes artifact metadata (no upload)."""
        adapter = self._adapter_with_patched_executors(
            monkeypatch,
            bmc_result=_FakeExecResult(
                status="EXEC_SUCCESS",
                screenshots=("/tmp/out/ss1.png", "/tmp/out/ss2.png"),
                html_file="/tmp/out/page.html",
            ),
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "10.0.0.1",
                                "oob_username": "a", "oob_password_ref": "plain:x",
                                "inband_ip": "", "inband_username": "",
                                "inband_password_ref": ""},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "BMC", "execution_mode": "BMC_URL",
                              "url": "https://10.0.0.1/"},
        })
        types = [a["artifact_type"] for a in result.artifacts]
        assert "PNG_SCREENSHOT" in types
        assert "HTML_PAGE" in types

    def test_ssh_artifact_includes_txt(self, monkeypatch):
        """SSH result includes TXT artifact."""
        adapter = self._adapter_with_patched_executors(
            monkeypatch,
            ssh_result=_FakeExecResult(
                status="EXEC_SUCCESS",
                txt_file="/tmp/out/output.txt",
            ),
        )
        result = adapter.run_job({
            "device_snapshot": {"device_name": "D1", "oob_ip": "",
                                "inband_ip": "10.0.1.1", "inband_username": "u",
                                "oob_username": "", "oob_password_ref": "",
                                "inband_password_ref": "plain:x"},
            "task_snapshot": {"task_id": "t1", "task_name": "T1",
                              "task_type": "SSH", "execution_mode": "SSH_CMD",
                              "ssh_cmd": "show ver"},
        })
        types = [a["artifact_type"] for a in result.artifacts]
        assert "TXT_SSH_OUTPUT" in types

    def test_non_empty_bad_ref_raises_in_run_job(self):
        """12. Non-empty unresolvable secret_ref raises SecretError."""
        adapter = RealRunnerAdapter()
        with pytest.raises(SecretError) as exc:
            adapter.run_job({
                "device_snapshot": {"device_name": "D1", "oob_ip": "10.0.0.1",
                                    "oob_username": "a", "oob_password_ref": "env:NONEXISTENT_XYZ",
                                    "inband_ip": "", "inband_username": "",
                                    "inband_password_ref": ""},
                "task_snapshot": {"task_id": "t1", "task_name": "T1",
                                  "task_type": "BMC", "execution_mode": "BMC_URL",
                                  "url": "https://10.0.0.1/"},
            })
        assert exc.value.code == SECRET_NOT_FOUND

    def test_empty_password_ref_allowed_for_unused_protocol(self):
        """Empty password_ref is OK when that protocol isn't used."""
        adapter = RealRunnerAdapter()
        # BMC-only job: empty inband_password_ref is fine
        secrets = resolve_secrets({
            "oob_password_ref": "plain:bmc-pass",
            "inband_password_ref": "",  # SSH not used
        })
        assert secrets["oob_password"] == "plain:bmc-pass"
        assert secrets["inband_password"] == ""


# ===========================================================================
# DirectDispatchService runner_mode tests
# ===========================================================================

def _make_req(**overrides) -> dict:
    req = {
        "command_id": "cmd-t001",
        "command_type": "ASSIGN_JOB",
        "external_task_id": "server-task-1",
        "callback": {"status_url": "http://127.0.0.1/cb", "auth_token": "tok"},
        "job": {
            "job_id": "job-t001", "run_id": "run-t001", "attempt": 1,
            "resource_lock": {"lock_uri": "bmc://10.0.0.1"},
            "device_snapshot": {
                "device_id": "d1", "device_name": "D1", "device_group": "A3",
                "oob_ip": "10.0.0.1", "inband_ip": "10.0.1.1",
                "oob_username": "admin", "oob_password_ref": "plain:pw1",
                "inband_username": "root", "inband_password_ref": "plain:pw2",
            },
            "task_snapshot": {
                "task_id": "t1", "task_name": "Test",
                "task_type": "BMC", "execution_mode": "BMC_URL",
                "url": "https://10.0.0.1/", "timeout_seconds": 60,
            },
        },
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and k in req:
            req[k].update(v)
        else:
            req[k] = v
    return req


class TestServiceRunnerMode:
    """Tests 15-21: DirectDispatchService runner_mode."""

    def test_default_runner_is_fake(self):
        """15. Default runner_mode is fake."""
        svc = DirectDispatchService()
        assert isinstance(svc._runner, FakeRunner)

    def test_runner_mode_real_uses_real_adapter(self, monkeypatch):
        """16. runner_mode=real uses RealRunnerAdapter."""
        # Patch BMCExecutor.execute to avoid real browser
        def _fake_bmc(self_ignored, plan, output_root):
            return _FakeExecResult(status="EXEC_SUCCESS")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc
        )

        svc = DirectDispatchService(runner_mode="real", allow_real_runner=True)
        from src.job_runner_adapter import RealRunnerAdapter
        assert isinstance(svc._runner, RealRunnerAdapter)

    def test_real_runner_success_callback_payload(self, monkeypatch):
        """17. Real runner success → callback payload SUCCEEDED."""
        def _fake_bmc(self_ignored, plan, output_root):
            return _FakeExecResult(status="EXEC_SUCCESS")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc
        )

        svc = DirectDispatchService(runner_mode="real", allow_real_runner=True)
        svc.submit_job(_make_req())
        svc.run_all_pending()

        calls = svc.transport.calls
        finish = [c for c in calls if c["payload"].get("status") == "SUCCEEDED"]
        assert len(finish) == 1
        assert finish[0]["payload"]["external_task_id"] == "server-task-1"

    def test_real_runner_failure_callback_failed(self, monkeypatch):
        """18. Real runner failure → callback FAILED."""
        def _fake_bmc(self_ignored, plan, output_root):
            return _FakeExecResult(status="EXEC_FAILED", reason="BMC down")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc
        )

        svc = DirectDispatchService(runner_mode="real", allow_real_runner=True)
        svc.submit_job(_make_req())
        svc.run_all_pending()

        calls = svc.transport.calls
        fail_calls = [c for c in calls if c["payload"].get("status") == "FAILED"]
        assert len(fail_calls) == 1

    def test_real_runner_exception_lock_released(self, monkeypatch):
        """19. Runner crash → FAILED, lock released."""
        def _crash(self_ignored, plan, output_root):
            raise RuntimeError("boom")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _crash
        )

        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(
            runner_mode="real", lock_manager=lock_mgr,
            allow_real_runner=True,
        )
        svc.submit_job(_make_req())
        svc.run_all_pending()

        assert not lock_mgr.is_locked("bmc://10.0.0.1")
        job = svc.store.get_job("job-t001")
        assert job.status == "FAILED"

    def test_callback_failed_lock_still_released(self, monkeypatch):
        """20. CALLBACK_FAILED → lock still released."""
        def _fake_bmc(self_ignored, plan, output_root):
            return _FakeExecResult(status="EXEC_SUCCESS")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc
        )

        transport = FakeCallbackTransport()
        transport.set_failure()
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(
            runner_mode="real", lock_manager=lock_mgr,
            callback_transport=transport,
            allow_real_runner=True,
        )
        svc.submit_job(_make_req())
        svc.run_all_pending()

        assert not lock_mgr.is_locked("bmc://10.0.0.1")

    def test_callback_payload_no_password(self, monkeypatch):
        """21. Callback payload does not leak password."""
        def _fake_bmc(self_ignored, plan, output_root):
            return _FakeExecResult(status="EXEC_SUCCESS")
        monkeypatch.setattr(
            "src.executor.bmc_executor.BMCExecutor.execute", _fake_bmc
        )

        svc = DirectDispatchService(runner_mode="real", allow_real_runner=True)
        svc.submit_job(_make_req())
        svc.run_all_pending()

        for call in svc.transport.calls:
            payload_str = str(call["payload"])
            assert "plain:pw1" not in payload_str
            assert "plain:pw2" not in payload_str

    def test_fake_runner_still_default(self):
        """Explicit runner_mode=fake uses FakeRunner."""
        svc = DirectDispatchService(runner_mode="fake")
        assert isinstance(svc._runner, FakeRunner)


# ===========================================================================
# Startup script tests
# ===========================================================================

class TestStartupScript:
    """Tests 22-24: Startup script verification."""

    def test_help_succeeds(self):
        """22. --help exits successfully."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/start_executor_api_server.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_help_contains_runner(self):
        """23. --help includes --runner option."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/start_executor_api_server.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "--runner" in result.stdout

    def test_default_runner_is_fake(self):
        """24. Default runner is fake (in help text)."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/start_executor_api_server.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "fake" in result.stdout.lower()
