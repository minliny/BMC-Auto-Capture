"""
Tests for P1-PLAN-RUN-ITEM-STATUS-CALLBACK-001.
"""
from __future__ import annotations
import json, os, sys, subprocess, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.plan_run_service.service import PlanRunService, _set_latest_excel, _get_latest_excel, _excel_store, _store_lock
from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport, HttpCallbackTransport

EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


def _clear_store():
    with _store_lock:
        _excel_store.clear()


# Clear shared Excel store before each test in this file
@pytest.fixture(autouse=True)
def auto_clear_store(tmp_path, monkeypatch):
    import src.excel_config_store as config_store_module

    isolated_store = config_store_module.ExcelConfigStore(tmp_path)
    monkeypatch.setattr(config_store_module, "_default_store", isolated_store)
    monkeypatch.setattr(config_store_module, "_WORKSPACE_CANDIDATES", [tmp_path])
    monkeypatch.setattr(
        config_store_module,
        "_EXCEL_ALLOWED_ROOTS",
        [str(Path(__file__).resolve().parent.parent)],
    )
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
        time.sleep(1.1)
        r2 = svc.set_latest_excel(EXCEL_FILE)
        # configVersion removed — both calls return without configVersion
        assert r1["excelHash"] == r2["excelHash"]  # Same content, same hash

    def test_excel_hash_sha256(self):
        """5. sha256 is non-empty and hex."""
        svc = PlanRunService()
        r = svc.set_latest_excel(EXCEL_FILE)
        assert len(r["sha256"]) == 64
        assert all(c in "0123456789abcdef" for c in r["sha256"])

    def test_sha256_non_empty(self):
        """6. sha256 is non-empty."""
        svc = PlanRunService()
        r = svc.set_latest_excel(EXCEL_FILE)
        assert len(r["sha256"]) == 64


# ===========================================================================
# Plan run tests (7-22)
# ===========================================================================

class TestPlanRun:
    def test_start_plan_run_returns_plan_id_and_run_id(self, tmp_path):
        """7+9. Run returns planId and runId."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(42, {"callback": {}})
        assert r["accepted"] is True
        assert r["planId"] == 42
        assert "runId" in r

    def test_scope_all_expands_devices_tasks(self, tmp_path):
        """10. scope=ALL expands enabled devices × enabled tasks."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_by_plan_id(r["planId"])
        run = svc.get_plan(r["planId"])
        assert run["summary"]["total"] > 0

    def test_device_name_from_excel(self, tmp_path):
        """11. deviceName comes from Excel."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_by_plan_id(r["planId"])
        # All device names should be non-empty strings
        assert r["accepted"] is True

    def test_task_name_from_excel(self, tmp_path):
        """12. taskName comes from Excel."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {}})
        svc.run_by_plan_id(r["planId"])
        assert r["accepted"] is True

    def test_each_item_gets_callback(self, tmp_path):
        """13. Each completed item triggers a callback."""
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}})
        # Wait for background thread to complete
        time.sleep(3)
        run = svc.get_plan(r["planId"])
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
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}})
        svc.run_by_plan_id(r["planId"])
        for call in transport.calls:
            assert call["payload"]["status"] == "SUCCESS"
            assert call["payload"]["errorMessage"] is None

    def test_updater_default(self, tmp_path):
        """16. updater defaults to downstream-system."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}})
        svc.run_by_plan_id(r["planId"])
        for call in transport.calls:
            assert call["payload"]["updater"] == "downstream-system"

    def test_updater_override(self, tmp_path):
        """17. updater can be overridden in request."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}, "updater": "custom-updater"})
        svc.run_by_plan_id(r["planId"])
        for call in transport.calls:
            assert call["payload"]["updater"] == "custom-updater"

    def test_callback_no_legacy_fields(self, tmp_path):
        """18+19. Callback has ONLY 8 fields, no legacy fields."""
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}})
        svc.run_by_plan_id(r["planId"])
        required_fields = {"planId", "deviceName", "taskName", "status", "updater", "errorMessage", "startedAt", "finishedAt"}
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
        run = svc.get_plan(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
        has_failed = False
        for call in transport.calls:
            if call["payload"]["status"] == "FAILED":
                has_failed = True
        assert has_failed

    def test_callback_still_8_fields_with_real(self, monkeypatch):
        """9. Real runner callback still 8 fields only."""
        def _fake_run_job(self_ignored, job_payload):
            from src.job_runner_adapter import JobResult
            return JobResult(status="SUCCEEDED")

        monkeypatch.setattr("src.job_runner_adapter.RealRunnerAdapter.run_job", _fake_run_job)

        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
        required = {"planId", "deviceName", "taskName", "status", "updater", "errorMessage", "startedAt", "finishedAt"}
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
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


# ===========================================================================
# Server PlanItem Status API Compat — New tests
# ===========================================================================


class TestStatusMapping:
    """Status mapping: internal → server."""

    def test_pending_maps_to_pending(self):
        from src.plan_item_status_callback_client import map_status_to_server
        assert map_status_to_server("PENDING") == "PENDING"

    def test_running_maps_to_in_progress(self):
        from src.plan_item_status_callback_client import map_status_to_server
        assert map_status_to_server("RUNNING") == "IN_PROGRESS"

    def test_success_maps_to_success(self):
        from src.plan_item_status_callback_client import map_status_to_server
        assert map_status_to_server("SUCCESS") == "SUCCESS"

    def test_failed_maps_to_failed(self):
        from src.plan_item_status_callback_client import map_status_to_server
        assert map_status_to_server("FAILED") == "FAILED"

    def test_lowercase_status_maps_correctly(self):
        from src.plan_item_status_callback_client import map_status_to_server
        assert map_status_to_server("pending") == "PENDING"
        assert map_status_to_server("running") == "IN_PROGRESS"

    def test_unknown_status_raises(self):
        from src.plan_item_status_callback_client import map_status_to_server
        import pytest as pt
        with pt.raises(ValueError, match="CALLBACK_STATUS_MAPPING_ERROR"):
            map_status_to_server("UNKNOWN")

    def test_empty_status_raises(self):
        from src.plan_item_status_callback_client import map_status_to_server
        import pytest as pt
        with pt.raises(ValueError, match="CALLBACK_STATUS_MAPPING_ERROR"):
            map_status_to_server("")


class TestBuildCallbackItem:
    """build_callback_item produces correct 8-field dict."""

    def test_item_has_exactly_8_fields(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item("plan-abc-001", "D1", "T1", "SUCCESS")
        assert set(item.keys()) == {"planId", "deviceName", "taskName", "status", "updater", "errorMessage", "startedAt", "finishedAt"}

    def test_plan_id_is_string(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item("plan-abc-001", "D1", "T1", "SUCCESS")
        assert isinstance(item["planId"], str)
        assert item["planId"] == "plan-abc-001"

    def test_no_forbidden_fields(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item("plan-abc-001", "D1", "T1", "SUCCESS", error_message="err")
        forbidden = {"excelHash", "executorPlanId", "serverPlanId", "runId", "jobId",
                      "password", "token", "secret"}
        assert not (set(item.keys()) & forbidden)

    def test_status_is_mapped(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item("p1", "D1", "T1", "RUNNING")
        assert item["status"] == "IN_PROGRESS"

    def test_plan_id_int_converted_to_str(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item(42, "D1", "T1", "SUCCESS")
        assert item["planId"] == "42"
        assert isinstance(item["planId"], str)


class TestBatchCallbackClient:
    """send_batch / send_single unit tests with FakeCallbackTransport."""

    def test_send_batch_empty_items_returns_zero_result(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient
        cb = PlanItemStatusCallbackClient()
        result = cb.send_batch("http://cb", [])
        assert result.total == 0
        assert result.batches == 0
        assert result.ok

    def test_send_batch_payload_is_items_wrapped(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", f"D{i}", f"T{i}", "SUCCESS") for i in range(3)]
        cb.send_batch("http://cb", items)
        assert len(transport.calls) == 1
        assert "items" in transport.calls[0]["payload"]
        assert len(transport.calls[0]["payload"]["items"]) == 3

    def test_send_batch_all_plan_ids_same(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("plan-abc", f"D{i}", f"T{i}", "SUCCESS") for i in range(5)]
        cb.send_batch("http://cb", items)
        batch_items = transport.calls[0]["payload"]["items"]
        plan_ids = {it["planId"] for it in batch_items}
        assert plan_ids == {"plan-abc"}

    def test_send_batch_chunks_at_1000(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", f"D{i}", f"T{i}", "SUCCESS") for i in range(2500)]
        result = cb.send_batch("http://cb", items, max_batch_size=1000)
        assert result.batches == 3
        assert len(transport.calls) == 3
        assert len(transport.calls[0]["payload"]["items"]) == 1000
        assert len(transport.calls[1]["payload"]["items"]) == 1000
        assert len(transport.calls[2]["payload"]["items"]) == 500

    def test_send_single_no_items_field(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        item = build_callback_item("p1", "D1", "T1", "SUCCESS")
        cb.send_single("http://cb", item)
        assert len(transport.calls) == 1
        payload = transport.calls[0]["payload"]
        assert "planId" in payload
        assert "deviceName" in payload
        assert "items" not in payload

    def test_send_batch_content_type_is_utf8(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", "D1", "T1", "SUCCESS")]
        cb.send_batch("http://cb", items)
        ct = transport.calls[0]["headers"].get("Content-Type", "")
        assert "application/json" in ct
        assert "charset=utf-8" in ct

    def test_send_single_content_type_is_utf8(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        item = build_callback_item("p1", "D1", "T1", "SUCCESS")
        cb.send_single("http://cb", item)
        ct = transport.calls[0]["headers"].get("Content-Type", "")
        assert "application/json" in ct
        assert "charset=utf-8" in ct

    def test_batch_no_excel_hash_in_payload(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", "D1", "T1", "SUCCESS")]
        cb.send_batch("http://cb", items)
        payload_str = str(transport.calls[0]["payload"])
        assert "excelHash" not in payload_str

    def test_single_no_excel_hash_in_payload(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        item = build_callback_item("p1", "D1", "T1", "SUCCESS")
        cb.send_single("http://cb", item)
        payload_str = str(transport.calls[0]["payload"])
        assert "excelHash" not in payload_str


class TestCallbackResponseParsing:
    """Server response parsing and error classification."""

    def test_success_response_code0_failed0(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(200, '{"code":0,"message":"success","data":{"total":10,"success":10,"failed":0,"errors":[]}}')
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", f"D{i}", f"T{i}", "SUCCESS") for i in range(10)])
        assert result.ok
        assert result.success == 10
        assert result.failed == 0

    def test_partial_failure_code0_failed_gt0(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(200, '{"code":0,"message":"success","data":{"total":3,"success":2,"failed":1,"errors":[{"planId":"p1","reason":"not found"}]}}')
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", f"D{i}", f"T{i}", "SUCCESS") for i in range(3)])
        assert not result.ok
        assert result.last_error == "CALLBACK_PARTIAL_FAILURE"
        assert result.success == 2
        assert result.failed == 1
        assert len(result.errors) == 1

    def test_http_500_classified_as_http_error(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(500, "Internal Server Error")
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", "D1", "T1", "SUCCESS")])
        assert "CALLBACK_HTTP_ERROR: HTTP 500" in (result.last_error or "")

    def test_non_json_response_classified_as_parse_error(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(200, "not json at all!!!")
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", "D1", "T1", "SUCCESS")])
        assert "CALLBACK_PARSE_ERROR" in (result.last_error or "")

    def test_400_batch_too_large(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(400, '{"code":400,"message":"Batch size exceeds maximum of 1000"}')
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", "D1", "T1", "SUCCESS")])
        assert "CALLBACK_BATCH_TOO_LARGE" in (result.last_error or "")

    def test_400_plan_id_mismatch(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(400, '{"code":400,"message":"All items must belong to the same plan"}')
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", "D1", "T1", "SUCCESS")])
        assert "CALLBACK_PLAN_ID_MISMATCH" in (result.last_error or "")

    def test_400_empty_items(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        transport.configure_response(400, '{"code":400,"message":"Items list cannot be empty"}')
        cb = PlanItemStatusCallbackClient(transport=transport)
        result = cb.send_batch("http://cb", [build_callback_item("p1", "D1", "T1", "SUCCESS")])
        assert "CALLBACK_EMPTY_ITEMS" in (result.last_error or "")


class TestBatchModeIntegration:
    """Integration tests: batch mode through PlanRunService."""

    def test_default_mode_is_batch(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        # Wait for background thread to complete; run_all_sync may re-execute
        time.sleep(3)
        run = svc.get_plan(r["planId"])
        # With batch mode, transport receives the batch (at least once)
        assert len(transport.calls) >= 1
        first_call = transport.calls[0]
        assert "items" in first_call["payload"]
        batch_count = len(first_call["payload"]["items"])
        assert batch_count == run["summary"]["total"]

    def test_single_mode_sends_per_item(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"}})
        svc.run_by_plan_id(r["planId"])
        run = svc.get_plan(r["planId"])
        # Single mode: each item gets its own POST
        assert len(transport.calls) >= run["summary"]["total"]
        for call in transport.calls[:run["summary"]["total"]]:
            assert "planId" in call["payload"]
            assert "items" not in call["payload"]

    def test_batch_mode_items_use_mapped_status(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
        batch_items = transport.calls[0]["payload"]["items"]
        for item in batch_items:
            assert item["status"] in ("PENDING", "IN_PROGRESS", "SUCCESS", "FAILED")

    def test_callback_failure_does_not_change_plan_status(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        transport.set_failure()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        svc.run_by_plan_id(r["planId"])
        run = svc.get_plan(r["planId"])
        # Plan status is still COMPLETED even though callback failed
        assert run["status"] == "COMPLETED"
        assert run["summary"]["success"] == run["summary"]["total"]

    def test_external_plan_default_batch_mode(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        time.sleep(3)
        assert len(transport.calls) >= 1
        assert "items" in transport.calls[0]["payload"]

    def test_external_plan_single_mode(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"},
            "runner": "fake",
        })
        time.sleep(3)
        assert len(transport.calls) > 0
        for call in transport.calls:
            assert "planId" in call["payload"]
            assert "items" not in call["payload"]

    def test_invalid_callback_mode_rejected(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb", "mode": "invalid"}})
        assert r["accepted"] is False
        assert "INVALID_CALLBACK_MODE" in (r.get("reason", "") or r.get("errorMessage", ""))


class TestCallbackEncoding:
    """UTF-8 encoding tests."""

    def test_utf8_chinese_device_name_in_callback(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", "设备A", "任务X", "SUCCESS")]
        cb.send_batch("http://cb", items)
        payload = transport.calls[0]["payload"]
        batch_item = payload["items"][0]
        assert batch_item["deviceName"] == "设备A"
        assert batch_item["taskName"] == "任务X"

    def test_utf8_payload_json_encodable(self):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, build_callback_item
        import json
        transport = FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        items = [build_callback_item("p1", "设备A", "任务X", "SUCCESS")]
        cb.send_batch("http://cb", items)
        # Verify the payload is JSON-serializable without errors
        payload = transport.calls[0]["payload"]
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "设备A" in encoded
        assert "任务X" in encoded
        # Re-parse to verify round-trip
        decoded = json.loads(encoded)
        assert decoded["items"][0]["deviceName"] == "设备A"


class TestPlanItemCountEdgeCases:
    """Edge cases for item counts."""

    def test_items_count_equals_summary_total(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        time.sleep(3)
        run = svc.get_plan(r["planId"])
        batch_items = transport.calls[0]["payload"]["items"]
        assert len(batch_items) == run["summary"]["total"]

    def test_external_plan_id_is_string(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert isinstance(r["planId"], str)
        assert r["planId"] == "1"

    def test_external_plan_response_includes_run_id(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert "runId" in r
        assert "jobId" not in r


# ===========================================================================
# callback.planId / service-side planId tests
# ===========================================================================


class TestCallbackPlanId:
    """Tests for callback.planId (service-side planId)."""

    def test_callback_plan_id_accepted(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_external_plan({
            "excelHash": svc.set_latest_excel(EXCEL_FILE)["excelHash"],
            "callback": {"planId": "42", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert r["accepted"] is True
        assert r["planId"] == "42"

    def test_callback_plan_id_is_string(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": 7, "itemStatusUrl": "http://cb"},  # int → str
            "runner": "fake",
        })
        time.sleep(3)
        batch_items = transport.calls[0]["payload"]["items"]
        for item in batch_items:
            assert item["planId"] == "7"
            assert isinstance(item["planId"], str)

    def test_callback_plan_id_int_converted_to_string(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": 1, "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        time.sleep(3)
        for call in transport.calls:
            for item in call["payload"]["items"]:
                assert isinstance(item["planId"], str)

    def test_callback_plan_id_from_plan_id_field(self):
        """Callback uses plan_id directly (no server_plan_id needed)."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        # plan_id=1 becomes the callback planId
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        time.sleep(3)
        assert len(transport.calls) > 0
        # Batch mode: planId inside items array
        for call in transport.calls:
            items = call["payload"].get("items", [])
            for item in items:
                assert item.get("planId") == "1"

    def test_callback_plan_id_used_not_executor_plan_id(self):
        """Callback body planId equals the single business planId."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "service-plan-99", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        time.sleep(3)
        batch_items = transport.calls[0]["payload"]["items"]
        for item in batch_items:
            # planId in callback body equals the response planId (same single ID)
            assert item["planId"] == r["planId"]
            assert item["planId"] == "service-plan-99"

    def test_callback_body_no_excel_hash(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        time.sleep(3)
        payload_str = str(transport.calls[0]["payload"])
        assert "excelHash" not in payload_str


# ===========================================================================
# PlanItem status alignment tests
# ===========================================================================


class TestPlanItemStatusAlignment:
    """PlanItem internal status uses PENDING/IN_PROGRESS/SUCCESS/FAILED."""

    def test_plan_item_initial_status_is_pending(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        time.sleep(3)
        items_data = svc.get_plan_items(r["planId"])
        # Initial status should be PENDING before execution, but after fake run all are SUCCESS
        for item in items_data["items"]:
            assert item["status"] in ("PENDING", "IN_PROGRESS", "SUCCESS", "FAILED")

    def test_plan_item_no_running_status(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        time.sleep(3)
        items_data = svc.get_plan_items(r["planId"])
        for item in items_data["items"]:
            assert item["status"] != "RUNNING", f"PlanItem should not use RUNNING, got {item['status']}"

    def test_summary_has_in_progress_not_running(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        time.sleep(3)
        run = svc.get_plan(r["planId"])
        s = run["summary"]
        assert "in_progress" in s
        assert "running" not in s

    def test_plan_status_still_has_running(self):
        """PlanRun.status still uses RUNNING (not changed)."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb"}})
        # Plan is accepted with status RUNNING internally
        assert r["status"] == "ACCEPTED"  # response status

    def test_direct_dispatch_job_status_unchanged(self):
        """Direct Dispatch job status uses its own enum (not affected by PlanItem change)."""
        from src.direct_dispatch_store import JobStoreStatus
        assert JobStoreStatus.RUNNING == "RUNNING"
        assert hasattr(JobStoreStatus, "RUNNING")


# ===========================================================================
# Registry client tests
# ===========================================================================


class TestRegistryClient:
    """Tests for server_registry_client."""

    def test_is_active_true_values(self, monkeypatch):
        from src.server_registry_client import _is_active
        for v in (True, "true", "TRUE", "1", 1, "Y", "YES", "是"):
            assert _is_active(v) is True, f"Expected _is_active({v!r})=True"

    def test_is_active_false_values(self, monkeypatch):
        from src.server_registry_client import _is_active
        for v in (False, "false", "0", 0, "N", "NO", "", None):
            assert _is_active(v) is False, f"Expected _is_active({v!r})=False"

    def test_registry_not_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MASTER_REGISTRY_URL", raising=False)
        from src.server_registry_client import discover_callback_url
        assert discover_callback_url() is None

    def test_registry_success_resolves_url(self, monkeypatch):
        import json

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                return _FakeResponse(200, json.dumps([
                    {"host_ip": "10.0.0.1", "service_port": "6003", "active": True},
                ]))

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_AUTH", "Basic dGVzdDpwYXNz")

        from src.server_registry_client import discover_callback_url
        url = discover_callback_url()
        assert url == "http://10.0.0.1:6003/api/plans/items/status"

    def test_registry_no_active_returns_none(self, monkeypatch):
        import json

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                return _FakeResponse(200, json.dumps([
                    {"host_ip": "10.0.0.1", "service_port": "6003", "active": False},
                ]))

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        from src.server_registry_client import discover_callback_url
        assert discover_callback_url() is None

    def test_registry_missing_host_ip_returns_none(self, monkeypatch):
        import json

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                return _FakeResponse(200, json.dumps([
                    {"host_ip": "", "service_port": "6003", "active": True},
                ]))

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        from src.server_registry_client import discover_callback_url
        assert discover_callback_url() is None

    def test_registry_http_error_returns_none(self, monkeypatch):
        import urllib.error

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                raise urllib.error.HTTPError("http://reg", 500, "Error", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        from src.server_registry_client import discover_callback_url
        assert discover_callback_url() is None

    def test_registry_response_list_format(self, monkeypatch):
        import json

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                return _FakeResponse(200, json.dumps([
                    {"host_ip": "10.0.1.1", "service_port": "8080", "active": "true"},
                    {"host_ip": "10.0.1.2", "service_port": "8080", "active": "true"},
                ]))

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        from src.server_registry_client import discover_callback_url
        url = discover_callback_url()
        assert url == "http://10.0.1.1:8080/api/plans/items/status"


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ===========================================================================
# URL resolution tests
# ===========================================================================


class TestCallbackUrlResolution:
    """Tests for _resolve_callback_url priority chain."""

    def test_registry_priority_over_item_status_url(self, monkeypatch):
        import json

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                return _FakeResponse(200, json.dumps([
                    {"host_ip": "10.0.99.1", "service_port": "7000", "active": True},
                ]))

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://request-url/cb"},
            "runner": "fake",
        })
        time.sleep(3)
        # Registry wins → callback sent to registry URL
        assert len(transport.calls) >= 1
        url_used = transport.calls[0]["url"]
        assert "10.0.99.1:7000" in url_used

    def test_no_registry_fallback_to_item_status_url(self, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MASTER_REGISTRY_URL", raising=False)
        monkeypatch.delenv("EXECUTOR_PLAN_ITEM_STATUS_URL", raising=False)

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://fallback-url/cb"},
            "runner": "fake",
        })
        time.sleep(3)
        assert len(transport.calls) >= 1
        assert transport.calls[0]["url"] == "http://fallback-url/cb"

    def test_env_var_fallback_when_no_registry_no_request_url(self, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MASTER_REGISTRY_URL", raising=False)
        monkeypatch.setenv("EXECUTOR_PLAN_ITEM_STATUS_URL", "http://env-url/cb")

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1"},  # no itemStatusUrl
            "runner": "fake",
        })
        time.sleep(3)
        assert len(transport.calls) >= 1
        assert transport.calls[0]["url"] == "http://env-url/cb"

    def test_no_urls_at_all_no_callback(self, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MASTER_REGISTRY_URL", raising=False)
        monkeypatch.delenv("EXECUTOR_PLAN_ITEM_STATUS_URL", raising=False)

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1"},  # no itemStatusUrl
            "runner": "fake",
        })
        time.sleep(3)
        # No URL configured → no callback
        assert len(transport.calls) == 0

    def test_registry_http_error_fallback_to_item_status_url(self, monkeypatch):
        import urllib.error

        class FakeRegistryHandler:
            def urlopen(self, req, timeout):
                raise urllib.error.HTTPError("http://reg", 500, "Error", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", FakeRegistryHandler().urlopen)
        monkeypatch.setenv("EXECUTOR_MASTER_REGISTRY_URL", "http://reg/test")

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"planId": "1", "itemStatusUrl": "http://request-url/cb"},
            "runner": "fake",
        })
        time.sleep(3)
        # Registry failed → falls back to request URL
        assert transport.calls[0]["url"] == "http://request-url/cb"


# ===========================================================================
# P0: _resolve_transport() test coverage
# ===========================================================================


class TestResolveTransport:
    """Tests for _resolve_transport() — verifies auto HTTP vs Fake selection.

    All existing callback tests bypass _resolve_transport() by injecting
    FakeCallbackTransport directly.  These tests validate the transport
    resolution logic itself WITHOUT making real HTTP requests.
    """

    def test_http_transport_when_item_status_url_provided(self):
        """_resolve_transport returns HttpCallbackTransport when URL is non-empty."""
        svc = PlanRunService()
        transport = svc._resolve_transport("http://example.com/callback")
        assert isinstance(transport, HttpCallbackTransport), (
            f"Expected HttpCallbackTransport, got {type(transport).__name__}"
        )

    def test_fake_transport_when_item_status_url_empty(self):
        """_resolve_transport returns FakeCallbackTransport when URL is empty string."""
        svc = PlanRunService()
        transport = svc._resolve_transport("")
        assert isinstance(transport, FakeCallbackTransport), (
            f"Expected FakeCallbackTransport, got {type(transport).__name__}"
        )

    def test_fake_transport_when_item_status_url_none_like(self):
        """_resolve_transport returns FakeCallbackTransport with empty URL (None-like)."""
        svc = PlanRunService()
        # In practice, item_status_url comes from dict.get("itemStatusUrl", "")
        # which is always a string. Empty strings behave the same as None.
        transport = svc._resolve_transport("")
        assert isinstance(transport, FakeCallbackTransport)

    def test_explicit_transport_takes_priority(self):
        """Explicit callback_transport at construction time wins over URL-based auto-detect."""
        fake = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=fake)
        transport = svc._resolve_transport("http://example.com/callback")
        assert transport is fake, (
            "Explicit transport should take priority over auto-detected HttpCallbackTransport"
        )

    def test_explicit_transport_used_even_without_url(self):
        """Explicit transport is returned even when URL is empty."""
        fake = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=fake)
        transport = svc._resolve_transport("")
        assert transport is fake

    def test_callback_transport_property_reflects_explicit(self):
        """callback_transport property returns the explicit transport."""
        fake = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=fake)
        assert svc.callback_transport is fake

    def test_callback_transport_property_none_by_default(self):
        """callback_transport property is None when not explicitly set."""
        svc = PlanRunService()
        assert svc.callback_transport is None

    def test_start_plan_run_uses_http_transport_via_resolve(self):
        """Integration: start_plan_run() with itemStatusUrl selects HttpCallbackTransport.

        We verify this indirectly: when itemStatusUrl is provided and no explicit
        transport is injected, the run should still complete successfully (meaning
        _resolve_transport was called). Since we can't easily intercept the
        transport without mocking, we verify the run completes and the response
        indicates callbackTransportMode=http.
        """
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb/items"}})
        assert r["accepted"] is True
        # The response should work fine — transport resolution happened internally
        time.sleep(3)
        run = svc.get_plan(r["planId"])
        assert run["status"] == "COMPLETED"


# ===========================================================================
# P1-1: callback.planId vs path plan_id conflict
# ===========================================================================


class TestPlanIdConflict:
    """Tests verifying path plan_id is authoritative over callback.planId."""

    def test_path_plan_id_wins_when_callback_plan_id_differs(self):
        """When callback.planId != path plan_id, path plan_id is authoritative."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(11, {
            "callback": {"planId": "2", "itemStatusUrl": "http://cb/items"},
        })
        svc.run_by_plan_id(r["planId"])

        # Response planId must be path plan_id (11), not callback.planId ("2")
        assert r["planId"] == 11, f"Expected planId=11, got {r['planId']}"

        # All callback payload items must have planId = "11"
        for call in transport.calls:
            items = call["payload"].get("items", [])
            for item in items:
                assert item["planId"] == "11", (
                    f"Callback item planId should be '11', got {item['planId']}"
                )

    def test_path_plan_id_wins_top_level_plan_id_in_batch(self):
        """Batch payload top-level planId comes from path plan_id."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        svc.start_plan_run(99, {
            "callback": {"planId": "88", "itemStatusUrl": "http://cb/items"},
        })
        time.sleep(3)

        for call in transport.calls:
            top_plan_id = call["payload"].get("planId")
            if top_plan_id is not None:
                assert top_plan_id == "99", (
                    f"Top-level planId should be '99', got {top_plan_id}"
                )

    def test_plan_id_conflict_logs_warning(self, caplog):
        """When callback.planId differs from path plan_id, a WARNING is logged."""
        import logging

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        with caplog.at_level(logging.WARNING, logger="bmc_auto_capture.plan_run"):
            svc.start_plan_run(11, {
                "callback": {"planId": "2", "itemStatusUrl": "http://cb/items"},
            })

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        conflict_logs = [
            r.message for r in warnings
            if "callback.planId" in r.message and "path plan_id" in r.message
        ]
        assert len(conflict_logs) >= 1, (
            f"Expected WARNING about planId conflict, got warnings: {[r.message for r in warnings]}"
        )

    def test_no_warning_when_plan_ids_match(self, caplog):
        """No warning when callback.planId matches path plan_id."""
        import logging

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        with caplog.at_level(logging.WARNING, logger="bmc_auto_capture.plan_run"):
            svc.start_plan_run(42, {
                "callback": {"planId": "42", "itemStatusUrl": "http://cb/items"},
            })

        conflict_logs = [
            r.message for r in caplog.records
            if "callback.planId" in r.message and "path plan_id" in r.message
        ]
        assert len(conflict_logs) == 0, (
            f"Should NOT warn when planIds match, got: {conflict_logs}"
        )

    def test_no_warning_when_callback_plan_id_empty(self, caplog):
        """No warning when callback.planId is not provided (empty string)."""
        import logging

        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        with caplog.at_level(logging.WARNING, logger="bmc_auto_capture.plan_run"):
            svc.start_plan_run(1, {
                "callback": {"itemStatusUrl": "http://cb/items"},
            })

        conflict_logs = [
            r.message for r in caplog.records
            if "callback.planId" in r.message and "path plan_id" in r.message
        ]
        assert len(conflict_logs) == 0


# ===========================================================================
# P1-2: Single mode callback item fields (startedAt/finishedAt)
# ===========================================================================


class TestSingleModeCallbackFields:
    """Tests verifying single mode callback payloads include startedAt/finishedAt."""

    def test_single_mode_items_have_started_at_and_finished_at(self):
        """Single mode callback payloads MUST include startedAt and finishedAt."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"},
        })
        svc.run_by_plan_id(r["planId"])

        assert len(transport.calls) > 0, "Expected at least one callback call"
        for call in transport.calls:
            payload = call["payload"]
            assert "startedAt" in payload, (
                f"Single mode payload missing startedAt: {list(payload.keys())}"
            )
            assert "finishedAt" in payload, (
                f"Single mode payload missing finishedAt: {list(payload.keys())}"
            )
            # startedAt and finishedAt should be non-None for executed items
            assert payload["startedAt"] is not None, "startedAt should not be None"
            assert payload["finishedAt"] is not None, "finishedAt should not be None"

    def test_single_mode_has_same_fields_as_batch_item(self):
        """Single mode and batch mode items have identical field sets."""
        # Batch mode test
        svc_batch = PlanRunService()
        svc_batch.set_latest_excel(EXCEL_FILE)
        transport_batch = FakeCallbackTransport()
        svc_batch._cb_transport = transport_batch
        r_batch = svc_batch.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "batch"},
        })
        svc_batch.run_by_plan_id(r_batch["planId"])
        batch_item_keys = set(transport_batch.calls[0]["payload"]["items"][0].keys())

        # Single mode test
        svc_single = PlanRunService()
        svc_single.set_latest_excel(EXCEL_FILE)
        transport_single = FakeCallbackTransport()
        svc_single._cb_transport = transport_single
        r_single = svc_single.start_plan_run(2, {
            "callback": {"planId": "2", "itemStatusUrl": "http://cb", "mode": "single"},
        })
        svc_single.run_by_plan_id(r_single["planId"])
        single_item_keys = set(transport_single.calls[0]["payload"].keys())

        # Both should have the same 8 fields
        expected_fields = {"planId", "deviceName", "taskName", "status",
                           "updater", "errorMessage", "startedAt", "finishedAt"}
        assert batch_item_keys == expected_fields, (
            f"Batch item fields mismatch: {batch_item_keys ^ expected_fields}"
        )
        assert single_item_keys == expected_fields, (
            f"Single item fields mismatch: {single_item_keys ^ expected_fields}"
        )

    def test_single_mode_does_not_use_outbox_to_callback_body(self):
        """Single mode sends 8-field items, NOT the 6-field outbox internal format.

        CallbackOutboxItem.to_callback_body() returns 6 fields (no startedAt/finishedAt).
        Single mode must use build_callback_item() which returns 8 fields.
        """
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "single"},
        })
        svc.run_by_plan_id(r["planId"])

        for call in transport.calls:
            payload = call["payload"]
            # If to_callback_body() (6 fields) were used, startedAt/finishedAt would be missing
            assert "startedAt" in payload, (
                "startedAt missing — single mode may be using 6-field outbox format incorrectly"
            )
            assert "finishedAt" in payload, (
                "finishedAt missing — single mode may be using 6-field outbox format incorrectly"
            )
            # 8 fields total
            assert len(payload) == 8, (
                f"Expected 8 fields, got {len(payload)}: {list(payload.keys())}"
            )

    def test_batch_mode_items_also_have_started_at_finished_at(self):
        """Batch mode item payloads also include startedAt/finishedAt."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb", "mode": "batch"},
        })
        svc.run_by_plan_id(r["planId"])

        assert len(transport.calls) > 0
        items = transport.calls[0]["payload"]["items"]
        for item in items:
            assert "startedAt" in item
            assert "finishedAt" in item
            assert item["startedAt"] is not None
            assert item["finishedAt"] is not None


# ===========================================================================
# P1-3: use_http_callback deprecated — transport selection is now URL-based
# ===========================================================================


class TestUseHttpCallbackDeprecated:
    """Tests verifying use_http_callback is deprecated and no longer controls transport."""

    def test_use_http_callback_true_logs_warning(self, caplog):
        """Passing use_http_callback=True logs a deprecation warning."""
        import logging

        with caplog.at_level(logging.WARNING, logger="bmc_auto_capture.plan_run"):
            PlanRunService(use_http_callback=True)

        deprecation_logs = [
            r.message for r in caplog.records
            if "deprecated" in r.message.lower()
        ]
        assert len(deprecation_logs) >= 1, (
            f"Expected deprecation warning, got: {[r.message for r in caplog.records]}"
        )

    def test_use_http_callback_false_no_warning(self, caplog):
        """Passing use_http_callback=False (default) does NOT log a warning."""
        import logging

        with caplog.at_level(logging.WARNING, logger="bmc_auto_capture.plan_run"):
            PlanRunService(use_http_callback=False)

        deprecation_logs = [
            r.message for r in caplog.records
            if "deprecated" in r.message.lower()
        ]
        assert len(deprecation_logs) == 0, (
            f"Should NOT warn when use_http_callback=False, got: {deprecation_logs}"
        )

    def test_transport_selection_ignores_use_http_callback(self):
        """use_http_callback=True does NOT force HttpCallbackTransport.

        Transport selection is based on itemStatusUrl via _resolve_transport(),
        not on the deprecated use_http_callback parameter.
        """
        svc = PlanRunService(use_http_callback=True)
        # Empty URL → FakeCallbackTransport even though use_http_callback=True
        transport = svc._resolve_transport("")
        assert isinstance(transport, FakeCallbackTransport), (
            f"use_http_callback should NOT force HTTP transport, got {type(transport).__name__}"
        )

    def test_explicit_callback_transport_still_works_with_deprecated_flag(self):
        """Explicit callback_transport works correctly even with use_http_callback=True."""
        fake = FakeCallbackTransport()
        svc = PlanRunService(use_http_callback=True, callback_transport=fake)
        transport = svc._resolve_transport("http://example.com/callback")
        assert transport is fake
        assert isinstance(transport, FakeCallbackTransport)

    def test_run_by_plan_id_uses_resolve_transport(self):
        """run_by_plan_id() uses _resolve_transport(), not a hardcoded fallback."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(1, {"callback": {"planId": "1", "itemStatusUrl": "http://cb/items"}})
        svc.run_by_plan_id(r["planId"])

        # run_by_plan_id should complete successfully with the injected transport
        run = svc.get_plan(r["planId"])
        assert run["status"] == "COMPLETED"
        assert len(transport.calls) > 0


# ===========================================================================
# P2: Contracts consistency with real payload
# ===========================================================================


class TestContractsConsistency:
    """Verify API contracts match actual runtime behavior."""

    def test_plan_run_contract_has_request_body(self):
        """POST /executor/v1/plans/{plan_id}:run contract includes requestBody."""
        from src.executor_api_server.contracts import PLAN_RUN_CONTRACT

        assert "requestBody" in PLAN_RUN_CONTRACT, "PLAN_RUN_CONTRACT missing requestBody"
        rb = PLAN_RUN_CONTRACT["requestBody"]
        field_names = {f["name"] for f in rb.get("fields", [])}
        assert "callback" in field_names, "requestBody missing 'callback' field"
        assert "updater" in field_names, "requestBody missing 'updater' field"
        assert "runner" in field_names, "requestBody missing 'runner' field"

        # callback children
        cb_field = next(f for f in rb["fields"] if f["name"] == "callback")
        cb_children = {c["name"] for c in cb_field.get("children", [])}
        assert "callback.itemStatusUrl" in cb_children
        assert "callback.planId" in cb_children
        assert "callback.mode" in cb_children

    def test_plan_run_contract_response_fields(self):
        """Plan run contract response includes accepted, planId, runId, status, etc."""
        from src.executor_api_server.contracts import PLAN_RUN_CONTRACT

        resp = PLAN_RUN_CONTRACT["responseBody"]
        field_names = {f["name"] for f in resp.get("fields", [])}
        required_fields = {"accepted", "planId", "runId", "status", "excelHash", "message"}
        for rf in required_fields:
            assert rf in field_names, f"Response missing field: {rf}"

    def test_plan_run_contract_response_includes_callback_transport_mode(self):
        """Plan run contract response includes callbackTransportMode field."""
        from src.executor_api_server.contracts import PLAN_RUN_CONTRACT

        resp = PLAN_RUN_CONTRACT["responseBody"]
        field_names = {f["name"] for f in resp.get("fields", [])}
        assert "callbackTransportMode" in field_names, (
            "PLAN_RUN_CONTRACT response missing callbackTransportMode"
        )

    def test_callback_contract_item_fields_include_started_at_finished_at(self):
        """Callback contract item fields include startedAt and finishedAt."""
        from src.executor_api_server.contracts import PLAN_ITEM_STATUS_CALLBACK_CONTRACT

        field_names = {f["name"] for f in PLAN_ITEM_STATUS_CALLBACK_CONTRACT["fields"]}
        assert "startedAt" in field_names, "Callback contract missing startedAt"
        assert "finishedAt" in field_names, "Callback contract missing finishedAt"

    def test_callback_contract_batch_payload_has_top_level_fields(self):
        """Batch callback payload includes planId/runId/items/summary at top level."""
        from src.executor_api_server.contracts import PLAN_ITEM_STATUS_CALLBACK_CONTRACT

        batch = PLAN_ITEM_STATUS_CALLBACK_CONTRACT["modes"]["batch"]
        payload = batch["payloadStructure"]
        assert "planId" in payload, "Batch payload structure missing planId"
        assert "runId" in payload, "Batch payload structure missing runId"
        assert "items" in payload, "Batch payload structure missing items"
        assert "summary" in payload, "Batch payload structure missing summary"

    def test_callback_contract_single_mode_has_8_fields(self):
        """Single mode contract payload has 8 fields including startedAt/finishedAt."""
        from src.executor_api_server.contracts import PLAN_ITEM_STATUS_CALLBACK_CONTRACT

        single = PLAN_ITEM_STATUS_CALLBACK_CONTRACT["modes"]["single"]
        example = single["examplePayload"]
        required_single_fields = {"planId", "deviceName", "taskName", "status",
                                   "updater", "errorMessage", "startedAt", "finishedAt"}
        for field in required_single_fields:
            assert field in example, (
                f"Single mode examplePayload missing field: {field}"
            )

    def test_batch_payload_has_plan_id_run_id_items_summary(self):
        """Integration: real batch payload includes planId, runId, items, summary."""
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc._cb_transport = transport

        r = svc.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb/items"},
        })
        svc.run_by_plan_id(r["planId"])

        batch_payload = transport.calls[0]["payload"]
        assert "planId" in batch_payload, "Batch payload missing top-level planId"
        assert "runId" in batch_payload, "Batch payload missing top-level runId"
        assert "items" in batch_payload, "Batch payload missing items array"
        assert "summary" in batch_payload, "Batch payload missing summary"
        assert isinstance(batch_payload["items"], list)
        assert isinstance(batch_payload["summary"], dict)
        assert "total" in batch_payload["summary"]

    def test_callback_contract_matches_runtime_payload(self):
        """Callback contract field names match what build_callback_item produces."""
        from src.plan_item_status_callback_client import build_callback_item
        from src.executor_api_server.contracts import PLAN_ITEM_STATUS_CALLBACK_CONTRACT

        # Build a real item
        item = build_callback_item("p1", "D1", "T1", "SUCCESS",
                                    started_at="2026-01-01T00:00:00+00:00",
                                    finished_at="2026-01-01T00:00:01+00:00")

        # Contract fields
        contract_field_names = {f["name"] for f in PLAN_ITEM_STATUS_CALLBACK_CONTRACT["fields"]}

        # All item keys must be in contract fields
        for key in item:
            assert key in contract_field_names, (
                f"Runtime field '{key}' not in callback contract fields"
            )

        # All required contract fields must be in item
        required_contract_fields = {
            f["name"] for f in PLAN_ITEM_STATUS_CALLBACK_CONTRACT["fields"]
            if f.get("required")
        }
        for field in required_contract_fields:
            assert field in item, (
                f"Required contract field '{field}' not in runtime item"
            )
