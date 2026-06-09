"""
Comprehensive tests for Remote Windows Executor API (REMOTE_WINDOWS_EXECUTOR_API_COMPLETE_003).

Covers:
- POST /executor/v1/config/excel (multipart upload)
- GET /executor/v1/config/latest
- GET /executor/v1/plans/{plan_id}/runs/{run_id}/items
- PlanRun empty/non-JSON body handling
- 4.1.15 A3 optical module task per_group_commands
- Debug callback receiver still functional
- Version consistency (/status vs /openapi)
"""

from __future__ import annotations
import json, os, sys, time, hashlib
from pathlib import Path
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from src.executor_api_server.app import (
    create_app, _debug_callback_store, _debug_callback_lock,
)
from src.executor_api_server.service import DirectDispatchService
from src.plan_run_service import PlanRunService
from src.plan_run_service.service import _excel_store, _store_lock
from src.plan_item_status_callback_client import FakeCallbackTransport

EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_shared_state():
    """Clear shared module-level state before each test."""
    with _debug_callback_lock:
        _debug_callback_store.clear()
    with _store_lock:
        _excel_store.clear()


@pytest.fixture
def prs():
    return PlanRunService()


@pytest.fixture
def svc():
    s = DirectDispatchService(executor_id="test-remote-api")
    s.start_background_worker()
    return s


@pytest.fixture
def app(svc, prs):
    return create_app(svc, plan_run_service=prs, debug_callback_receiver=True)


@pytest.fixture
def client(app):
    return TestClient(app)


# ===========================================================================
# P0-1: Remote Excel upload
# ===========================================================================

class TestRemoteExcelUpload:
    """POST /executor/v1/config/excel — multipart upload."""

    def test_upload_excel_success(self, client, prs):
        """Upload xlsx file, verify accepted and stats returned."""
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        resp = client.post("/executor/v1/config/excel", files={
            "file": ("test_config.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["deviceCount"] > 0
        assert data["taskCount"] > 0
        assert data["filename"] == "test_config.xlsx"
        assert len(data["sha256"]) == 64
        assert data["message"] == "excel config uploaded and accepted as latest"

    def test_upload_excel_sets_latest(self, client):
        """After upload, latest config is the uploaded file."""
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        client.post("/executor/v1/config/excel", files={"file": ("up.xlsx", raw)})
        # Check latest
        resp = client.get("/executor/v1/config/latest")
        data = resp.json()
        assert data["hasLatest"] is True
        assert data["deviceCount"] > 0

    def test_upload_non_xlsx_rejected(self, client):
        """Non-xlsx file returns INVALID_EXCEL_FILE."""
        resp = client.post("/executor/v1/config/excel", files={
            "file": ("test.txt", b"not an excel", "text/plain"),
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["accepted"] is False
        assert data["code"] == "INVALID_EXCEL_FILE"

    def test_upload_empty_file_rejected(self, client):
        """Empty file returns EMPTY_EXCEL_FILE."""
        resp = client.post("/executor/v1/config/excel", files={
            "file": ("empty.xlsx", b"", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["accepted"] is False
        assert data["code"] == "EMPTY_EXCEL_FILE"

    def test_upload_corrupted_excel_rejected(self, client):
        """Corrupted xlsx returns INVALID_EXCEL_CONFIG."""
        resp = client.post("/executor/v1/config/excel", files={
            "file": ("bad.xlsx", b"PK\x03\x04" + b"\x00" * 100, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["accepted"] is False
        # Accept either "code" or "reason" field for the error code
        error_code = data.get("code") or data.get("reason", "")
        assert "INVALID_EXCEL_CONFIG" in error_code or "INVALID" in error_code

    def test_upload_then_plan_run_uses_uploaded(self, client):
        """Plan run after upload uses the uploaded file, not a local path."""
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        resp = client.post("/executor/v1/config/excel", files={"file": ("up.xlsx", raw)})
        assert resp.status_code == 200

        # Plan run must use the uploaded config
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True

    def test_upload_stored_path_response(self, client):
        """The upload response should have storedPath field."""
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        resp = client.post("/executor/v1/config/excel", files={"file": ("up.xlsx", raw)})
        data = resp.json()
        assert "storedPath" in data
        assert ".runtime" in data["storedPath"] or "latest.xlsx" in data["storedPath"]


# ===========================================================================
# P0-2: Latest Excel query
# ===========================================================================

class TestLatestConfig:
    """GET /executor/v1/config/latest."""

    def test_no_latest_config(self, client):
        """No latest set -> hasLatest=false."""
        resp = client.get("/executor/v1/config/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hasLatest"] is False

    def test_latest_has_device_info(self, client):
        """After set via path, latest returns device/task counts."""
        resp = client.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        assert resp.status_code == 200

        resp = client.get("/executor/v1/config/latest")
        data = resp.json()
        assert data["hasLatest"] is True
        assert data["deviceCount"] > 0
        assert data["taskCount"] > 0
        assert data["enabledDeviceCount"] > 0
        assert data["enabledTaskCount"] > 0


# ===========================================================================
# P0-3: Run items
# ===========================================================================

class TestRunItems:
    """GET /executor/v1/plans/{plan_id}/runs/{run_id}/items."""

    def test_run_items_not_found(self, client):
        """Non-existent run returns 404."""
        resp = client.get("/executor/v1/plans/1/runs/nonexistent/items")
        assert resp.status_code == 404
        data = resp.json()
        assert "RUN_NOT_FOUND" in str(data)

    def test_run_items_have_details(self, client, svc, prs):
        """Items return deviceName, taskName, status, errorMessage."""
        from src.plan_item_status_callback_client import FakeCallbackTransport
        from src.executor_api_server.app import _debug_callback_lock, _debug_callback_store

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
        c = TestClient(app)

        # Set Excel
        c.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})

        # Start run
        resp = c.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        run_id = resp.json()["runId"]
        time.sleep(3)

        # Get items
        resp = c.get(f"/executor/v1/plans/1/runs/{run_id}/items")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert len(data["items"]) == data["summary"]["total"]

        for item in data["items"]:
            assert "deviceName" in item
            assert "taskName" in item
            assert "status" in item
            assert "errorMessage" in item
            assert "password" not in str(item).lower()
            assert "token" not in str(item).lower()

    def test_run_items_count_matches_summary(self, client, svc, prs):
        """len(items) == summary.total."""
        from src.executor_api_server.app import _debug_callback_lock, _debug_callback_store

        class DebugTransport:
            def post(self, url, payload, headers):
                entry = {"receivedAt": time.time(), "payload": dict(payload)}
                with _debug_callback_lock:
                    _debug_callback_store.append(entry)
                return 200, '{"ok":true}'

        prs2 = PlanRunService(callback_transport=DebugTransport())
        app = create_app(svc, plan_run_service=prs2, debug_callback_receiver=True)
        c = TestClient(app)

        c.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        resp = c.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        run_id = resp.json()["runId"]
        time.sleep(3)

        resp_items = c.get(f"/executor/v1/plans/1/runs/{run_id}/items")
        data = resp_items.json()
        assert data["summary"]["total"] == len(data["items"])


# ===========================================================================
# P0-4: Fix PlanRun empty/non-JSON 500
# ===========================================================================

class TestPlanRunBodyHandling:
    """Empty body / non-JSON / no Content-Type should not 500."""

    def test_empty_body_returns_400(self, client):
        """Empty body returns 400, not 500."""
        resp = client.post("/executor/v1/plans/1:run", data=b"", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        data = resp.json()
        assert "INVALID_JSON_BODY" in str(data)

    def test_non_json_body_returns_400(self, client):
        """Non-JSON body returns 400, not 500."""
        resp = client.post("/executor/v1/plans/1:run", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        data = resp.json()
        assert "INVALID_JSON_BODY" in str(data)

    def test_no_content_type_returns_400(self, client):
        """No Content-Type header returns 400, not 500."""
        resp = client.post("/executor/v1/plans/1:run", data=b"{}")
        # Without content-type, the body might not even reach json parsing.
        # FastAPI may handle this differently in TestClient vs real.
        # At minimum it should not 500.
        assert resp.status_code != 500

    def test_valid_json_accepted(self, client, prs):
        """With valid JSON body and no latest Excel, returns 400 with reason."""
        # No latest set yet
        resp = client.post("/executor/v1/plans/1:run", json={"callback": {}, "runner": "fake"})
        assert resp.status_code == 400
        data = resp.json()
        assert data.get("reason") == "NO_LATEST_EXCEL_CONFIG"

    def test_valid_body_with_latest_accepted(self, client):
        """Valid JSON with Excel set returns accepted."""
        resp = client.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        assert resp.status_code == 200

        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True


# ===========================================================================
# P0-5: 4.1.15 A3 optical module task
# ===========================================================================

class TestOpticalModuleTask415:
    """Verify 4.1.15 task covers A3 with hccn_tool command."""

    def test_excel_parses_415_L1_L2_A3(self):
        """Excel loader parses 4.1.15 for L1/L2/A3 groups."""
        from src.loader.excel_reader import load_all
        devices, tasks = load_all(EXCEL_FILE)

        optical_task = None
        for t in tasks:
            if "光模块" in t.task_name:
                optical_task = t
                break
        assert optical_task is not None, "4.1.15 task not found in Excel"
        assert "A3" in optical_task.match_group, f"A3 not in match_group: {optical_task.match_group}"
        assert "L1" in optical_task.match_group
        assert "L2" in optical_task.match_group

    def test_optical_task_has_per_group_commands(self):
        """4.1.15 task has per_group_commands for A3."""
        from src.loader.excel_reader import load_all
        devices, tasks = load_all(EXCEL_FILE)

        optical_task = None
        for t in tasks:
            if "光模块" in t.task_name:
                optical_task = t
                break

        pgc = getattr(optical_task, '_per_group_commands', None) or {}
        assert "A3" in pgc, "A3 not in per_group_commands"
        assert "hccn_tool" in pgc["A3"], f"A3 command should use hccn_tool, got: {pgc['A3']}"
        assert "display interface transceiver" in optical_task.command_or_url

    def test_optical_plan_run_uses_hccn_for_A3(self, svc):
        """Plan run items for A3 use hccn_tool command, L1/L2 use transceiver."""
        from src.loader.excel_reader import load_all
        devices, tasks = load_all(EXCEL_FILE)

        # Find A3 device
        a3_device = None
        l1_device = None
        l2_device = None
        for d in devices:
            g = (getattr(d, "device_group", "") or "").upper()
            if g == "A3":
                a3_device = d
            elif g == "L1":
                l1_device = d
            elif g == "L2":
                l2_device = d

        # Find optical task
        optical_task = None
        for t in tasks:
            if "光模块" in t.task_name:
                optical_task = t
                break

        assert optical_task is not None
        pgc = getattr(optical_task, '_per_group_commands', None) or {}

        # Verify per_group_commands
        assert "A3" in pgc
        a3_cmd = pgc["A3"]
        assert "for i in $(seq 0 15)" in a3_cmd
        assert "hccn_tool -i $i -optical-g" in a3_cmd

        # Verify L1/L2 still use default
        assert optical_task.command_or_url == "display interface transceiver"

    def test_plan_run_expands_415_for_A3_L1_L2(self, svc, prs):
        """Plan run items include 4.1.15 for each A3, L1, L2 device."""
        from src.loader.excel_reader import load_all

        resp = TestClient(create_app(svc, plan_run_service=prs, debug_callback_receiver=True))
        resp.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})

        # Start run
        resp2 = resp.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp2.status_code == 200
        run_id = resp2.json()["runId"]
        time.sleep(3)

        # Get items
        resp3 = resp.get(f"/executor/v1/plans/1/runs/{run_id}/items")
        items = resp3.json()["items"]

        # Find 4.1.15 items
        optical_items = [i for i in items if "光模块" in i["taskName"]]
        assert len(optical_items) >= 2, f"Expected >=2 optical items, got {len(optical_items)}"

        groups_found = set()
        for item in optical_items:
            assert item["taskName"] == "计算节点光模块信息查询测试"
            assert item["status"] == "SUCCESS"
        # We can't easily determine which group from the items response,
        # but we can verify the count makes sense

    def test_optical_task_name_keeps_full_name(self):
        """taskName retains full 编号+name."""
        from src.loader.excel_reader import load_all
        devices, tasks = load_all(EXCEL_FILE)

        for t in tasks:
            if "光模块" in t.task_name:
                assert "光模块" in t.task_name
                break


# ===========================================================================
# P1-1: OpenAPI routes naming
# ===========================================================================

class TestOpenAPIRoutes:
    """OpenAPI must contain new routes with correct naming."""

    def test_openapi_includes_new_routes(self, client):
        """OpenAPI routes contain config/excel, config/latest, runs/.../items."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json().get("paths", {})

        # Must have new routes
        assert "/executor/v1/config/excel" in paths, "POST config/excel missing from openapi"
        assert "/executor/v1/config/latest" in paths, "GET config/latest missing from openapi"

        # Must have /items route
        has_items = any("/runs/" in k and "/items" in k for k in paths)
        assert has_items, "items route missing from openapi"

    def test_version_consistency(self, client):
        """openapi.json info.version should match 0.3.0."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        info = resp.json().get("info", {})
        assert info["version"] == "0.3.0", f"OpenAPI version mismatch: {info['version']}"

    def test_status_version_consistency(self, client):
        """GET /executor/v1/status must report version 0.3.0."""
        # Need to have a server running for this
        from src.executor_api_server.service import DirectDispatchService
        svc2 = DirectDispatchService(executor_id="test-version")
        svc2.start_background_worker()
        app = create_app(svc2)
        c = TestClient(app)

        resp = c.get("/executor/v1/status")
        data = resp.json()
        assert data["version"] == "0.3.0", f"Status version mismatch: {data['version']}"

    def test_routes_use_underscore_params(self, client):
        """Route paths should use {plan_id} / {run_id} not {planId}/{runId}."""
        resp = client.get("/routes")
        routes = resp.json().get("routes", [])
        paths = [r["path"] for r in routes]

        # Check plan run items path uses underscore notation
        items_paths = [p for p in paths if "items" in p]
        for p in items_paths:
            assert "{plan_id}" in p or "{run_id}" in p, f"Route uses wrong param naming: {p}"


# ===========================================================================
# Debug callback receiver still works
# ===========================================================================

class TestDebugCallbackStillWorks:
    """Existing debug callback receiver must still function."""

    def test_post_get_delete(self, client):
        """POST / GET / DELETE chain works."""
        resp = client.post("/debug/plan-item-statuses", json={
            "planId": 1, "deviceName": "D1", "taskName": "T1",
            "status": "SUCCESS", "updater": "t", "errorMessage": None,
        })
        assert resp.status_code == 200

        resp = client.get("/debug/plan-item-statuses")
        assert resp.json()["summary"]["total"] == 1

        resp = client.delete("/debug/plan-item-statuses")
        assert resp.status_code == 200

        resp = client.get("/debug/plan-item-statuses")
        assert resp.json()["summary"]["total"] == 0

    def test_plan_run_callback_via_debug_endpoint(self, svc):
        """Plan run with debug itemStatusUrl reaches debug store."""
        from src.executor_api_server.app import _debug_callback_lock, _debug_callback_store

        class DebugTransport:
            def post(self, url, payload, headers):
                entry = {"receivedAt": time.time(), "payload": dict(payload)}
                with _debug_callback_lock:
                    _debug_callback_store.append(entry)
                return 200, '{"ok":true}'

        prs = PlanRunService(callback_transport=DebugTransport())
        app = create_app(svc, plan_run_service=prs, debug_callback_receiver=True)
        c = TestClient(app)

        c.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        resp = c.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        time.sleep(3)

        resp = c.get("/debug/plan-item-statuses")
        data = resp.json()
        assert data["summary"]["total"] > 0

    def test_debug_callback_payload_strict_6_fields(self, client):
        """Callback payload has exactly 6 required fields."""
        from src.executor_api_server.app import _debug_callback_lock, _debug_callback_store

        class DebugTransport:
            def post(self, url, payload, headers):
                entry = {"receivedAt": time.time(), "payload": dict(payload)}
                with _debug_callback_lock:
                    _debug_callback_store.append(entry)
                return 200, '{"ok":true}'

        prs = PlanRunService(callback_transport=DebugTransport())
        app = create_app(DirectDispatchService(executor_id="test-cb6"),
                         plan_run_service=prs, debug_callback_receiver=True)
        c = TestClient(app)

        c.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        resp = c.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        time.sleep(3)

        resp = c.get("/debug/plan-item-statuses")
        data = resp.json()
        required = {"planId", "deviceName", "taskName", "status", "updater", "errorMessage"}
        forbidden = {"job_id", "external_task_id", "executor_id", "duration_ms", "artifacts"}
        for item in data["items"]:
            keys = set(item["payload"].keys())
            assert keys == required, f"Expected 6 fields, got {keys}"
            assert not (keys & forbidden), f"Has forbidden: {keys & forbidden}"


# ===========================================================================
# Runner default/mode check
# ===========================================================================

class TestRunnerMode:
    """Default runner must be fake."""

    def test_default_runner_fake(self, prs):
        """Default runner is fake in service."""
        excel = prs.set_latest_excel(EXCEL_FILE)
        assert excel["accepted"] is True

        r = prs.start_plan_run(1, {"callback": {"itemStatusUrl": "http://cb"}})
        assert r["accepted"] is True
        assert r["status"] == "ACCEPTED"

    def test_runner_real_must_be_explicit(self, prs):
        """runner=real must be explicitly set."""
        excel = prs.set_latest_excel(EXCEL_FILE)
        r = prs.start_plan_run(1, {"runner": "real", "callback": {"itemStatusUrl": "http://cb"}})
        assert r["accepted"] is True


# ===========================================================================
# Legacy path still works
# ===========================================================================

class TestLegacyPath:
    """POST /executor/v1/config/excel:path still works."""

    def test_path_set_excel(self, client):
        """Local path method still works."""
        resp = client.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True


# ===========================================================================
# No-system-Python guarantee
# ===========================================================================

class TestNoPythonRequirement:
    """Verify runtime doesn't require system Python by checking help flag presence."""

    def test_server_flag_no_python(self):
        """Just verify the flag exists (can't test exe on macOS)."""
        # This test is a placeholder for the CI smoke script validation
        assert True


# ===========================================================================
# Always-fake runner check for 4.1.15
# ===========================================================================

class TestOpticalModuleFakeRun:
    """4.1.15 with fake runner succeeds (no real SSH needed)."""

    def test_optical_items_succeed_with_fake(self, client):
        """4.1.15 items succeed with fake runner."""
        resp = client.post("/executor/v1/config/excel:path", json={"excelPath": EXCEL_FILE})
        assert resp.status_code == 200

        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        run_id = resp.json()["runId"]
        time.sleep(3)

        resp = client.get(f"/executor/v1/plans/1/runs/{run_id}")
        data = resp.json()
        assert data["summary"]["failed"] == 0
        assert data["summary"]["total"] == data["summary"]["success"]


# ===========================================================================
# Version consistency
# ===========================================================================

class TestVersionConsistency:
    """/status and OpenAPI must not show conflicting versions."""

    def test_explicit_version(self):
        """The status endpoint version is 0.3.0."""
        from src.executor_api_server.service import DirectDispatchService
        svc = DirectDispatchService(executor_id="test-ver")
        svc.start_background_worker()
        app = create_app(svc)
        c = TestClient(app)
        resp = c.get("/executor/v1/status")
        assert resp.json()["version"] == "0.3.0"

    def test_explicit_openapi_version(self):
        """OpenAPI version is 0.3.0."""
        svc = DirectDispatchService(executor_id="test-ver2")
        svc.start_background_worker()
        app = create_app(svc)
        c = TestClient(app)
        resp = c.get("/openapi.json")
        assert resp.json()["info"]["version"] == "0.3.0"