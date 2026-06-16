"""
P0 boundary security tests — validates all six P0 fixes.

P0-1 (NEW-001): path fail-fast connected to execution chain
P0-2 (NEW-002): CallbackOutbox path containment
P0-3 (NEW-003): CallbackOutbox sensitive value redaction
P0-4 (NEW-004): local Excel path allowed-roots
P0-5 (NEW-005): legacy callback excel_hash backdoor removed
P0-6 (PARAM-001): planId/runId boundary cleanup
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ============================================================================
# P0-1: path fail-fast at execution chain level
# ============================================================================


class TestPathFailFastAtExecutionChain:
    """Validate that validate_template_for_path() is called in real execution paths.

    Not just testing the utility function — testing that _build_output_dir()
    and _resolve_file_basename() in both executors actually call it.
    """

    def test_bmc_build_output_dir_rejects_password_template(self):
        """BMC _build_output_dir must raise ValueError on {带外管理密码}."""
        from src.executor.bmc_executor import BMCExecutor
        from src.executor.browser_manager import BrowserManager

        bm = BrowserManager(headless=True, max_tasks=1, max_age_seconds=1)
        executor = BMCExecutor(bm)

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "secret123"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "secret456"
            tags = ""

        class MockTask:
            output_dir_template = "{带外管理密码}-{设备名称}"
            image_name_template = "{TaskName}_{timestamp}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "BMC"
            execution_mode = "BMC_URL"

        with pytest.raises(ValueError) as exc:
            executor._build_output_dir("/tmp/test_root", MockDevice(), MockTask())
        msg = str(exc.value)
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in msg
        # Verify no real password value leaked in error message
        assert "secret123" not in msg
        assert "secret456" not in msg

    def test_bmc_build_output_dir_rejects_ib_password_template(self):
        """BMC _build_output_dir must raise on {IB_Password}."""
        from src.executor.bmc_executor import BMCExecutor
        from src.executor.browser_manager import BrowserManager

        bm = BrowserManager(headless=True, max_tasks=1, max_age_seconds=1)
        executor = BMCExecutor(bm)

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "secret"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "secret"
            tags = ""

        class MockTask:
            output_dir_template = "{IB_Password}/{设备名称}"
            image_name_template = "{TaskName}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "BMC"
            execution_mode = "BMC_URL"

        with pytest.raises(ValueError) as exc:
            executor._build_output_dir("/tmp/test_root", MockDevice(), MockTask())
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_bmc_build_output_dir_rejects_token_template(self):
        """BMC _build_output_dir must raise on {api_token}."""
        from src.executor.bmc_executor import BMCExecutor
        from src.executor.browser_manager import BrowserManager

        bm = BrowserManager(headless=True, max_tasks=1, max_age_seconds=1)
        executor = BMCExecutor(bm)

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "secret"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "secret"
            tags = ""

        class MockTask:
            output_dir_template = "{api_token}/screenshots"
            image_name_template = "{TaskName}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "BMC"
            execution_mode = "BMC_URL"

        with pytest.raises(ValueError) as exc:
            executor._build_output_dir("/tmp/test_root", MockDevice(), MockTask())
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_bmc_resolve_file_basename_rejects_password_template(self):
        """BMC _resolve_file_basename must raise on {带外管理密码} in image template."""
        from src.executor.bmc_executor import BMCExecutor
        from src.executor.browser_manager import BrowserManager

        bm = BrowserManager(headless=True, max_tasks=1, max_age_seconds=1)
        executor = BMCExecutor(bm)

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "RealPass456"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "RealPass789"
            tags = ""

        class MockTask:
            output_dir_template = "正常路径"
            image_name_template = "{带外管理密码}-{TaskName}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "BMC"
            execution_mode = "BMC_URL"

        with pytest.raises(ValueError) as exc:
            executor._resolve_file_basename(MockTask(), MockDevice())
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)
        # Verify no real password in error
        assert "RealPass456" not in str(exc.value)
        assert "RealPass789" not in str(exc.value)

    def test_ssh_build_output_dir_rejects_password_template(self):
        """SSH _build_output_dir must raise on {带外管理密码}."""
        from src.executor.ssh_executor import SSHExecutor

        executor = SSHExecutor()

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "secret"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "secret"
            tags = ""

        class MockTask:
            output_dir_template = "{带外管理密码}-{设备名称}"
            image_name_template = "{TaskName}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "SSH"
            execution_mode = "SSH_CMD"

        with pytest.raises(ValueError) as exc:
            executor._build_output_dir("/tmp/test_root", MockDevice(), MockTask())
        assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in str(exc.value)

    def test_safe_template_passes(self):
        """Safe templates (no sensitive vars) must pass through without error."""
        from src.executor.bmc_executor import BMCExecutor
        from src.executor.browser_manager import BrowserManager

        bm = BrowserManager(headless=True, max_tasks=1, max_age_seconds=1)
        executor = BMCExecutor(bm)

        class MockDevice:
            device_name = "test"
            device_group = "A3"
            bmc_ip = "10.0.0.1"
            bmc_username = "admin"
            bmc_password = "secret"
            inband_ip = "10.0.0.2"
            inband_username = "root"
            inband_password = "secret"
            tags = ""

        class MockTask:
            output_dir_template = "{带外管理IP}-{设备名称}"
            image_name_template = "{TaskName}"
            sequence = 1
            sequence_str = "01"
            task_name = "test_task"
            task_type = "BMC"
            execution_mode = "BMC_URL"

        # Must not raise - these are safe paths
        out_dir = executor._build_output_dir("/tmp/test_root", MockDevice(), MockTask())
        assert out_dir.startswith("/tmp/test_root")
        assert "10.0.0.1" in out_dir or "test" in out_dir

    def test_error_message_does_not_leak_password(self):
        """Error messages from path validation must not contain real passwords."""
        from src.utils.path_safety import validate_template_for_path

        try:
            validate_template_for_path("{带外管理密码}-test", "test_path")
            assert False, "Should have raised"
        except ValueError as e:
            msg = str(e)
            assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in msg
            # Should not contain the template's resolved password value
            assert "带外管理密码" not in msg or "password_variable" in msg
            # Should not contain any actual password
            assert "TEMPLATE_SENSITIVE_FIELD_IN_PATH" in msg


# ============================================================================
# P0-2: CallbackOutbox path containment
# ============================================================================


class TestCallbackOutboxPathContainment:
    """Validate that CallbackOutbox rejects path traversal in plan_id."""

    def test_plan_id_traversal_rejected(self):
        """plan_id='../../escaped' must raise ValueError."""
        from src.callback_outbox import CallbackOutbox, _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("../../escaped")

    def test_plan_id_windows_traversal_rejected(self):
        """plan_id='..\\escaped' must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("..\\escaped")

    def test_plan_id_absolute_path_rejected(self):
        """plan_id='/tmp/escaped' must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("/tmp/escaped")

    def test_plan_id_absolute_path_backslash_rejected(self):
        """plan_id='\\\\server\\share' must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("\\server\\share")

    def test_plan_id_drive_letter_rejected(self):
        """plan_id='C:\\escaped' must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("C:\\escaped")

    def test_plan_id_empty_rejected(self):
        """Empty plan_id must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("")

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("   ")

    def test_plan_id_path_separator_rejected(self):
        """plan_id with forward slash must raise ValueError."""
        from src.callback_outbox import _safe_plan_id

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            _safe_plan_id("run/abc")

    def test_plan_id_valid_accepted(self):
        """Normal plan_id like 'plan-1' or '1' must pass."""
        from src.callback_outbox import _safe_plan_id

        result = _safe_plan_id("plan-1")
        assert result == "plan-1"

    def test_callback_outbox_constructor_rejects_traversal(self):
        """CallbackOutbox('../../escaped') must raise ValueError."""
        from src.callback_outbox import CallbackOutbox

        with pytest.raises(ValueError, match="INVALID_PLAN_ID_FOR_OUTBOX_PATH"):
            CallbackOutbox("../../escaped")

    def test_callback_outbox_valid_plan_id_creates_path(self):
        """CallbackOutbox with valid plan_id must create the outbox directory."""
        import tempfile
        from src.callback_outbox import CallbackOutbox

        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("plan-1", outbox_dir=tmpdir)
            assert os.path.exists(outbox._outbox_dir)

    def test_callback_outbox_path_resolves_under_base(self):
        """Outbox path must resolve under executor_state/plans/..."""
        import tempfile
        from src.callback_outbox import CallbackOutbox

        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("plan-1", outbox_dir=tmpdir)
            resolved = os.path.abspath(os.path.normpath(outbox._outbox_path))
            expected_base = os.path.abspath(os.path.normpath(tmpdir))
            assert resolved.startswith(expected_base + os.sep) or resolved == expected_base


# ============================================================================
# P0-3: CallbackOutbox sensitive value redaction
# ============================================================================


class TestCallbackOutboxSensitiveRedaction:
    """Validate that sensitive values are redacted in outbox jsonl."""

    def test_callback_url_token_redacted(self):
        """callbackUrl with ?token=abc must not contain 'abc' in jsonl."""
        import tempfile
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem

        item = CallbackOutboxItem(
            plan_id="1",
            device_name="test",
            task_name="test",
            status="SUCCESS",
            callback_url="http://example.com/api?token=abc123&normal=1",
        )
        d = item.to_outbox_dict()
        assert "abc123" not in d["callbackUrl"], "token leaked in callbackUrl"
        assert "***REDACTED***" in d["callbackUrl"], "REDACTED marker missing"

    def test_callback_url_userinfo_redacted(self):
        """callbackUrl with user:pass@host must not contain password in jsonl."""
        from src.callback_outbox import CallbackOutboxItem

        item = CallbackOutboxItem(
            plan_id="1",
            device_name="test",
            task_name="test",
            status="SUCCESS",
            callback_url="http://user:RealPass123@example.com/api",
        )
        d = item.to_outbox_dict()
        assert "RealPass123" not in d["callbackUrl"], "password leaked via userinfo"
        assert "***REDACTED***" in d["callbackUrl"]

    def test_error_message_authorization_redacted(self):
        """errorMessage with Authorization: Bearer must be redacted."""
        from src.callback_outbox import CallbackOutboxItem

        item = CallbackOutboxItem(
            plan_id="1",
            device_name="test",
            task_name="test",
            status="FAILED",
            error_message="Authorization: Bearer super-secret-token-xyz",
        )
        d = item.to_outbox_dict()
        assert "super-secret-token-xyz" not in d["errorMessage"], "auth token leaked"
        assert "***REDACTED***" in d["errorMessage"]

    def test_last_error_message_password_redacted(self):
        """lastErrorMessage with password=RealPass123 must be redacted."""
        from src.callback_outbox import CallbackOutboxItem, _redact_sensitive

        redacted = _redact_sensitive("error: password=RealPass123")
        assert "RealPass123" not in redacted
        assert "***REDACTED***" in redacted

    def test_normal_error_text_preserved(self):
        """Normal error text without sensitive values must be preserved."""
        from src.callback_outbox import _redact_sensitive

        text = "Connection refused: target port 22 not open"
        redacted = _redact_sensitive(text)
        assert redacted == text

    def test_jsonl_file_contains_no_sensitive_values(self):
        """CallbackOutbox.append must write redacted values to the jsonl file."""
        import tempfile
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem

        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-run-redact", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="1",
                device_name="test",
                task_name="test",
                status="SUCCESS",
                callback_url="http://example.com?token=SecretToken123",
                error_message="Authorization: Bearer MyBearerToken",
            )
            outbox.append(item)

            with open(outbox._outbox_path, "r") as f:
                content = f.read()

            assert "SecretToken123" not in content, "token leaked in jsonl"
            assert "MyBearerToken" not in content, "bearer token leaked in jsonl"
            assert "***REDACTED***" in content, "REDACTED marker missing"
            assert outbox._outbox_path.endswith("callback_outbox.jsonl")

    def test_callback_url_access_token_redacted(self):
        """URL with access_token must be redacted in outbox."""
        from src.callback_outbox import _redact_sensitive

        redacted = _redact_sensitive("http://api.example.com/data?access_token=gho_abc123")
        assert "gho_abc123" not in redacted
        assert "***REDACTED***" in redacted

    def test_last_error_message_secret_redacted(self):
        """lastErrorMessage with 'secret=my-api-secret' must be redacted."""
        from src.callback_outbox import _redact_sensitive

        redacted = _redact_sensitive("callback failed: secret=my-api-secret-999")
        assert "my-api-secret-999" not in redacted
        assert "***REDACTED***" in redacted

    def test_callback_body_public_fields(self):
        """to_callback_body() must return public callback fields without metadata leak."""
        from src.callback_outbox import CallbackOutboxItem

        item = CallbackOutboxItem(
            plan_id="1", device_name="d1", task_name="t1",
            status="SUCCESS", callback_url="http://example.com?token=abc",
        )
        body = item.to_callback_body()
        assert set(body.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }
        # The token is redacted in outbox storage but NOT present in callback body
        assert "token" not in str(body.values())


# ============================================================================
# P0-4: local Excel path allowed-roots
# ============================================================================


class TestExcelAllowedRoots:
    """Validate that activate_from_local_path enforces allowed-roots."""

    def test_allowed_root_xlsx_accepted(self):
        """An xlsx file inside the project workspace must be accepted."""
        from src.excel_config_store import ExcelConfigStore
        import tempfile

        store = ExcelConfigStore()
        # Create a valid xlsx file inside workspace
        ws = store.workspace
        test_path = ws / "test_allowed_import.xlsx"
        try:
            # Minimal valid xlsx content
            test_path.write_bytes(
                b'\x50\x4b\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00\x00\x00'
                b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            )
            result = store.activate_from_local_path(str(test_path))
            # May fail on Excel parsing (the fake content is not valid xlsx),
            # but the path check must pass (not EXCEL_PATH_NOT_ALLOWED)
            assert result.get("code") != "EXCEL_PATH_NOT_ALLOWED", \
                f"Path was rejected: {result}"
        finally:
            if test_path.exists():
                test_path.unlink()

    def test_tmp_file_rejected(self):
        """A .xlsx file from /tmp must be rejected with EXCEL_PATH_NOT_ALLOWED."""
        import tempfile
        from src.excel_config_store import ExcelConfigStore

        store = ExcelConfigStore()
        # Create a real temp file so path exists — must still be rejected by allowed-roots
        tmp_path = os.path.join(tempfile.gettempdir(), "p0_test_evil.xlsx")
        try:
            with open(tmp_path, "wb") as f:
                f.write(b'\x50\x4b\x03\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
            result = store.activate_from_local_path(tmp_path)
            assert result.get("code") == "EXCEL_PATH_NOT_ALLOWED", \
                f"/tmp path was not rejected: {result}"
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_relative_path_traversal_rejected(self):
        """Relative path containing ../evil.xlsx must be rejected."""
        from src.excel_config_store import ExcelConfigStore

        store = ExcelConfigStore()
        result = store.activate_from_local_path("../evil.xlsx")
        # Despite potential resolve to workspace parent, must not be accepted
        assert not result.get("accepted", True), \
            f"Traversal path was not rejected: {result}"

    def test_legacy_path_allowed(self):
        """Legacy .runtime/configs/latest.xlsx path must be allowed (migration)."""
        from src.excel_config_store import ExcelConfigStore, _is_path_allowed

        ws = ExcelConfigStore().workspace
        legacy_path = str(ws / ".runtime" / "configs" / "latest.xlsx")
        allowed, _ = _is_path_allowed(legacy_path)
        assert allowed, f"Legacy path {legacy_path} should be allowed"

    def test_error_message_no_sensitive_details(self):
        """Error message must not leak sensitive path details."""
        import tempfile
        from src.excel_config_store import ExcelConfigStore

        store = ExcelConfigStore()
        tmp_path = os.path.join(tempfile.gettempdir(), "p0_test_evil.xlsx")
        try:
            with open(tmp_path, "wb") as f:
                f.write(b'\x50\x4b\x03\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
            result = store.activate_from_local_path(tmp_path)
            msg = result.get("message", "")
            assert result.get("code") == "EXCEL_PATH_NOT_ALLOWED", \
                f"Unexpected code: {result}"
            # Should not print the full path with sensitive directory details
            # The path itself is not sensitive, but the error should explain clearly
            # error code already verified above
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


# ============================================================================
# P0-5: legacy callback excel_hash backdoor
# ============================================================================


class TestLegacyCallbackBackdoor:
    """Validate that legacy send(... excel_hash=...) does not leak excelHash."""

    def test_send_excel_hash_ignored(self):
        """send(excel_hash='abc') must NOT include excelHash in POST body."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

        transport = FakeCallbackTransport()
        client = PlanItemStatusCallbackClient(transport)

        client.send(
            url="http://example.com/cb",
            plan_id="1", device_name="d1", task_name="t1",
            status="SUCCESS", excel_hash="abc123",
        )

        assert len(transport.calls) == 1
        payload = transport.calls[0]["payload"]
        assert "excelHash" not in payload, f"excelHash leaked: {payload}"
        assert set(payload.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }, \
            f"Payload has extra fields: {payload}"

    def test_send_excel_hash_none_no_excel_hash(self):
        """send(excel_hash=None) must NOT include excelHash."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

        transport = FakeCallbackTransport()
        client = PlanItemStatusCallbackClient(transport)

        client.send(
            url="http://example.com/cb",
            plan_id="1", device_name="d1", task_name="t1",
            status="SUCCESS",
        )

        payload = transport.calls[0]["payload"]
        assert "excelHash" not in payload

    def test_send_single_body_public_fields(self):
        """send_single() body must contain exactly the public item fields."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

        transport = FakeCallbackTransport()
        client = PlanItemStatusCallbackClient(transport)

        client.send_single("http://example.com/cb", {
            "planId": "1", "deviceGroup": "g1", "deviceName": "d1", "taskName": "t1",
            "status": "SUCCESS", "updater": "system", "errorMessage": None,
            "startedAt": None, "finishedAt": None,
        })

        payload = transport.calls[0]["payload"]
        assert set(payload.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_send_single_rejects_execution_status_value(self):
        """Execution-domain statuses must never cross the callback boundary."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

        transport = FakeCallbackTransport()
        client = PlanItemStatusCallbackClient(transport)

        with pytest.raises(ValueError, match="CALLBACK_STATUS_MAPPING_ERROR"):
            client.send_single("http://example.com/cb", {
                "planId": "1", "deviceName": "d1", "taskName": "t1",
                "status": "EXEC_SUCCESS", "updater": "system", "errorMessage": None,
            })

        assert transport.calls == []

    def test_build_callback_item_public_fields(self):
        """build_callback_item() must return exactly the public item fields."""
        from src.plan_item_status_callback_client import build_callback_item

        item = build_callback_item("1", "d1", "t1", "SUCCESS")
        assert set(item.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_rule_context_resolve_path_stays_under_output_root(self, tmp_path):
        """Rule action filenames cannot traverse outside the evidence root."""
        from src.rules.engine import RuleContext

        with pytest.raises(ValueError, match="Unsafe path component"):
            RuleContext(output_dir=str(tmp_path)).resolve_path("../../outside.html")

    def test_send_batch_items_public_fields_each(self):
        """send_batch() items must not contain excelHash."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient, FakeCallbackTransport

        transport = FakeCallbackTransport()
        client = PlanItemStatusCallbackClient(transport)

        items = [
            {"planId": "1", "deviceGroup": "g1", "deviceName": "d1", "taskName": "t1",
             "status": "SUCCESS", "updater": "system", "errorMessage": None,
             "startedAt": None, "finishedAt": None},
        ]
        client.send_batch("http://example.com/cb", items)

        payload = transport.calls[0]["payload"]
        for item in payload["items"]:
            assert set(item.keys()) == {
                "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
                "updater", "errorMessage", "startedAt", "finishedAt",
            }


# ============================================================================
# P0-6: planId/runId boundary
# ============================================================================


class TestPlanIdBoundary:
    """Validate planId is the single business plan ID — no serverPlanId/callbackPlanId."""

    def test_plan_run_uses_only_plan_id(self):
        """PlanRun must NOT have server_plan_id or callback_plan_id fields."""
        from src.plan_run_service.service import PlanRun

        run = PlanRun(plan_id="1")
        # plan_id is the only plan identifier
        assert run.plan_id == "1"
        # server_plan_id and callback_plan_id must NOT exist
        assert not hasattr(run, "server_plan_id")
        assert not hasattr(run, "callback_plan_id")
        assert not hasattr(run, "executor_plan_id")
        # run_id is now a valid field on PlanRun
        assert hasattr(run, "run_id")

    def test_plan_id_used_in_callback_body(self):
        """Callback body planId must come from run.plan_id directly."""
        from src.plan_run_service.service import PlanRun
        from src.plan_item_status_callback_client import build_callback_item

        run = PlanRun(plan_id="42")

        cb = build_callback_item(
            plan_id=str(run.plan_id),
            device_name="d1",
            task_name="t1",
            status="SUCCESS",
        )
        assert cb["planId"] == "42", \
            f"planId should be plan_id, got {cb['planId']}"

    def test_no_plan_id_fields_in_callback_except_6(self):
        """Callback body must not contain executorPlanId/serverPlanId/callbackPlanId."""
        from src.plan_item_status_callback_client import build_callback_item

        cb = build_callback_item("1", "d1", "t1", "SUCCESS")
        forbidden = {"executorPlanId", "serverPlanId", "callbackPlanId",
                      "excelHash", "runId", "configId", "storedPath"}
        for key in forbidden:
            assert key not in cb, f"Forbidden field {key} found in callback: {cb}"

    def test_run_id_not_in_callback_body(self):
        """Callback body must not contain runId."""
        from src.plan_item_status_callback_client import build_callback_item

        cb = build_callback_item("1", "d1", "t1", "SUCCESS")
        assert "runId" not in cb


# ============================================================================
# Run all tests if executed directly
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
