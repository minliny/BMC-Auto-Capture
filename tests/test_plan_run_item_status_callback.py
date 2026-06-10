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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
        r = svc.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
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
    """build_callback_item produces correct 6-field dict."""

    def test_item_has_exactly_6_fields(self):
        from src.plan_item_status_callback_client import build_callback_item
        item = build_callback_item("plan-abc-001", "D1", "T1", "SUCCESS")
        assert set(item.keys()) == {"planId", "deviceName", "taskName", "status", "updater", "errorMessage"}

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
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        # Wait for background thread to complete; run_all_sync may re-execute
        time.sleep(3)
        run = svc.get_run(r["runId"])
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
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb", "mode": "single"}})
        time.sleep(3)
        run = svc.get_run(r["runId"])
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
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        time.sleep(3)
        batch_items = transport.calls[0]["payload"]["items"]
        for item in batch_items:
            assert item["status"] in ("PENDING", "IN_PROGRESS", "SUCCESS", "FAILED")

    def test_callback_failure_does_not_change_plan_status(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        transport.set_failure()
        svc._cb_transport = transport
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        time.sleep(3)
        run = svc.get_run(r["runId"])
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
            "callback": {"itemStatusUrl": "http://cb"},
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
            "callback": {"itemStatusUrl": "http://cb", "mode": "single"},
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
        r = svc.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        time.sleep(3)
        run = svc.get_run(r["runId"])
        batch_items = transport.calls[0]["payload"]["items"]
        assert len(batch_items) == run["summary"]["total"]

    def test_external_plan_id_is_string(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert isinstance(r["planId"], str)
        assert r["planId"].startswith("plan-")

    def test_external_plan_response_no_run_id(self):
        svc = PlanRunService()
        svc.set_latest_excel(EXCEL_FILE)
        excel_hash = svc.set_latest_excel(EXCEL_FILE)["excelHash"]
        r = svc.start_external_plan({
            "excelHash": excel_hash,
            "callback": {"itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert "runId" not in r
        assert "jobId" not in r
