"""
Tests for built-in debug callback receiver (方案 B — no Python mock server needed).

The debug callback receiver is integrated into the Executor API at:
  POST   /debug/plan-item-statuses
  GET    /debug/plan-item-statuses
  DELETE /debug/plan-item-statuses

It replaces the need for a separate mock_plan_status_server.py.
"""

from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from src.executor_api_server.app import create_app, _debug_callback_store, _debug_callback_lock
from src.executor_api_server.service import DirectDispatchService
from src.plan_run_service import PlanRunService


@pytest.fixture(autouse=True)
def clear_debug_store():
    """Clear the module-level debug callback store before each test."""
    with _debug_callback_lock:
        _debug_callback_store.clear()

EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")

CALLBACK_ITEM_FIELDS = {
    "planId", "deviceGroup", "deviceName", "taskName", "status",
    "updater", "errorMessage", "startedAt", "finishedAt",
}
CALLBACK_FORBIDDEN_FIELDS = {
    "job_id", "external_task_id", "executor_id", "duration_ms", "artifacts",
    "excelHash", "runId",
}


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def app():
    svc = DirectDispatchService(executor_id="test-debug-cb")
    svc.start_background_worker()
    prs = PlanRunService(use_http_callback=False)
    app = create_app(svc, plan_run_service=prs, debug_callback_receiver=True)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ===========================================================================
# Debug callback receiver tests
# ===========================================================================

class TestDebugCallbackReceiver:
    """Test the built-in debug callback receiver (not dependent on system Python)."""

    def test_debug_callback_get_empty(self, client):
        """GET /debug/plan-item-statuses returns empty when no callbacks received."""
        resp = client.get("/debug/plan-item-statuses")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total"] == 0
        assert data["summary"]["SUCCESS"] == 0
        assert data["summary"]["FAILED"] == 0
        assert data["items"] == []

    def test_debug_callback_post_receives_payload(self, client):
        """POST /debug/plan-item-statuses receives and stores a callback."""
        payload = {
            "planId": 1,
            "deviceName": "Switch-A",
            "taskName": "BMC Login",
            "status": "SUCCESS",
            "updater": "test-updater",
            "errorMessage": None,
        }
        resp = client.post("/debug/plan-item-statuses", json=payload)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0

        # Verify stored
        resp2 = client.get("/debug/plan-item-statuses")
        data = resp2.json()
        assert data["summary"]["total"] == 1
        assert data["summary"]["SUCCESS"] == 1
        assert data["items"][0]["payload"]["planId"] == 1
        assert data["items"][0]["payload"]["deviceName"] == "Switch-A"
        assert data["items"][0]["payload"]["taskName"] == "BMC Login"
        assert data["items"][0]["payload"]["status"] == "SUCCESS"
        assert data["items"][0]["payload"]["errorMessage"] is None

    def test_debug_callback_payload_public_fields(self, client):
        """POST payload stores only public item fields, no extra legacy fields."""
        payload = {
            "planId": 1,
            "deviceGroup": "A3",
            "deviceName": "Switch-A",
            "taskName": "BMC Login",
            "status": "SUCCESS",
            "updater": "test-updater",
            "errorMessage": None,
            "startedAt": None,
            "finishedAt": None,
        }
        resp = client.post("/debug/plan-item-statuses", json=payload)
        assert resp.status_code == 200

        resp2 = client.get("/debug/plan-item-statuses")
        data = resp2.json()
        stored_payload = data["items"][-1]["payload"]

        keys = set(stored_payload.keys())
        assert keys == CALLBACK_ITEM_FIELDS, f"Expected public item fields, got: {keys}"
        assert not (keys & CALLBACK_FORBIDDEN_FIELDS), f"Should not have legacy fields, got: {keys & CALLBACK_FORBIDDEN_FIELDS}"

    def test_debug_callback_post_failed_status(self, client):
        """POST with status=FAILED is correctly counted."""
        payload = {
            "planId": 1,
            "deviceName": "Switch-B",
            "taskName": "SSH Check",
            "status": "FAILED",
            "updater": "test",
            "errorMessage": "Connection timeout",
        }
        client.post("/debug/plan-item-statuses", json=payload)

        resp = client.get("/debug/plan-item-statuses")
        data = resp.json()
        assert data["summary"]["FAILED"] >= 1

    def test_debug_callback_clear(self, client):
        """DELETE /debug/plan-item-statuses clears all stored callbacks."""
        # Add a callback
        client.post("/debug/plan-item-statuses", json={
            "planId": 1, "deviceName": "D1", "taskName": "T1",
            "status": "SUCCESS", "updater": "t", "errorMessage": None,
        })

        # Verify it exists
        resp = client.get("/debug/plan-item-statuses")
        assert resp.json()["summary"]["total"] > 0

        # Clear
        resp = client.delete("/debug/plan-item-statuses")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "cleared" in resp.json()["message"]

        # Verify empty
        resp = client.get("/debug/plan-item-statuses")
        assert resp.json()["summary"]["total"] == 0

    def test_debug_callback_multiple_items(self, client):
        """POST multiple callbacks, GET returns all."""
        items = [
            {"planId": 1, "deviceName": f"D{i}", "taskName": f"T{j}",
             "status": "SUCCESS", "updater": "test", "errorMessage": None}
            for i in range(3) for j in range(3)
        ]
        for item in items:
            client.post("/debug/plan-item-statuses", json=item)

        resp = client.get("/debug/plan-item-statuses")
        data = resp.json()
        assert data["summary"]["total"] == 9
        assert data["summary"]["SUCCESS"] == 9


class TestDebugCallbackWithPlanRun:
    """Integration: PlanRunService sends callbacks to the debug receiver."""

    def test_plan_run_callbacks_via_debug_endpoint(self, client):
        """Plan run callbacks reach the debug callback store via a custom transport."""
        from src.executor_api_server.app import _debug_callback_lock

        # Custom transport that writes to the shared in-memory debug store
        class DebugStoreTransport:
            """Transport that writes callbacks directly to the debug store."""
            def __init__(self):
                self.calls = []

            def post(self, url, payload, headers):
                self.calls.append({"url": url, "payload": dict(payload)})
                # Handle batch payload: expand items into individual entries
                if "items" in payload:
                    for item in payload["items"]:
                        entry = {"receivedAt": time.time(), "type": "item", "payload": dict(item)}
                        with _debug_callback_lock:
                            _debug_callback_store.append(entry)
                elif "summary" in payload and "taskName" not in payload:
                    entry = {
                        "receivedAt": time.time(),
                        "type": "summary",
                        "payload": {"planId": payload.get("planId"), "summary": payload.get("summary", {})},
                    }
                    with _debug_callback_lock:
                        _debug_callback_store.append(entry)
                else:
                    entry = {"receivedAt": time.time(), "type": "item", "payload": dict(payload)}
                    with _debug_callback_lock:
                        _debug_callback_store.append(entry)
                return 200, '{"code":0,"message":"success","data":{"total":1,"success":1,"failed":0,"errors":[]}}'

        svc = DirectDispatchService(executor_id="test-plan-run-debug")
        svc.start_background_worker()
        prs = PlanRunService(callback_transport=DebugStoreTransport())
        app = create_app(svc, plan_run_service=prs, debug_callback_receiver=True)
        c = TestClient(app)

        # Set Excel
        resp = c.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

        # Start plan run — itemStatusUrl points to debug endpoint
        resp = c.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": "http://local/debug"},
            "updater": "test-plan-run",
            "runner": "fake",
        })
        assert resp.status_code == 200
        run_info = resp.json()
        assert run_info["accepted"] is True
        run_id = run_info["planId"]

        # Wait for completion
        time.sleep(3)

        # Check run status
        resp = c.get(f"/executor/v1/plans/{run_id}")
        assert resp.status_code == 200
        run_status = resp.json()
        assert run_status["status"] == "COMPLETED"
        total = run_status["summary"]["total"]
        success = run_status["summary"]["success"]
        failed = run_status["summary"]["failed"]

        assert total > 0, "Plan run should have >0 items"
        assert success == total, "All items should succeed with fake runner"
        assert failed == 0, "No items should fail with fake runner"

        # Check debug callback store via HTTP endpoint
        resp = c.get("/debug/plan-item-statuses")
        cb_data = resp.json()
        cb_total = cb_data["summary"]["total"]
        cb_success = cb_data["summary"]["SUCCESS"]
        cb_failed = cb_data["summary"]["FAILED"]

        assert cb_total >= total, f"Callback count ({cb_total}) should include status changes for run total ({total})"
        assert cb_success == total, f"Callback success count ({cb_success}) should match run total ({total})"
        assert cb_failed == 0, f"Callback failed count ({cb_failed}) should be 0"

        # Verify each item callback has only public fields
        for item in cb_data["items"]:
            if item.get("type") != "item":
                continue
            keys = set(item["payload"].keys())
            assert keys == CALLBACK_ITEM_FIELDS, f"Expected public item fields, got {keys}"
            assert not (keys & CALLBACK_FORBIDDEN_FIELDS), f"Has forbidden fields: {keys & CALLBACK_FORBIDDEN_FIELDS}"

    def test_debug_callback_not_available_without_flag(self):
        """Debug callback receiver NOT present when debug_callback_receiver=False."""
        svc = DirectDispatchService(executor_id="test-no-debug")
        svc.start_background_worker()
        prs = PlanRunService()
        app = create_app(svc, plan_run_service=prs, debug_callback_receiver=False)
        client = TestClient(app)

        resp = client.get("/debug/plan-item-statuses")
        assert resp.status_code == 404, "Should 404 when debug callback receiver not enabled"

    def test_debug_callback_preserves_received_timestamp(self, client):
        """Each callback entry has a receivedAt timestamp."""
        client.post("/debug/plan-item-statuses", json={
            "planId": 1, "deviceName": "D1", "taskName": "T1",
            "status": "SUCCESS", "updater": "t", "errorMessage": None,
        })
        resp = client.get("/debug/plan-item-statuses")
        data = resp.json()
        assert "receivedAt" in data["items"][0]
        assert data["items"][0]["receivedAt"] > 0

    def test_debug_callback_stores_only_public_fields_from_extra_payload(self, client):
        """Extra fields in POST body are dropped — only public item fields stored."""
        # POST with extra fields
        client.post("/debug/plan-item-statuses", json={
            "planId": 1,
            "deviceGroup": "A3",
            "deviceName": "D1",
            "taskName": "T1",
            "status": "SUCCESS",
            "updater": "t",
            "errorMessage": None,
            "startedAt": None,
            "finishedAt": None,
            "extraField": "should-not-exist",
            "job_id": "should-be-dropped",
            "duration_ms": 1234,
        })
        resp = client.get("/debug/plan-item-statuses")
        data = resp.json()
        stored = data["items"][0]["payload"]
        assert "extraField" not in stored, "extraField should have been dropped"
        assert "job_id" not in stored, "job_id should have been dropped"
        assert "duration_ms" not in stored, "duration_ms should have been dropped"
        keys = set(stored.keys())
        assert keys == CALLBACK_ITEM_FIELDS


# ===========================================================================
# Regression: server mode check
# ===========================================================================

class TestDefaultServerMode:
    """Default server mode must start Executor API, not legacy Network Boot API."""

    def test_default_server_mode_has_executor_routes(self, app):
        """Default create_app (without special flags) should have executor routes."""
        svc = DirectDispatchService(executor_id="test-default")
        svc.start_background_worker()
        prs = PlanRunService()
        app = create_app(svc, plan_run_service=prs)
        client = TestClient(app)

        # Must have Executor API routes
        resp = client.get("/routes")
        routes = resp.json()["routes"]
        route_paths = [r["path"] for r in routes]

        executor_routes = [
            "/executor/v1/status",
            "/executor/v1/config/excel:path",
            "/executor/v1/plans/{plan_id}:run",
            "/executor/v1/plans/{plan_id}",
        ]
        for route in executor_routes:
            assert route in route_paths, f"Executor route {route} must exist in default server mode"

        # Legacy compatible routes should also exist
        legacy_routes = ["/health", "/version", "/network/ping", "/routes"]
        for route in legacy_routes:
            assert route in route_paths, f"Legacy route {route} must exist for compatibility"

        # Debug callback should NOT be present by default
        assert "/debug/plan-item-statuses" not in route_paths, \
            "Debug callback should NOT be present unless explicitly enabled"


# ===========================================================================
# Regression: public callback payload fields
# ===========================================================================

class TestCallbackPayloadFields:
    """Verify that the public callback item contract is maintained via debug receiver."""

    def test_callback_payload_contains_all_public_fields(self):
        """Test that any POST to debug callback receiver stores all public fields correctly."""
        svc = DirectDispatchService(executor_id="test-6fields")
        svc.start_background_worker()
        prs = PlanRunService()
        app = create_app(svc, plan_run_service=prs, debug_callback_receiver=True)
        c = TestClient(app)

        # Clear any leftover state from other tests
        c.delete("/debug/plan-item-statuses")

        for status_val in ["SUCCESS", "FAILED", "RUNNING", "PENDING"]:
            payload = {
                "planId": 42,
                "deviceGroup": "A3",
                "deviceName": f"Device-{status_val}",
                "taskName": f"Task-{status_val}",
                "status": status_val,
                "updater": "test-updater",
                "errorMessage": "some error" if status_val == "FAILED" else None,
                "startedAt": None,
                "finishedAt": None,
            }
            resp = c.post("/debug/plan-item-statuses", json=payload)
            assert resp.status_code == 200

        resp = c.get("/debug/plan-item-statuses")
        data = resp.json()
        assert data["summary"]["total"] == 4
        assert data["summary"]["SUCCESS"] == 1
        assert data["summary"]["FAILED"] == 1
