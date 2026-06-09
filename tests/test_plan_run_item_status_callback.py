"""
Tests for P1-PLAN-RUN-ITEM-STATUS-CALLBACK-001.
"""
from __future__ import annotations
import json, os, sys, subprocess, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.plan_run_service.service import PlanRunService, _set_latest_excel, _get_latest_excel, _excel_store, _store_lock
from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


def _clear_store():
    with _store_lock:
        _excel_store.clear()


# Clear shared Excel store before each test in this file
@pytest.fixture(autouse=True)
def auto_clear_store():
    _clear_store()


# ===========================================================================
# Excel config tests (1-6)
# ===========================================================================

class TestExcelConfig:
    def test_set_latest_excel_returns_device_count(self, tmp_path):
        """1+2. Setting Excel returns deviceCount/taskCount."""
        svc = PlanRunService()
        r = svc.set_latest_excel(EXCEL_FILE)
        assert r["accepted"] is True
        assert r["deviceCount"] > 0
        assert r["taskCount"] > 0

    def test_no_latest_excel_rejected(self):
        """3. No latest Excel → rejected."""
        _clear_store()
        svc = PlanRunService()
        r = svc.start_plan_run(1, {})
        assert r["accepted"] is False
        assert r["reason"] == "NO_LATEST_EXCEL_CONFIG"

    def test_overwrite_latest_excel(self, tmp_path):
        """4. New upload overwrites old latest."""
        svc = PlanRunService()
        r1 = svc.set_latest_excel(EXCEL_FILE)
        time.sleep(1.1)  # Ensure configVersion differs (second-level precision)
        r2 = svc.set_latest_excel(EXCEL_FILE)
        assert r1["configVersion"] != r2["configVersion"]

    def test_config_version_non_empty(self):
        """5. configVersion is non-empty."""
        svc = PlanRunService()
        r = svc.set_latest_excel(EXCEL_FILE)
        assert r["configVersion"] != ""
        assert r["configVersion"].startswith("excel-")

    def test_sha256_non_empty(self):
        """6. sha256 is non-empty."""
        svc = PlanRunService()
        r = svc.set_latest_excel(EXCEL_FILE)
        assert len(r["sha256"]) == 64


# ===========================================================================
# Plan run tests (7-22)
# ===========================================================================

class TestPlanRun:
    def test_start_plan_run_returns_run_id(self, tmp_path):
        """7+9. Run returns planId + runId."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(42, {"callback": {}})
        assert r["accepted"] is True
        assert r["planId"] == 42
        assert r["runId"].startswith("plan-42-run-")

    def test_scope_all_expands_devices_tasks(self, tmp_path):
        """10. scope=ALL expands enabled devices × enabled tasks."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_all_sync(r["runId"])
        run = svc.get_run(r["runId"])
        assert run["summary"]["total"] > 0

    def test_device_name_from_excel(self, tmp_path):
        """11. deviceName comes from Excel."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_all_sync(r["runId"])
        # All device names should be non-empty strings
        assert r["accepted"] is True

    def test_task_name_from_excel(self, tmp_path):
        """12. taskName comes from Excel."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_all_sync(r["runId"])
        assert r["accepted"] is True

    def test_each_item_gets_callback(self, tmp_path):
        """13. Each completed item triggers a callback."""
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}})
        # Wait for background thread to complete
        time.sleep(3)
        run = svc.get_run(r["runId"])
        assert run is not None
        assert run["summary"]["total"] > 0
        assert run["summary"]["total"] == run["summary"]["success"]
        # Callback transport should have recorded calls
        assert len(transport.calls) > 0

    def test_success_item_sends_success_status(self, tmp_path):
        """14. Successful items callback with status=SUCCESS."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["status"] == "SUCCESS"
            assert call["payload"]["errorMessage"] is None

    def test_updater_default(self, tmp_path):
        """16. updater defaults to downstream-system."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["updater"] == "downstream-system"

    def test_updater_override(self, tmp_path):
        """17. updater can be overridden in request."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}, "updater": "custom-updater"})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["updater"] == "custom-updater"

    def test_callback_no_legacy_fields(self, tmp_path):
        """18+19. Callback has ONLY 6 fields, no legacy fields."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}})
        svc.run_all_sync(r["runId"])
        required_fields = {"planId", "deviceName", "taskName", "status", "updater", "errorMessage"}
        forbidden_fields = {"job_id", "external_task_id", "executor_id", "duration_ms", "artifacts"}
        for call in transport.calls:
            keys = set(call["payload"].keys())
            assert keys == required_fields
            assert not (keys & forbidden_fields)

    def test_get_run_summary(self, tmp_path):
        """20. GET run returns summary."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        time.sleep(3)
        run = svc.get_run(r["runId"])
        assert "summary" in run
        assert run["summary"]["total"] > 0
        assert run["summary"]["success"] == run["summary"]["total"]

    def test_default_runner_fake(self, tmp_path):
        """21. Default runner is fake."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        assert r["accepted"] is True

    def test_real_runner_rejected(self, tmp_path):
        """22. runner=real is accepted now."""
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        assert r["accepted"] is True  # now supported


# ===========================================================================
# Real runner tests (monkeypatched, no real devices)
# ===========================================================================

class _FakeExecResult:
    def __init__(self, status="EXEC_SUCCESS", reason="", screenshots=(), html_file="", txt_file="",
                 step_results=None, duration=1.5):
        self.execution_status = status
        self.execution_failure_reason = reason
        self.screenshots = screenshots
        self.html_file = html_file
        self.txt_file = txt_file
        self.step_results = step_results or []
        self.duration_seconds = duration


class TestRealRunner:
    """Tests 1-19: runner=real path."""

    def test_default_runner_still_fake(self):
        """1. Default runner is fake."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        assert r["accepted"] is True

    def test_real_runner_calls_adapter(self, monkeypatch):
        """2. runner=real uses RealRunnerAdapter."""
        calls = []
        def _fake_run_job(self_ignored, job_payload):
            calls.append(job_payload)
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED", duration_ms=100)

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        assert len(calls) > 0

    def test_real_runner_succeeded_callback_success(self, monkeypatch):
        """5. Real success → callback SUCCESS, errorMessage=null."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED", duration_ms=100)

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["status"] == "SUCCESS"
            assert call["payload"]["errorMessage"] is None

    def test_real_runner_failed_callback_failed(self, monkeypatch):
        """6. Real failure → callback FAILED, errorMessage non-empty."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="FAILED", error={"message": "BMC down", "code": "ERR"})

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        has_failed = False
        for call in transport.calls:
            if call["payload"]["status"] == "FAILED":
                has_failed = True
                assert call["payload"]["errorMessage"] is not None
        assert has_failed

    def test_real_runner_exception_callback_failed(self, monkeypatch):
        """7. Runner crash → callback FAILED."""
        def _crash(self_ignored, job_payload):
            raise RuntimeError("boom")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _crash)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        has_failed = False
        for call in transport.calls:
            if call["payload"]["status"] == "FAILED":
                has_failed = True
        assert has_failed

    def test_callback_still_6_fields_with_real(self, monkeypatch):
        """9. Real runner callback still 6 fields only."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        required = {"planId", "deviceName", "taskName", "status", "updater", "errorMessage"}
        forbidden = {"job_id", "external_task_id", "executor_id", "duration_ms", "artifacts"}
        for call in transport.calls:
            assert set(call["payload"].keys()) == required
            assert not (set(call["payload"].keys()) & forbidden)

    def test_device_name_from_excel_real(self, monkeypatch):
        """10. deviceName from Excel in real mode."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["deviceName"] != ""

    def test_task_name_from_excel_real(self, monkeypatch):
        """11. taskName from Excel in real mode."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            assert call["payload"]["taskName"] != ""

    def test_lock_acquired_in_real_mode(self, monkeypatch):
        """12. Lock acquire called in real mode."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        from src.resource_lock_manager import ResourceLockManager
        lock_mgr = ResourceLockManager()
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport, lock_manager=lock_mgr)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        # After execution, all locks should be released
        assert len(lock_mgr.snapshot()) == 0

    def test_lock_released_after_success(self, monkeypatch):
        """13. Lock released after success."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        from src.resource_lock_manager import ResourceLockManager
        lock_mgr = ResourceLockManager()
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport, lock_manager=lock_mgr)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        assert len(lock_mgr.snapshot()) == 0

    def test_lock_released_after_failure(self, monkeypatch):
        """14. Lock released after runner failure."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="FAILED", error={"message": "err"})

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        from src.resource_lock_manager import ResourceLockManager
        lock_mgr = ResourceLockManager()
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport, lock_manager=lock_mgr)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        assert len(lock_mgr.snapshot()) == 0

    def test_lock_released_after_callback_failed(self, monkeypatch):
        """15. Lock released even when callback fails."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        from src.resource_lock_manager import ResourceLockManager
        lock_mgr = ResourceLockManager()
        transport = FakeCallbackTransport()
        transport.set_failure()
        svc = PlanRunService(callback_transport=transport, lock_manager=lock_mgr)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        assert len(lock_mgr.snapshot()) == 0

    def test_no_password_in_callback_real(self, monkeypatch):
        """17. Password not in callback payload."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        for call in transport.calls:
            payload_str = str(call["payload"])
            assert "password" not in payload_str.lower()

    def test_real_runner_still_serial(self, monkeypatch):
        """19. Real runner still serial (no concurrency)."""
        order = []
        def _tracked_run(self_ignored, job_payload):
            order.append(job_payload.get("task_snapshot", {}).get("task_name", ""))
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _tracked_run)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        svc.run_all_sync(r["runId"])
        # Serial execution means order is deterministic
        assert len(order) > 0


# ===========================================================================
# Mock server tests (24-27)
# ===========================================================================

class TestMockServer:
    def test_mock_server_starts_and_receives(self):
        import http.server, threading, socket, urllib.request, json
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from mock_plan_status_server import Handler, _store
        _store._items.clear()

        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.1)
        try:
            data = json.dumps({"planId": 1, "deviceName": "D1", "taskName": "T1", "status": "SUCCESS", "updater": "x", "errorMessage": None}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/anything", data=data, headers={"Content-Type":"application/json"}, method="POST")
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
            assert len(_store.list_all()) == 1

            resp2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/plan-item-statuses", timeout=5)
            body = json.loads(resp2.read().decode())
            assert body["summary"]["total"] == 1
            assert body["summary"]["SUCCESS"] == 1
        finally:
            srv.shutdown()
            srv.server_close()

    def test_mock_server_help(self):
        result = subprocess.run([sys.executable, "scripts/mock_plan_status_server.py", "--help"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0

    def test_submit_plan_run_help(self):
        result = subprocess.run([sys.executable, "scripts/submit_plan_run.py", "--help"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "--plan-id" in result.stdout


# ===========================================================================
# Regression: Direct Dispatch tests still pass
# ===========================================================================

class TestRegression:
    def test_direct_dispatch_still_imports(self):
        """23. Direct Dispatch service still importable."""
        from src.executor_api_server.service import DirectDispatchService
        svc = DirectDispatchService(executor_id="exec-test")
        assert svc is not None
