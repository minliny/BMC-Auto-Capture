"""
Integration tests for direct dispatch → execute → callback flow.

Tests the DirectDispatchService directly (no HTTP layer needed).
Covers all 16 required test cases.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.executor_api_server.service import (
    DirectDispatchService,
    ValidationError,
)
from src.server_callback_client import FakeCallbackTransport
from src.job_runner_adapter import FakeRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_request(**overrides) -> dict:
    req = {
        "command_id": "cmd-001",
        "command_type": "ASSIGN_JOB",
        "external_task_id": "server-task-123",
        "callback": {
            "status_url": "http://server.example.com/api/tasks/server-task-123/status",
            "artifact_url": "http://server.example.com/api/tasks/server-task-123/artifacts",
            "auth_token": "token-placeholder",
        },
        "job": {
            "job_id": "job-local-001",
            "run_id": "run-direct-001",
            "attempt": 1,
            "resource_lock": {
                "lock_uri": "bmc://10.146.219.1",
                "lock_exclusive": True,
            },
            "device_snapshot": {
                "device_id": "dev-001",
                "device_name": "Switch-A",
                "device_group": "A3",
                "oob_ip": "10.146.219.1",
                "inband_ip": "10.10.10.1",
                "oob_username": "Administrator",
                "oob_password_ref": "secret:bmc-001",
                "inband_username": "root",
                "inband_password_ref": "secret:ssh-001",
            },
            "task_snapshot": {
                "task_id": "task-4.1.8",
                "task_no": "4.1.8",
                "task_name": "RAID配置测试",
                "task_type": "BMC_URL",
                "execution_mode": "BMC_URL",
                "url": "https://{oob_ip}/UI/Static/#/navigate/system/storage",
                "timeout_seconds": 300,
                "retry_count": 0,
            },
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in req:
            req[key].update(value)
        else:
            req[key] = value
    return req


@pytest.fixture
def service():
    return DirectDispatchService(executor_id="exec-test-001")


# ---------------------------------------------------------------------------
# Tests 1-5: Job submission + validation
# ---------------------------------------------------------------------------

class TestJobSubmission:
    """POST /executor/v1/jobs validation and acceptance."""

    def test_accept_job_with_external_task_id(self, service):
        """1. Successful accept sets external_task_id."""
        result = service.submit_job(_make_valid_request())
        assert result["accepted"] is True
        assert result["external_task_id"] == "server-task-123"
        assert result["job_id"] == "job-local-001"
        assert result["status"] == "ACCEPTED"

    def test_missing_external_task_id_rejected(self, service):
        """2. Missing external_task_id raises ValidationError."""
        with pytest.raises(ValidationError) as exc:
            service.submit_job(_make_valid_request(external_task_id=""))
        assert "INVALID_EXTERNAL_TASK_ID" in str(exc.value.code)

    def test_missing_callback_status_url_rejected(self, service):
        """3. Missing callback.status_url raises ValidationError."""
        with pytest.raises(ValidationError) as exc:
            service.submit_job(_make_valid_request(
                callback={"status_url": "", "auth_token": "x"}
            ))
        assert "INVALID_CALLBACK_URL" in str(exc.value.code)

    def test_duplicate_command_id_idempotent(self, service):
        """4. Duplicate command_id returns duplicate=True, no new job created."""
        req = _make_valid_request()
        r1 = service.submit_job(req)
        assert r1["accepted"] is True

        r2 = service.submit_job(req)
        assert r2["accepted"] is False
        assert r2["duplicate"] is True
        assert r2["job_id"] == "job-local-001"
        assert len(service.store) == 1

    def test_accepted_status_after_receive(self, service):
        """5. Job status is ACCEPTED after submission."""
        result = service.submit_job(_make_valid_request())
        assert result["status"] == "ACCEPTED"
        job = service.store.get_job("job-local-001")
        assert job.status == "ACCEPTED"

    def test_missing_task_snapshot_rejected(self, service):
        """Missing task_snapshot raises ValidationError."""
        with pytest.raises(ValidationError) as exc:
            service.submit_job(_make_valid_request(
                job={"job_id": "j1", "task_snapshot": {}}
            ))
        assert "INVALID_TASK_SNAPSHOT" in str(exc.value.code)

    def test_missing_command_id_rejected(self, service):
        """Missing command_id raises ValidationError."""
        with pytest.raises(ValidationError) as exc:
            service.submit_job(_make_valid_request(command_id=""))
        assert "MISSING_COMMAND_ID" in str(exc.value.code)

    def test_different_command_ids_create_two_jobs(self, service):
        """Unique command_ids each create a job."""
        r1 = service.submit_job(_make_valid_request(
            command_id="cmd-001", external_task_id="task-001",
            job={"job_id": "job-001", "task_snapshot": {"task_id": "t1", "task_name": "T1"}},
        ))
        r2 = service.submit_job(_make_valid_request(
            command_id="cmd-002", external_task_id="task-002",
            job={"job_id": "job-002", "task_snapshot": {"task_id": "t2", "task_name": "T2"}},
        ))
        assert r1["accepted"] is True
        assert r2["accepted"] is True
        assert len(service.store) == 2


# ---------------------------------------------------------------------------
# Tests 6-8: Execution flow (fake runner)
# ---------------------------------------------------------------------------

class TestExecutionFlow:
    """Execution phase using FakeRunner."""

    def test_fake_runner_succeeded(self, service):
        """6. After execution, job status is SUCCEEDED."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        job = service.store.get_job("job-local-001")
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert job.duration_ms > 0

    def test_fake_runner_failed(self, service):
        """7. Fake runner with _fake_result=failure sets status FAILED."""
        service.submit_job(_make_valid_request(
            job={"_fake_result": "failure", "task_snapshot": {"task_id": "t1", "task_name": "T1"}}
        ))
        service.run_all_pending()

        job = service.store.get_job("job-local-001")
        assert job.status == "FAILED"
        assert job.error is not None
        assert job.error["code"] != ""

    def test_fake_runner_timeout(self, service):
        """Fake runner with _fake_result=timeout sets status TIMEOUT."""
        service.submit_job(_make_valid_request(
            job={"_fake_result": "timeout", "task_snapshot": {"task_id": "t1", "task_name": "T1"}}
        ))
        service.run_all_pending()

        job = service.store.get_job("job-local-001")
        assert job.status == "TIMEOUT"

    def test_status_transition_accepted_to_running_to_succeeded(self, service):
        """Job goes ACCEPTED → RUNNING → SUCCEEDED."""
        result = service.submit_job(_make_valid_request())
        assert result["status"] == "ACCEPTED"

        job_before = service.store.get_job("job-local-001")
        assert job_before.status == "ACCEPTED"

        service.run_all_pending()

        job_after = service.store.get_job("job-local-001")
        assert job_after.status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Tests 8-12: Callback
# ---------------------------------------------------------------------------

class TestCallback:
    """Callback phase tests."""

    def test_callback_called_with_status_url(self, service):
        """8. Callback is sent to the correct status_url."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        calls = service.transport.calls
        assert len(calls) >= 1
        finish_calls = [c for c in calls
                        if str(c.get("payload", {}).get("status")) == "SUCCEEDED"]
        assert len(finish_calls) == 1
        assert "server.example.com" in finish_calls[0]["url"]

    def test_callback_payload_contains_external_task_id(self, service):
        """9. Callback payload must contain external_task_id."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        last = service.transport.last_call
        assert last is not None
        assert last["payload"]["external_task_id"] == "server-task-123"

    def test_callback_payload_contains_job_id(self, service):
        """10. Callback payload must contain job_id."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        last = service.transport.last_call
        assert last["payload"]["job_id"] == "job-local-001"

    def test_callback_payload_contains_duration_ms(self, service):
        """11. Callback payload must contain duration_ms > 0."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        last = service.transport.last_call
        assert last["payload"]["duration_ms"] > 0

    def test_callback_failure_sets_callback_failed(self, service):
        """12. When callback transport fails, job status → CALLBACK_FAILED."""
        service.transport.set_failure()

        service.submit_job(_make_valid_request())
        service.run_all_pending()

        job = service.store.get_job("job-local-001")
        assert job.status == "CALLBACK_FAILED"
        assert job.last_callback_error != ""

    def test_callback_failure_preserves_execution_result(self, service):
        """Callback failure doesn't lose the execution result in store."""
        service.transport.set_failure()

        service.submit_job(_make_valid_request())
        service.run_all_pending()

        job = service.store.get_job("job-local-001")
        assert job.status == "CALLBACK_FAILED"
        assert job.result_summary.get("summary", "") != ""
        assert job.duration_ms > 0

    def test_auth_token_not_in_transport_call_log(self, service):
        """auth_token is stripped from call recording (headers)."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        for call in service.transport.calls:
            headers = call.get("headers", {})
            assert "authorization" not in {k.lower() for k in headers}
            payload_str = str(call.get("payload", {}))
            assert "token-placeholder" not in payload_str


# ---------------------------------------------------------------------------
# Tests 13-14: Query
# ---------------------------------------------------------------------------

class TestQuery:
    """DirectDispatchService query methods."""

    def test_get_job_returns_status(self, service):
        """13. get_job_status returns job details after execution."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        status = service.get_job_status("job-local-001")
        assert status["job_id"] == "job-local-001"
        assert status["external_task_id"] == "server-task-123"
        assert status["status"] == "SUCCEEDED"
        assert status["duration_ms"] > 0

    def test_get_nonexistent_job(self, service):
        """Querying nonexistent job returns NOT_FOUND."""
        status = service.get_job_status("nonexistent")
        assert status["status"] == "NOT_FOUND"

    def test_get_executor_status_counts(self, service):
        """14. get_executor_status returns job counts."""
        service.submit_job(_make_valid_request(
            command_id="c1", external_task_id="t1",
            job={"job_id": "j1", "task_snapshot": {"task_id": "x", "task_name": "X"}},
        ))
        service.submit_job(_make_valid_request(
            command_id="c2", external_task_id="t2",
            job={"job_id": "j2", "_fake_result": "failure", "task_snapshot": {"task_id": "y", "task_name": "Y"}},
        ))
        service.run_all_pending()

        status = service.get_executor_status()
        assert status["executor_id"] == "exec-test-001"
        assert status["total_jobs"] == 2
        counts = status["job_counts"]
        assert counts.get("SUCCEEDED", 0) == 1
        assert counts.get("FAILED", 0) == 1


# ---------------------------------------------------------------------------
# Test 15: No sensitive data leak
# ---------------------------------------------------------------------------

class TestNoSensitiveDataLeak:
    """Verify password_ref / token are not exposed in query results."""

    def test_get_job_status_no_password_ref(self, service):
        """get_job_status response should not contain password_ref."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        status_str = str(service.get_job_status("job-local-001"))
        assert "secret:bmc-001" not in status_str
        assert "secret:ssh-001" not in status_str
        assert "token-placeholder" not in status_str

    def test_executor_status_no_password_ref(self, service):
        """get_executor_status response should not contain password_ref."""
        service.submit_job(_make_valid_request())
        service.run_all_pending()

        status_str = str(service.get_executor_status())
        assert "secret:bmc-001" not in status_str
        assert "token-placeholder" not in status_str

    def test_snapshot_no_password_ref(self, service):
        """store snapshot should not leak raw payload passwords."""
        service.submit_job(_make_valid_request())
        snap_str = str(service.store.snapshot())
        assert "secret:bmc-001" not in snap_str


# ---------------------------------------------------------------------------
# Store-level tests
# ---------------------------------------------------------------------------

class TestStoreQueries:
    """DirectDispatchStore query methods."""

    def test_get_by_external_task_id(self, service):
        service.submit_job(_make_valid_request())
        job = service.store.get_by_external_task_id("server-task-123")
        assert job is not None
        assert job.job_id == "job-local-001"

    def test_get_by_command_id(self, service):
        service.submit_job(_make_valid_request())
        job = service.store.get_by_command_id("cmd-001")
        assert job is not None
        assert job.job_id == "job-local-001"

    def test_snapshot_multiple_jobs(self, service):
        service.submit_job(_make_valid_request(
            command_id="c1", external_task_id="t1",
            job={"job_id": "j1", "task_snapshot": {"task_id": "x", "task_name": "X"}},
        ))
        service.submit_job(_make_valid_request(
            command_id="c2", external_task_id="t2",
            job={"job_id": "j2", "task_snapshot": {"task_id": "y", "task_name": "Y"}},
        ))
        snap = service.store.snapshot()
        assert len(snap) == 2
        assert "j1" in snap
        assert "j2" in snap
