"""
Tests for the external Plan API (excelHash + string planId model).

Service side uses excelHash + planId only — runId/jobId are internal.
"""
from __future__ import annotations
import os
import time
import pytest
from pathlib import Path

EXCEL_FILE = str(Path(__file__).resolve().parent / "fixtures" / "validation.json")


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def svc():
    from src.executor_api_server.service import DirectDispatchService
    s = DirectDispatchService(executor_id="test-ext-plan")
    s.start_background_worker()
    return s


@pytest.fixture
def prs():
    from src.plan_run_service import PlanRunService
    return PlanRunService()


@pytest.fixture
def client(svc, prs):
    from starlette.testclient import TestClient
    from src.executor_api_server.app import create_app
    app = create_app(svc, plan_run_service=prs, debug_callback_receiver=True)
    return TestClient(app)


# ===========================================================================
# Helpers
# ===========================================================================

def _set_excel(client):
    """Upload the test Excel and return the response."""
    xlsx = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
    if not xlsx.exists():
        pytest.skip(f"Test Excel not found: {xlsx}")
    with open(xlsx, "rb") as f:
        resp = client.post("/executor/v1/config/excel", files={"file": ("test.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 200
    return resp.json()


# ===========================================================================
# Excel hash
# ===========================================================================

class TestExcelHash:
    """POST /executor/v1/config/excel returns excelHash."""

    def test_upload_returns_excel_hash(self, client):
        data = _set_excel(client)
        assert "excelHash" in data, "excelHash missing from upload response"
        assert data["excelHash"] == data["sha256"], "excelHash must equal sha256"
        assert isinstance(data["excelHash"], str) and len(data["excelHash"]) == 64

    def test_latest_returns_excel_hash(self, client):
        _set_excel(client)
        resp = client.get("/executor/v1/config/latest")
        data = resp.json()
        assert data["hasLatest"] is True
        assert "excelHash" in data
        assert data["excelHash"] == data["sha256"]

    def test_no_latest_still_no_excel_hash(self, client):
        # Clear any previous state by creating a fresh PlanRunService
        from src.plan_run_service.service import _excel_store, _store_lock
        with _store_lock:
            _excel_store.clear()
        resp = client.get("/executor/v1/config/latest")
        data = resp.json()
        assert data == {"hasLatest": False}

    def test_excel_hash_not_driver_count(self, client):
        data = _set_excel(client)
        assert "deviceCount" in data
        assert "driverCount" not in str(data)


# ===========================================================================
# Start external plan
# ===========================================================================

class TestStartExternalPlan:
    """POST /executor/v1/plans."""

    def test_start_success(self, client):
        upload = _set_excel(client)
        excel_hash = upload["excelHash"]
        resp = client.post("/executor/v1/plans", json={
            "excelHash": excel_hash,
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["excelHash"] == excel_hash
        assert data["planId"].startswith("plan-")
        assert "runId" not in data, "External API must not expose runId"
        assert data["status"] == "ACCEPTED"

    def test_missing_excel_hash(self, client):
        resp = client.post("/executor/v1/plans", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data.get("errorMessage") == "MISSING_EXCEL_HASH"

    def test_excel_hash_mismatch(self, client):
        _set_excel(client)
        resp = client.post("/executor/v1/plans", json={
            "excelHash": "a" * 64,
            "callback": {"itemStatusUrl": "http://local/debug"},
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data.get("errorMessage") == "EXCEL_HASH_MISMATCH"

    def test_no_latest_excel(self, client):
        from src.plan_run_service.service import _excel_store, _store_lock
        with _store_lock:
            _excel_store.clear()
        resp = client.post("/executor/v1/plans", json={
            "excelHash": "a" * 64,
            "callback": {"itemStatusUrl": "http://local/debug"},
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data.get("errorMessage") == "NO_LATEST_EXCEL_CONFIG"

    def test_plan_id_unique_per_hash(self, client):
        upload = _set_excel(client)
        h = upload["excelHash"]
        ids = set()
        for _ in range(3):
            resp = client.post("/executor/v1/plans", json={
                "excelHash": h, "callback": {"itemStatusUrl": "http://local/debug"},
                "runner": "fake",
            })
            assert resp.status_code == 200
            ids.add(resp.json()["planId"])
        assert len(ids) == 3, "Each start must get a unique planId"

    def test_plan_id_not_expose_run_id(self, client):
        upload = _set_excel(client)
        resp = client.post("/executor/v1/plans", json={
            "excelHash": upload["excelHash"],
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        data = resp.json()
        assert "runId" not in data
        assert "jobId" not in data
        assert "job_id" not in str(data)


# ===========================================================================
# Query external plan
# ===========================================================================

class TestQueryExternalPlan:
    """GET /executor/v1/plans/{planId}?excelHash=..."""

    def _start_plan(self, client):
        upload = _set_excel(client)
        resp = client.post("/executor/v1/plans", json={
            "excelHash": upload["excelHash"],
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        return upload["excelHash"], resp.json()["planId"]

    def test_query_success(self, client):
        excel_hash, plan_id = self._start_plan(client)
        time.sleep(3)
        resp = client.get(f"/executor/v1/plans/{plan_id}?excelHash={excel_hash}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["excelHash"] == excel_hash
        assert data["planId"] == plan_id
        assert data["status"] in ("ACCEPTED", "RUNNING", "COMPLETED", "FAILED")
        assert "summary" in data
        assert data["summary"]["total"] > 0
        assert "runId" not in data
        assert "jobId" not in data
        assert "job_id" not in str(data)

    def test_plan_not_found(self, client):
        excel_hash, _ = self._start_plan(client)
        resp = client.get(f"/executor/v1/plans/nonexistent?excelHash={excel_hash}")
        assert resp.status_code == 404
        data = resp.json()
        assert "PLAN_NOT_FOUND" in str(data)

    def test_missing_excel_hash_param(self, client):
        _, plan_id = self._start_plan(client)
        resp = client.get(f"/executor/v1/plans/{plan_id}")
        assert resp.status_code == 400
        data = resp.json()
        assert "MISSING_EXCEL_HASH" in str(data)

    def test_excel_hash_mismatch(self, client):
        excel_hash, plan_id = self._start_plan(client)
        wrong_hash = "b" * 64
        resp = client.get(f"/executor/v1/plans/{plan_id}?excelHash={wrong_hash}")
        assert resp.status_code == 400
        data = resp.json()
        assert "PLAN_EXCEL_HASH_MISMATCH" in str(data)

    def test_summary_fields(self, client):
        excel_hash, plan_id = self._start_plan(client)
        time.sleep(3)
        resp = client.get(f"/executor/v1/plans/{plan_id}?excelHash={excel_hash}")
        data = resp.json()
        s = data["summary"]
        assert "total" in s
        assert "success" in s
        assert "failed" in s
        assert s["total"] >= 0
        assert s["total"] == s["success"] + s["failed"] + s["running"] + s["pending"]


# ===========================================================================
# Query external plan items
# ===========================================================================

class TestQueryExternalPlanItems:
    """GET /executor/v1/plans/{planId}/items?excelHash=..."""

    def _start_plan(self, client):
        upload = _set_excel(client)
        resp = client.post("/executor/v1/plans", json={
            "excelHash": upload["excelHash"],
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        return upload["excelHash"], resp.json()["planId"]

    def test_items_success(self, client):
        excel_hash, plan_id = self._start_plan(client)
        time.sleep(3)
        resp = client.get(f"/executor/v1/plans/{plan_id}/items?excelHash={excel_hash}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["excelHash"] == excel_hash
        assert data["planId"] == plan_id
        assert data["status"] == "COMPLETED"
        assert len(data["items"]) == data["summary"]["total"]
        assert "runId" not in data
        assert "jobId" not in data

    def test_items_status_enum(self, client):
        excel_hash, plan_id = self._start_plan(client)
        time.sleep(3)
        resp = client.get(f"/executor/v1/plans/{plan_id}/items?excelHash={excel_hash}")
        data = resp.json()
        for item in data["items"]:
            assert item["status"] in ("PENDING", "RUNNING", "SUCCESS", "FAILED"), f"Unexpected status: {item['status']}"
            assert "password" not in str(item).lower()
            assert "token" not in str(item).lower()

    def test_items_not_found(self, client):
        excel_hash, _ = self._start_plan(client)
        resp = client.get(f"/executor/v1/plans/nonexistent/items?excelHash={excel_hash}")
        assert resp.status_code == 404

    def test_items_hash_mismatch(self, client):
        excel_hash, plan_id = self._start_plan(client)
        resp = client.get(f"/executor/v1/plans/{plan_id}/items?excelHash={'c'*64}")
        assert resp.status_code == 400

    def test_items_optical_module(self, client):
        """4.1.15 optical module items are queryable."""
        excel_hash, plan_id = self._start_plan(client)
        time.sleep(3)
        resp = client.get(f"/executor/v1/plans/{plan_id}/items?excelHash={excel_hash}")
        data = resp.json()
        optical = [i for i in data["items"] if "光模块" in i["taskName"]]
        assert len(optical) >= 2, f"Expected >=2 optical items, got {len(optical)}"


# ===========================================================================
# Callback payload
# ===========================================================================

class TestExternalPlanCallback:
    """External plan callbacks include excelHash in payload."""

    def _make_callback_app(self, svc):
        from src.executor_api_server.app import _debug_callback_lock, _debug_callback_store, create_app
        from src.plan_run_service import PlanRunService
        from starlette.testclient import TestClient

        class DebugStoreTransport:
            def __init__(self):
                self.calls = []
            def post(self, url, payload, headers):
                self.calls.append(payload)
                entry = {"receivedAt": time.time(), "payload": dict(payload)}
                with _debug_callback_lock:
                    _debug_callback_store.append(entry)
                return 200, '{"ok":true}'

        prs2 = PlanRunService(callback_transport=DebugStoreTransport())
        app = create_app(svc, plan_run_service=prs2, debug_callback_receiver=True)
        return TestClient(app)

    def test_external_callback_has_excel_hash(self, svc):
        from src.executor_api_server.app import _debug_callback_store, _debug_callback_lock
        _debug_callback_store.clear()
        c = self._make_callback_app(svc)

        upload = _set_excel(c)
        excel_hash = upload["excelHash"]

        resp = c.post("/executor/v1/plans", json={
            "excelHash": excel_hash,
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        plan_id = resp.json()["planId"]
        time.sleep(3)

        with _debug_callback_lock:
            items = list(_debug_callback_store)

        plan_callbacks = [i for i in items if str(i["payload"].get("planId")) == plan_id]
        assert len(plan_callbacks) > 0, "No callbacks found for external plan"

        for cb in plan_callbacks:
            assert "excelHash" in cb["payload"], f"Callback missing excelHash: {cb['payload']}"
            assert cb["payload"]["excelHash"] == excel_hash
            assert cb["payload"]["status"] in ("SUCCESS", "FAILED")

    def test_callback_status_enum(self, svc):
        from src.executor_api_server.app import _debug_callback_store, _debug_callback_lock
        _debug_callback_store.clear()
        c = self._make_callback_app(svc)

        upload = _set_excel(c)
        resp = c.post("/executor/v1/plans", json={
            "excelHash": upload["excelHash"],
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        time.sleep(3)

        with _debug_callback_lock:
            items = list(_debug_callback_store)

        for cb in items:
            st = cb["payload"].get("status", "")
            assert st in ("SUCCESS", "FAILED"), f"Callback status must be SUCCESS/FAILED, got: {st}"


# ===========================================================================
# Regression: old API still works
# ===========================================================================

class TestRegression:
    """Old APIs still work alongside new external API."""

    def test_old_plan_run_still_works(self, client):
        _set_excel(client)
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert "runId" in data  # Old API still returns runId

    def test_direct_dispatch_still_works(self, client):
        payload = {
            "command_id": "cmd-ext-test-1",
            "command_type": "ASSIGN_JOB",
            "external_task_id": "task-ext-test-1",
            "callback": {"status_url": "http://localhost/cb", "auth_token": ""},
            "job": {
                "job_id": "job-ext-test-1",
                "resource_lock": {"lock_uri": "bmc://10.0.0.1"},
                "device_snapshot": {
                    "device_id": "dev-001", "device_name": "Test",
                    "device_group": "A3", "oob_ip": "10.0.0.1",
                    "oob_username": "admin", "oob_password_ref": "env:BMC_PASS",
                    "inband_ip": "", "inband_username": "", "inband_password_ref": "",
                },
                "task_snapshot": {
                    "task_id": "t1", "task_name": "Test",
                    "task_type": "BMC_URL", "execution_mode": "BMC_URL",
                    "url": "https://10.0.0.1/", "timeout_seconds": 60,
                },
            },
        }
        resp = client.post("/executor/v1/jobs", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
