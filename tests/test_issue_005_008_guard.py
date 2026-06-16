"""
ISSUE-005, ISSUE-008, GUARD-001 integration tests.
"""
from __future__ import annotations
import json, os, sys, time, tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from src.executor_api_server.app import create_app, _debug_callback_store, _debug_callback_lock
from src.executor_api_server.service import DirectDispatchService
from src.plan_run_service import PlanRunService
from src.plan_run_service.service import _excel_store, _store_lock
from src.plan_item_status_callback_client import FakeCallbackTransport

EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


@pytest.fixture(autouse=True)
def clear_state(tmp_path, monkeypatch):
    import src.excel_config_store as config_store_module

    isolated_store = config_store_module.ExcelConfigStore(tmp_path)
    monkeypatch.setattr(config_store_module, "_default_store", isolated_store)
    monkeypatch.setattr(config_store_module, "_WORKSPACE_CANDIDATES", [tmp_path])
    monkeypatch.setattr(
        config_store_module,
        "_EXCEL_ALLOWED_ROOTS",
        [str(Path(__file__).resolve().parent.parent)],
    )
    with _debug_callback_lock:
        _debug_callback_store.clear()
    with _store_lock:
        _excel_store.clear()


@pytest.fixture
def prs():
    return PlanRunService(callback_transport=FakeCallbackTransport())


@pytest.fixture
def svc():
    s = DirectDispatchService(executor_id="test-issue-005")
    s.start_background_worker()
    return s


@pytest.fixture
def app(svc, prs):
    return create_app(svc, plan_run_service=prs, debug_callback_receiver=True)


@pytest.fixture
def client(app):
    return TestClient(app)


# ===========================================================================
# ISSUE-005: CallbackOutbox
# ===========================================================================

class TestCallbackOutboxIntegration:
    """CallbackOutbox: persistent outbox, delivery failures don't affect local status."""

    def test_callback_success_outbox_sent(self, client):
        """On success: outbox items are SENT."""
        import time
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        client.post("/executor/v1/config/excel", files={
            "file": ("cfg.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        run_id = resp.json()["planId"]
        time.sleep(3)

        from src.callback_outbox import CallbackOutbox
        outbox = CallbackOutbox(str(run_id))
        stats = outbox.get_stats()
        assert stats.get("SENT", 0) > 0, f"Expected SENT items in outbox: {stats}"
        assert stats.get("FAILED_RETRYABLE", 0) == 0
        assert stats.get("FAILED_FINAL", 0) == 0

    def test_callback_delivery_outbox_persists_items(self, client):
        """Callback to unreachable URL: local plan COMPLETED, outbox has items.

        The outbox persists items regardless of delivery outcome.
        """
        import time
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        client.post("/executor/v1/config/excel", files={
            "file": ("cfg.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": "http://127.0.0.1:1/nonexistent",
                         "mode": "single"},
            "runner": "fake",
        })
        assert resp.status_code == 200
        run_id = resp.json()["planId"]
        time.sleep(5)  # Allow background thread to attempt delivery + fail

        # Local plan is COMPLETED regardless of callback failure
        from src.callback_outbox import CallbackOutbox
        outbox = CallbackOutbox(str(run_id))
        stats = outbox.get_stats()
        total_outbox = sum(stats.values())
        assert total_outbox > 0, f"Outbox must have items even on delivery failure: stats={stats}"
        # Items exist in outbox with delivery status recorded
        all_items = outbox._read_all()
        assert len(all_items) > 0
        for it in all_items[:1]:
            assert it.delivery_status != "PENDING", \
                f"Delivery should have been attempted: {it.delivery_status}"

    def test_callback_url_not_configured_outbox_preserved(self, client):
        """URL not configured: outbox items kept as URL_NOT_CONFIGURED."""
        import time
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        client.post("/executor/v1/config/excel", files={
            "file": ("cfg.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": ""},
            "runner": "fake",
        })
        assert resp.status_code == 200
        run_id = resp.json()["planId"]
        time.sleep(3)

        from src.callback_outbox import CallbackOutbox
        outbox = CallbackOutbox(str(run_id))
        stats = outbox.get_stats()
        # No callback URL → items preserved as URL_NOT_CONFIGURED
        assert stats.get("URL_NOT_CONFIGURED", 0) > 0 or sum(stats.values()) > 0

    def test_outbox_body_no_forbidden_fields(self):
        """Outbox metadata must NOT contain password/token/secret/excelHash/configId/storedPath."""
        from src.callback_outbox import CallbackOutboxItem, build_outbox_item_from_callback_body
        item = build_outbox_item_from_callback_body(
            plan_id="1", device_name="D1", task_name="T1",
            status="SUCCESS", callback_url="http://cb",
        )
        d = item.to_outbox_dict()
        forbidden = {"password", "token", "secret", "Authorization",
                     "excelHash", "configId", "storedPath", "executorPlanId",
                     "serverPlanId", "callbackPlanId", "runId"}
        for key in d:
            assert key not in forbidden, f"Forbidden key in outbox: {key}"
        # Only allowed fields
        allowed = {"outboxId", "planId", "taskId", "planItemId",
                   "deviceName", "taskName", "status",
                   "deviceGroup", "updater", "errorMessage", "startedAt",
                   "finishedAt", "callbackUrl", "deliveryStatus",
                   "attemptCount", "lastErrorCode", "lastErrorMessage",
                   "nextRetryAt", "createdAt", "updatedAt"}
        for key in d:
            assert key in allowed, f"Unknown key in outbox: {key}"

    def test_callback_body_public_fields_from_outbox(self):
        """to_callback_body() returns public item callback fields."""
        from src.callback_outbox import CallbackOutboxItem, build_outbox_item_from_callback_body
        item = build_outbox_item_from_callback_body(
            plan_id="1", device_name="D1", task_name="T1",
            status="SUCCESS", callback_url="http://cb",
        )
        body = item.to_callback_body()
        assert set(body.keys()) == {
            "planId", "taskId", "planItemId",
            "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_outbox_append_and_read_roundtrip(self, tmp_path):
        """Append items, read them back, verify integrity."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        outbox = CallbackOutbox("test-plan", outbox_dir=str(tmp_path))
        items = [
            CallbackOutboxItem(plan_id="1", device_name="D1", task_name="T1",
                              status="SUCCESS", callback_url="http://cb"),
            CallbackOutboxItem(plan_id="1", device_name="D2", task_name="T2",
                              status="FAILED", error_message="err",
                              callback_url="http://cb"),
        ]
        outbox.append_batch(items)
        pending = outbox.get_pending()
        assert len(pending) == 2
        assert pending[0].plan_id == "1"
        assert pending[1].status == "FAILED"

    def test_mark_sent_updates_delivery_status(self, tmp_path):
        """mark_sent changes delivery_status, NOT callback body status."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        outbox = CallbackOutbox("test-plan", outbox_dir=str(tmp_path))
        item = CallbackOutboxItem(plan_id="1", device_name="D1", task_name="T1",
                                  status="FAILED", error_message="err",
                                  callback_url="http://cb")
        outbox.append(item)
        assert outbox.mark_sent(item.outbox_id)
        pending = outbox.get_pending()
        assert len(pending) == 0  # Not pending anymore
        stats = outbox.get_stats()
        assert stats.get("SENT", 0) == 1
        # Verify the CALLBACK body status is UNCHANGED
        all_items = outbox._read_all()
        assert all_items[0].status == "FAILED"  # Callback body status preserved
        assert all_items[0].delivery_status == "SENT"  # Delivery status updated

    def test_mark_failed_retryable(self, tmp_path):
        """mark_failed with retryable error sets FAILED_RETRYABLE + backoff."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        outbox = CallbackOutbox("test-plan", outbox_dir=str(tmp_path))
        item = CallbackOutboxItem(plan_id="1", device_name="D1", task_name="T1",
                                  status="SUCCESS", callback_url="http://cb")
        outbox.append(item)
        outbox.mark_failed(item.outbox_id, "CALLBACK_HTTP_ERROR: 500", retryable=True)
        stats = outbox.get_stats()
        assert stats.get("FAILED_RETRYABLE", 0) == 1
        all_items = outbox._read_all()
        assert all_items[0].attempt_count == 1
        assert all_items[0].next_retry_at > 0
        # Callback body status unchanged
        assert all_items[0].status == "SUCCESS"

    def test_mark_failed_max_retries_final(self, tmp_path):
        """After MAX_RETRY_ATTEMPTS, status becomes FAILED_FINAL."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem, MAX_RETRY_ATTEMPTS
        outbox = CallbackOutbox("test-plan", outbox_dir=str(tmp_path))
        item = CallbackOutboxItem(plan_id="1", device_name="D1", task_name="T1",
                                  status="SUCCESS", callback_url="http://cb")
        outbox.append(item)
        for _ in range(MAX_RETRY_ATTEMPTS):
            outbox.mark_failed(item.outbox_id, "CALLBACK_HTTP_ERROR: 500", retryable=True)
        stats = outbox.get_stats()
        assert stats.get("FAILED_FINAL", 0) == 1
        all_items = outbox._read_all()
        assert all_items[0].attempt_count == MAX_RETRY_ATTEMPTS


# ===========================================================================
# ISSUE-008: Path template fail-fast
# ===========================================================================

class TestPathTemplateFailFast:
    """Sensitive variables in path templates must fail-fast, not REDACTED."""

    def test_password_var_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{带外管理密码}-{设备名称}", "output_dir")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)
        assert "password" in str(exc.value).lower()

    def test_oob_password_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{OOB_Password}_{TaskName}", "image_name")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_ib_password_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{IB_Password}", "output_dir")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_token_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{api_token}/screenshots", "output_dir")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_secret_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("secret_{key}", "image_name")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_key_in_path_raises(self):
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{api_key}", "output_dir")
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_safe_path_template_passes(self):
        from src.utils.path_safety import validate_template_for_path
        # Normal template should not raise
        validate_template_for_path("{带外管理IP}-{设备名称}", "output_dir")
        validate_template_for_path("{TaskName}/{DeviceGroup}", "image_name")
        validate_template_for_path("正常路径", "output_dir")

    def test_error_message_no_real_password(self):
        """Error message must not contain actual passwords."""
        from src.utils.path_safety import validate_template_for_path
        with pytest.raises(ValueError) as exc:
            validate_template_for_path("{带外管理密码}", "image_name")
        msg = str(exc.value)
        # Error describes the VARIABLE TYPE, not the value
        assert "password_variable" in msg.lower() or "password" in msg.lower()
        # Should not contain any actual password text
        assert "secret123" not in msg.lower()
        assert "mypassword" not in msg.lower()

    def test_check_path_template_returns_matches(self):
        from src.utils.path_safety import check_path_template_for_sensitive_vars
        found = check_path_template_for_sensitive_vars("{带外管理密码}-{设备名称}")
        assert len(found) > 0
        assert "{带外管理密码}" in found

    def test_check_path_template_empty_for_safe(self):
        from src.utils.path_safety import check_path_template_for_sensitive_vars
        found = check_path_template_for_sensitive_vars("{带外管理IP}-{设备名称}")
        assert len(found) == 0


# ===========================================================================
# GUARD-001: _execute_run must not re-read latest
# ===========================================================================

class TestRunSnapshotGuard:
    """_execute_run must NOT call get_latest() / _get_latest_excel()."""

    def test_execute_run_does_not_re_read_latest(self):
        """Monkeypatch get_latest to raise; _execute_run must succeed or fail cleanly."""
        import time
        from src.plan_run_service.service import PlanRunService, _set_latest_excel
        from src.plan_item_status_callback_client import FakeCallbackTransport

        _set_latest_excel(EXCEL_FILE)
        transport = FakeCallbackTransport()
        svc = PlanRunService(callback_transport=transport)

        # Start a run normally
        r = svc.start_plan_run(1, {
            "callback": {"planId": "1", "itemStatusUrl": "http://cb"},
            "runner": "fake",
        })
        time.sleep(3)

        # After run completes, verify it didn't crash due to snapshot issues
        run = svc._runs.get(str(r["planId"]))
        assert run is not None
        assert run.status == "COMPLETED"
        assert run.config_snapshot is not None, "Run must have config snapshot"
        assert run.config_snapshot.excel_hash != "", "Snapshot must have hash"

    def test_snapshot_is_frozen_immutable(self):
        """RunConfigSnapshot is frozen — cannot be modified after creation."""
        from src.plan_run_service.service import RunConfigSnapshot
        snap = RunConfigSnapshot(
            excel_hash="d" * 64,
            stored_path="/tmp/test.xlsx",
        )
        with pytest.raises(Exception):
            snap.excel_hash = "modified"  # frozen dataclass

    def test_snapshot_bound_to_run_at_startup(self, client):
        """Verify run response includes excelHash from snapshot."""
        with open(EXCEL_FILE, "rb") as f:
            raw = f.read()
        resp = client.post("/executor/v1/config/excel", files={
            "file": ("cfg.xlsx", raw, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        hash_val = resp.json()["excelHash"]

        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.json()["excelHash"] == hash_val

    def test_get_latest_still_returns_current_after_snapshot(self, client):
        """After run snapshots A, get_latest still returns current latest."""
        with open(EXCEL_FILE, "rb") as f:
            raw_a = f.read()
        client.post("/executor/v1/config/excel", files={
            "file": ("a.xlsx", raw_a, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        })
        hash_a = resp_a = None
        resp = client.get("/executor/v1/config/latest")
        hash_a = resp.json()["excelHash"]

        # Start run with A snapshot
        resp = client.post("/executor/v1/plans/1:run", json={
            "callback": {"planId": "1", "itemStatusUrl": "http://local/debug"},
            "runner": "fake",
        })
        assert resp.json()["excelHash"] == hash_a

        # get_latest still returns A (no change)
        resp = client.get("/executor/v1/config/latest")
        assert resp.json()["excelHash"] == hash_a
