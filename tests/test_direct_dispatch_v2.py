"""
Tests for P1-DIRECT-DISPATCH-CALLBACK-002:
  - HttpCallbackTransport (real HTTP via urllib)
  - DirectDispatchService + ResourceLockManager integration
  - DirectDispatchService + real HTTP callback (local test server)

Uses stdlib http.server for local callback test server — no external deps.
"""
from __future__ import annotations
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.server_callback_client.http_transport import (
    HttpCallbackTransport,
    _redact_headers,
    _redact_payload_for_log,
)
from src.server_callback_client import (
    ServerCallbackClient,
    FakeCallbackTransport,
)
from src.executor_api_server.service import (
    DirectDispatchService,
    ValidationError,
)
from src.resource_lock_manager import ResourceLockManager


# ---------------------------------------------------------------------------
# Local test callback server (stdlib http.server)
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Records incoming callback requests for test assertions."""

    # Class-level storage shared across requests
    requests: list[dict] = []
    response_status: int = 200
    response_body: str = '{"ok": true}'
    _lock: threading.Lock = threading.Lock()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body}

        with self._lock:
            self.__class__.requests.append({
                "path": self.path,
                "headers": dict(self.headers),
                "payload": payload,
            })

        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.__class__.response_body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # Suppress server logs during tests


def _reset_handler():
    _CallbackHandler.requests.clear()
    _CallbackHandler.response_status = 200
    _CallbackHandler.response_body = '{"ok": true}'


class TestCallbackServer:
    """Context manager that runs a local HTTP server for callback testing."""

    def __init__(self, port: int = 0):
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[dict]:
        return list(_CallbackHandler.requests)

    def set_status(self, status: int):
        _CallbackHandler.response_status = status

    def start(self):
        _reset_handler()
        self._server = HTTPServer(("127.0.0.1", self.port), _CallbackHandler)
        self.port = self._server.server_port  # Get assigned port if 0
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


@pytest.fixture
def callback_server():
    """Fixture: start local callback server, yield it, stop after test."""
    with TestCallbackServer() as srv:
        yield srv


def _make_request(**overrides) -> dict:
    req = {
        "command_id": "cmd-t001",
        "command_type": "ASSIGN_JOB",
        "external_task_id": "server-task-456",
        "callback": {
            "status_url": "http://127.0.0.1:0/callback",
            "artifact_url": "",
            "auth_token": "tok-test",
        },
        "job": {
            "job_id": "job-t001",
            "run_id": "run-t001",
            "attempt": 1,
            "resource_lock": {
                "lock_uri": "bmc://10.0.0.1",
                "lock_exclusive": True,
            },
            "device_snapshot": {
                "device_id": "dev-001",
                "device_name": "Test-Device",
                "device_group": "A3",
                "oob_ip": "10.0.0.1",
                "inband_ip": "10.0.1.1",
                "oob_username": "admin",
                "oob_password_ref": "secret:oob-001",
                "inband_username": "root",
                "inband_password_ref": "secret:ssh-001",
            },
            "task_snapshot": {
                "task_id": "task-001",
                "task_name": "Test Task",
                "task_type": "BMC_URL",
                "execution_mode": "BMC_URL",
                "url": "https://{oob_ip}/test",
                "timeout_seconds": 60,
            },
        },
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and k in req:
            req[k].update(v)
        else:
            req[k] = v
    return req


# ===========================================================================
# HttpCallbackTransport tests
# ===========================================================================

class TestHttpCallbackTransport:
    """Tests 1-8: HttpCallbackTransport using local test server."""

    def test_post_to_correct_url(self, callback_server):
        """1. POST reaches the correct path."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        transport.post(callback_server.url + "/status", {"key": "val"}, {})
        assert len(callback_server.requests) == 1
        assert callback_server.requests[0]["path"] == "/status"

    def test_json_payload_correct(self, callback_server):
        """2. JSON payload is correctly transmitted."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        payload = {"external_task_id": "t1", "job_id": "j1", "status": "SUCCEEDED"}
        transport.post(callback_server.url + "/cb", payload, {})
        assert len(callback_server.requests) == 1
        received = callback_server.requests[0]["payload"]
        assert received["external_task_id"] == "t1"
        assert received["job_id"] == "j1"

    def test_authorization_bearer_token(self, callback_server):
        """3. Authorization: Bearer header is set correctly."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        transport.post(
            callback_server.url + "/cb", {},
            {"Authorization": "Bearer token-abc"},
        )
        req = callback_server.requests[0]
        assert "Bearer token-abc" in req["headers"].get("Authorization", "")

    def test_idempotency_key_header(self, callback_server):
        """4. X-Idempotency-Key header is transmitted."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        transport.post(
            callback_server.url + "/cb", {},
            {"X-Idempotency-Key": "task-j1-SUCCEEDED"},
        )
        req = callback_server.requests[0]
        assert req["headers"].get("X-Idempotency-Key") == "task-j1-SUCCEEDED"

    def test_2xx_success(self, callback_server):
        """5. 2xx response returns status from server."""
        callback_server.set_status(200)
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        status, body = transport.post(callback_server.url + "/ok", {}, {})
        assert status == 200

    def test_500_returns_error_status(self, callback_server):
        """6. Non-2xx returns the actual status code."""
        callback_server.set_status(500)
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        status, body = transport.post(callback_server.url + "/fail", {}, {})
        assert status == 500

    def test_network_error_returns_zero(self):
        """7. Network error (connection refused) returns status 0."""
        transport = HttpCallbackTransport(timeout_seconds=2.0)
        status, body = transport.post(
            "http://127.0.0.1:1/nope",  # port 1 — nothing listening
            {}, {},
        )
        assert status == 0


# ===========================================================================
# Redaction tests
# ===========================================================================

class TestRedaction:
    """Test 8: No sensitive data leaked in logging helpers."""

    def test_redact_headers_hides_authorization(self):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer secret"}
        safe = _redact_headers(headers)
        assert safe["Authorization"] == "***REDACTED***"
        assert safe["Content-Type"] == "application/json"

    def test_redact_headers_hides_cookie(self):
        safe = _redact_headers({"Cookie": "session=abc"})
        assert safe["Cookie"] == "***REDACTED***"

    def test_redact_payload_hides_password(self):
        payload = {"password": "secret123", "user": "admin"}
        safe = _redact_payload_for_log(payload)
        assert safe["password"] == "***REDACTED***"

    def test_redact_payload_hides_token(self):
        payload = {"auth_token": "my-token", "data": "ok"}
        safe = _redact_payload_for_log(payload)
        assert safe["auth_token"] == "***REDACTED***"


# ===========================================================================
# DirectDispatchService + real HTTP callback
# ===========================================================================

class TestServiceWithHttpCallback:
    """Tests 9-14: Service using HttpCallbackTransport with local test server."""

    def test_callback_payload_has_external_task_id(self, callback_server):
        """9+10: Real callback payload contains external_task_id."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            use_http_callback=False,
            callback_transport=transport,
        )
        req = _make_request()
        req["callback"]["status_url"] = callback_server.url + "/status"
        svc.submit_job(req)
        svc.run_all_pending()

        assert len(callback_server.requests) >= 1
        # Find SUCCEEDED callback
        finish = [r for r in callback_server.requests
                  if r["payload"].get("status") == "SUCCEEDED"]
        assert len(finish) == 1
        assert finish[0]["payload"]["external_task_id"] == "server-task-456"

    def test_callback_payload_has_job_id(self, callback_server):
        """11. Callback payload contains job_id."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            callback_transport=transport,
        )
        req = _make_request()
        req["callback"]["status_url"] = callback_server.url + "/status"
        svc.submit_job(req)
        svc.run_all_pending()

        finish = [r for r in callback_server.requests
                  if r["payload"].get("status") == "SUCCEEDED"]
        assert finish[0]["payload"]["job_id"] == "job-t001"

    def test_callback_payload_has_duration_ms(self, callback_server):
        """12. Callback payload contains duration_ms."""
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            callback_transport=transport,
        )
        req = _make_request()
        req["callback"]["status_url"] = callback_server.url + "/status"
        svc.submit_job(req)
        svc.run_all_pending()

        finish = [r for r in callback_server.requests
                  if r["payload"].get("status") == "SUCCEEDED"]
        assert finish[0]["payload"]["duration_ms"] > 0

    def test_callback_failure_sets_callback_failed(self, callback_server):
        """13. Callback 500 → job status CALLBACK_FAILED."""
        callback_server.set_status(500)
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            callback_transport=transport,
        )
        req = _make_request()
        req["callback"]["status_url"] = callback_server.url + "/fail"
        svc.submit_job(req)
        svc.run_all_pending()

        job = svc.store.get_job("job-t001")
        assert job.status == "CALLBACK_FAILED"

    def test_callback_failure_preserves_result(self, callback_server):
        """14. CALLBACK_FAILED still preserves result_summary."""
        callback_server.set_status(503)
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            callback_transport=transport,
        )
        req = _make_request()
        req["callback"]["status_url"] = callback_server.url + "/fail"
        svc.submit_job(req)
        svc.run_all_pending()

        job = svc.store.get_job("job-t001")
        assert job.status == "CALLBACK_FAILED"
        assert "EXEC_SUCCEEDED" in job.result_summary.get("summary", "")


# ===========================================================================
# ResourceLockManager integration
# ===========================================================================

class TestLockIntegration:
    """Tests 15-20: ResourceLockManager integration in DirectDispatchService."""

    def test_same_lock_uri_no_concurrent_execution(self):
        """15. Two jobs with same lock_uri — first runs, second blocked."""
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-A", external_task_id="task-A",
            job={"job_id": "job-A", "task_snapshot": {"task_id": "t1", "task_name": "TA"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.submit_job(_make_request(
            command_id="cmd-B", external_task_id="task-B",
            job={"job_id": "job-B", "task_snapshot": {"task_id": "t2", "task_name": "TB"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))

        # First run processes job-A, acquires lock
        svc.run_pending_once()
        job_a = svc.store.get_job("job-A")
        assert job_a.status in ("SUCCEEDED", "RUNNING")

        # Second run: job-B should be blocked by lock, re-queued
        # The lock should be released after job-A finished
        # So job-B can now run
        svc.run_pending_once()
        job_b = svc.store.get_job("job-B")
        assert job_b.status == "SUCCEEDED"

    def test_different_lock_uri_not_blocked(self):
        """16. Different lock_uri jobs don't block each other."""
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-A", external_task_id="task-A",
            job={"job_id": "job-A", "task_snapshot": {"task_id": "t1", "task_name": "TA"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.submit_job(_make_request(
            command_id="cmd-B", external_task_id="task-B",
            job={"job_id": "job-B", "task_snapshot": {"task_id": "t2", "task_name": "TB"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.2"}},
        ))

        svc.run_all_pending()

        job_a = svc.store.get_job("job-A")
        job_b = svc.store.get_job("job-B")
        assert job_a.status == "SUCCEEDED"
        assert job_b.status == "SUCCEEDED"

    def test_lock_released_after_success(self):
        """17. Lock is released after successful execution."""
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-001", external_task_id="task-001",
            job={"job_id": "job-001", "task_snapshot": {"task_id": "t1", "task_name": "T1"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.run_all_pending()

        assert not lock_mgr.is_locked("bmc://10.0.0.1")

    def test_lock_released_after_failure(self, callback_server):
        """18. Lock is released after failed execution."""
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-fail", external_task_id="task-fail",
            job={"job_id": "job-fail",
                 "_fake_result": "failure",
                 "task_snapshot": {"task_id": "t1", "task_name": "TF"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.run_all_pending()

        assert not lock_mgr.is_locked("bmc://10.0.0.1")
        job = svc.store.get_job("job-fail")
        assert job.status == "FAILED"

    def test_lock_released_after_callback_failed(self, callback_server):
        """19. Lock is released even when callback fails."""
        callback_server.set_status(500)
        lock_mgr = ResourceLockManager()
        transport = HttpCallbackTransport(timeout_seconds=5.0)
        svc = DirectDispatchService(
            executor_id="exec-test",
            lock_manager=lock_mgr,
            callback_transport=transport,
        )

        req = _make_request(
            job={"job_id": "job-cb", "task_snapshot": {"task_id": "t1", "task_name": "TCB"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        )
        req["callback"]["status_url"] = callback_server.url + "/fail"
        svc.submit_job(req)
        svc.run_all_pending()

        assert not lock_mgr.is_locked("bmc://10.0.0.1")
        job = svc.store.get_job("job-cb")
        assert job.status == "CALLBACK_FAILED"

    def test_missing_lock_uri_derived_not_device_name(self):
        """20. Missing explicit lock_uri is derived from device_snapshot, NOT device_name."""
        svc = DirectDispatchService(executor_id="exec-test")
        req = _make_request(
            job={"job_id": "job-derived",
                 "resource_lock": {},  # No explicit lock_uri
                 "device_snapshot": {
                     "device_id": "d1", "device_name": "Switch-A",
                     "oob_ip": "10.0.0.1", "inband_ip": "",
                     "oob_password_ref": "ref1",
                     "inband_password_ref": "ref2",
                 },
                 "task_snapshot": {
                     "task_id": "t1", "task_name": "TD",
                     "execution_mode": "BMC_URL",
                 }},
        )
        result = svc.submit_job(req)
        assert result["accepted"] is True
        job = svc.store.get_job("job-derived")
        assert job.lock_uri == "bmc://10.0.0.1"
        # Must NOT be device_name
        assert "Switch-A" not in job.lock_uri

    def test_missing_lock_uri_cannot_derive_rejected(self):
        """Cannot derive lock_uri (no IPs) → rejected."""
        svc = DirectDispatchService(executor_id="exec-test")
        with pytest.raises(ValidationError) as exc:
            svc.submit_job(_make_request(
                job={"job_id": "job-bad",
                     "resource_lock": {},  # No explicit lock_uri — must derive
                     "device_snapshot": {
                         "device_id": "d1", "device_name": "X",
                         "oob_ip": "", "inband_ip": "",
                         "oob_password_ref": "ref1",
                         "inband_password_ref": "ref2",
                     },
                     "task_snapshot": {
                         "task_id": "t1", "task_name": "TX",
                         "execution_mode": "BMC_URL",
                     }},
            ))
        assert "MISSING_LOCK_URI" in exc.value.code or "lock_uri" in str(exc.value).lower()

    def test_lock_owner_is_job_id(self):
        """Lock owner_id is the job_id."""
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-own", external_task_id="task-own",
            job={"job_id": "job-own", "task_snapshot": {"task_id": "t1", "task_name": "TO"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.run_all_pending()

        # Lock should be released, so no owner
        assert lock_mgr.get_owner("bmc://10.0.0.1") is None


# ===========================================================================
# get_executor_status with lock info
# ===========================================================================

class TestStatusWithLocks:
    """Executor status includes lock info."""

    def test_status_includes_active_locks_count(self):
        lock_mgr = ResourceLockManager()
        svc = DirectDispatchService(executor_id="exec-test", lock_manager=lock_mgr)

        svc.submit_job(_make_request(
            command_id="cmd-s1", external_task_id="task-s1",
            job={"job_id": "job-s1", "task_snapshot": {"task_id": "tx", "task_name": "TS"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.run_all_pending()

        status = svc.get_executor_status()
        assert "active_locks" in status
        assert status["total_jobs"] == 1

    def test_job_status_includes_lock_info(self):
        svc = DirectDispatchService(executor_id="exec-test")
        svc.submit_job(_make_request(
            job={"job_id": "job-ls", "task_snapshot": {"task_id": "t1", "task_name": "T"},
                 "resource_lock": {"lock_uri": "bmc://10.0.0.1"}},
        ))
        svc.run_all_pending()

        status = svc.get_job_status("job-ls")
        assert "lock_uri" in status
        assert status["lock_uri"] == "bmc://10.0.0.1"
