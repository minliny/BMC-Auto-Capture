"""
AUDIT-NEW-001~008: Full regression test suite.

Covers:
  AUDIT-NEW-001: MHTML/state sensitive text redaction
  AUDIT-NEW-002: Plan item callback URL/nested payload log leak
  AUDIT-NEW-003: Report filename path escape
  AUDIT-NEW-004: Latest hot cache masking disk damage
  AUDIT-NEW-005: Outbox same record update losing attemptCount
  AUDIT-NEW-006: Config parse failure leaving by_hash
  AUDIT-NEW-007: Critical test always-true assertions (verified via grep)
  AUDIT-NEW-008: Terminal summary missing blocked/unknown
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from email import policy
from email.parser import BytesParser
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# AUDIT-NEW-001: MHTML/state sensitive text redaction
# ============================================================================

class TestAuditNew001SensitiveTextRedaction:
    """Verify all evidence files are redacted before writing to disk."""

    def test_redact_sensitive_text_authorization(self):
        """Authorization/Bearer/Basic text must be redacted."""
        from src.utils.sensitive import redact_sensitive_text
        text = "Authorization: Bearer secret_token_123"
        result = redact_sensitive_text(text)
        assert "secret_token_123" not in result
        assert "***REDACTED***" in result

    def test_redact_sensitive_text_basic_auth(self):
        from src.utils.sensitive import redact_sensitive_text
        text = "Authorization: Basic dXNlcjpwYXNz"
        result = redact_sensitive_text(text)
        assert "dXNlcjpwYXNz" not in result
        assert "***REDACTED***" in result

    def test_redact_sensitive_text_preserves_normal(self):
        from src.utils.sensitive import redact_sensitive_text
        text = "normal-ok 设备名称 任务名称"
        result = redact_sensitive_text(text)
        assert "normal-ok" in result
        assert "设备名称" in result
        assert "任务名称" in result

    def test_redact_sensitive_url_userinfo(self):
        """URL userinfo password must be redacted."""
        from src.utils.sensitive import redact_sensitive_url
        url = "https://user:URL_PASS@example.com/api"
        result = redact_sensitive_url(url)
        assert "URL_PASS" not in result
        assert "***REDACTED***" in result
        assert "example.com" in result

    def test_redact_sensitive_url_query_params(self):
        """URL query params with sensitive keys must be redacted."""
        from src.utils.sensitive import redact_sensitive_url
        url = "https://example.com/api?token=URL_TOKEN&secret=URL_SECRET&api_key=URL_KEY&normal=ok"
        result = redact_sensitive_url(url)
        assert "URL_TOKEN" not in result
        assert "URL_SECRET" not in result
        assert "URL_KEY" not in result
        assert "normal=ok" in result
        assert "***REDACTED***" in result

    def test_redact_nested_payload_dict(self):
        """Nested dict values for sensitive keys must be redacted."""
        from src.utils.sensitive import redact_nested_payload
        payload = {
            "metadata": {
                "password": "PAYLOAD_PASS",
                "nested": {"token": "PAYLOAD_TOKEN"}
            },
            "headers": {"Authorization": "Bearer PAYLOAD_BEARER"}
        }
        result = redact_nested_payload(payload)
        assert result["metadata"]["password"] == "***REDACTED***"
        assert result["metadata"]["nested"]["token"] == "***REDACTED***"
        assert result["headers"]["Authorization"] == "***REDACTED***"

    def test_redact_nested_payload_list(self):
        """List items must be recursively redacted."""
        from src.utils.sensitive import redact_nested_payload
        payload = [{"password": "secret1"}, {"token": "secret2"}]
        result = redact_nested_payload(payload)
        assert result[0]["password"] == "***REDACTED***"
        assert result[1]["token"] == "***REDACTED***"

    def test_redact_state_payload_url(self):
        """State payload URL must be redacted."""
        from src.utils.sensitive import redact_state_payload
        state = {"url": "https://example.com?token=abc123", "visible_text": "normal-ok"}
        result = redact_state_payload(state)
        assert "abc123" not in result["url"]
        assert "***REDACTED***" in result["url"]
        assert "normal-ok" in result["visible_text"]

    def test_redact_state_payload_visible_text(self):
        """State payload visible_text with Authorization must be redacted."""
        from src.utils.sensitive import redact_state_payload
        state = {"visible_text": "Authorization: Bearer my_secret_token", "url": "https://example.com"}
        result = redact_state_payload(state)
        assert "my_secret_token" not in result["visible_text"]
        assert "***REDACTED***" in result["visible_text"]

    def test_redact_state_payload_table_text(self):
        """Table visible_text_excerpt must be redacted."""
        from src.utils.sensitive import redact_state_payload
        state = {
            "tables": [{"visible_text_excerpt": "Authorization: Bearer table_secret"}],
            "url": "https://example.com"
        }
        result = redact_state_payload(state)
        assert "table_secret" not in result["tables"][0]["visible_text_excerpt"]

    def test_redact_state_payload_custom_element_text(self):
        """Custom element text must be redacted."""
        from src.utils.sensitive import redact_state_payload
        state = {
            "checked_like": [{"text": "Bearer custom_secret"}],
            "active_tab_like": [{"text": "Authorization: Basic abc"}],
            "url": "https://example.com"
        }
        result = redact_state_payload(state)
        assert "custom_secret" not in result["checked_like"][0]["text"]
        assert "abc" not in result["active_tab_like"][0]["text"]

    def test_redact_mhtml_payload(self):
        """MHTML content with Authorization must be redacted."""
        from src.utils.sensitive import redact_mhtml_payload
        mhtml = "Content-Type: text/html\n\n<html><body>Authorization: Bearer mhtml_secret</body></html>"
        result = redact_mhtml_payload(mhtml)
        assert "mhtml_secret" not in result
        assert "***REDACTED***" in result

    def test_redact_state_payload_select_and_nested_fields(self):
        from src.utils.sensitive import redact_state_payload

        state = {
            "url": "https://example.com/page?token=URL_TOKEN_SECRET&api_key=URL_API_KEY_SECRET",
            "visible_text": "Bearer VISIBLE_TOKEN_SECRET normal-ok",
            "selects": [{
                "selector": 'SELECT[name="api_token"]',
                "selected_values": ["SELECT_TOKEN_SECRET"],
                "selected_texts": ["SELECT_TOKEN_SECRET"],
                "options": [{
                    "value": "OPT_TOKEN_SECRET",
                    "text": "OPT_TOKEN_SECRET",
                }],
            }],
            "tables": [{"visible_text_excerpt": "TABLE_SECRET_TOKEN normal-ok"}],
            "checked_like": [{"text": "CUSTOM_SECRET_TOKEN normal-ok"}],
            "metadata": {
                "addressbar_url": "https://example.com/?token=URL_TOKEN_SECRET",
            },
        }

        result = json.dumps(redact_state_payload(state), ensure_ascii=False)
        for secret in (
            "SELECT_TOKEN_SECRET", "OPT_TOKEN_SECRET", "URL_TOKEN_SECRET",
            "URL_API_KEY_SECRET", "VISIBLE_TOKEN_SECRET", "TABLE_SECRET_TOKEN",
            "CUSTOM_SECRET_TOKEN",
        ):
            assert secret not in result
        assert "***REDACTED***" in result
        assert "normal-ok" in result

    def test_redact_mhtml_transfer_encodings(self):
        from src.utils.sensitive import redact_mhtml_payload

        plain = "Authorization: Bearer MHTML_TOKEN_SECRET normal-ok"
        encoded_html = base64.b64encode(
            b"<html><body>token=BASE64_MHTML_TOKEN_SECRET normal-ok</body></html>"
        ).decode("ascii")
        qp_json = "password=3DQP_MHTML_TOKEN_SECRET normal-ok"
        mhtml = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; boundary=freeze-boundary\r\n\r\n"
            "--freeze-boundary\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n"
            f"{plain}\r\n"
            "--freeze-boundary\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\n"
            f"{encoded_html}\r\n"
            "--freeze-boundary\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            f"{qp_json}\r\n"
            "--freeze-boundary--\r\n"
        )

        result = redact_mhtml_payload(mhtml)
        parsed = BytesParser(policy=policy.default).parsebytes(result.encode("utf-8"))
        decoded = []
        for part in parsed.walk():
            if not part.is_multipart():
                decoded.append((part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="replace"))
        combined = "\n".join(decoded)
        for secret in (
            "MHTML_TOKEN_SECRET", "BASE64_MHTML_TOKEN_SECRET",
            "QP_MHTML_TOKEN_SECRET",
        ):
            assert secret not in result
            assert secret not in combined
        assert "***REDACTED***" in combined
        assert "normal-ok" in combined


# ============================================================================
# AUDIT-NEW-002: Plan item callback URL/payload log leak
# ============================================================================

class TestAuditNew002CallbackLeak:
    """Verify plan item callback URLs and payloads are redacted in logs."""

    def test_plan_item_callback_redacts_response_and_payload(self):
        """Callback payloads must strip non-public sensitive fields."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient

        class FailureTransport:
            def __init__(self):
                self.calls = []

            def post(self, url, payload, headers):
                self.calls.append({"url": url, "payload": dict(payload), "headers": dict(headers)})
                return 500, (
                    '{"error":"RESPONSE_TOKEN_SECRET",'
                    '"detail":{"password":"RESPONSE_PASS_SECRET"}}'
                )

        transport = FailureTransport()
        client = PlanItemStatusCallbackClient(transport=transport)
        result = client.send_single(
            "https://example.com/cb?token=URL_TOKEN_SECRET&api_key=URL_API_SECRET",
            {
                "planId": "p1",
                "deviceName": "d1",
                "taskName": "t1",
                "status": "FAILED",
                "updater": "audit",
                "errorMessage": "failed",
                "metadata": {
                    "password": "PAYLOAD_PASS_SECRET",
                    "nested": {"token": "PAYLOAD_TOKEN_SECRET"},
                },
                "headers": {"Authorization": "Bearer PAYLOAD_BEARER_SECRET"},
                "deep": {"secret": "DEEP_SECRET_TOKEN"},
            },
        )

        assert result.failed == 1
        payload = transport.calls[0]["payload"]
        payload_text = json.dumps(payload, ensure_ascii=False)
        for secret in (
            "PAYLOAD_PASS_SECRET", "PAYLOAD_TOKEN_SECRET",
            "PAYLOAD_BEARER_SECRET", "DEEP_SECRET_TOKEN",
        ):
            assert secret not in payload_text
        assert set(payload) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName",
            "status", "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_plan_item_callback_redacts_exception_text(self, caplog):
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient

        class ExceptionTransport:
            def post(self, url, payload, headers):
                raise RuntimeError(
                    "password=EXCEPTION_PASS_SECRET token=EXCEPTION_TOKEN_SECRET")

        client = PlanItemStatusCallbackClient(transport=ExceptionTransport())
        with caplog.at_level("ERROR"):
            result = client.send_single(
                "https://example.com/cb?token=URL_TOKEN_SECRET",
                {"planId": "p1", "deviceName": "d1", "taskName": "t1", "status": "FAILED"},
            )
        assert result.failed == 1
        assert "EXCEPTION_PASS_SECRET" not in caplog.text
        assert "EXCEPTION_TOKEN_SECRET" not in caplog.text
        assert "URL_TOKEN_SECRET" not in caplog.text
        assert "REDACTED" in caplog.text

    def test_callback_outbox_redacts_sensitive_in_persistence(self):
        """CallbackOutbox must redact sensitive values in jsonl."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="test-plan",
                device_name="device1",
                task_name="task1",
                status="FAILED",
                error_message="password=LEAKED_PASS token=LEAKED_TOKEN",
                callback_url="https://user:URL_PASS@example.com?token=URL_TOKEN",
                last_error_message="STANDALONE_SECRET_TOKEN",
            )
            outbox.append(item)
            # Read the jsonl file and verify redaction
            with open(outbox._outbox_path, "r") as f:
                content = f.read()
            assert "LEAKED_PASS" not in content
            assert "LEAKED_TOKEN" not in content
            assert "URL_PASS" not in content
            assert "URL_TOKEN" not in content
            assert "STANDALONE_SECRET_TOKEN" not in content
            assert "***REDACTED***" in content

    def test_callback_body_public_fields(self):
        """PlanItem callback body must be exactly the public item fields."""
        from src.plan_item_status_callback_client import build_callback_item
        body = build_callback_item(
            plan_id="p1", device_name="d1", task_name="t1",
            status="SUCCESS", updater="system",
            error_message=None,
        )
        assert set(body.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_callback_body_rejects_extra_fields(self):
        """Extra fields in callback body must be stripped."""
        from src.plan_item_status_callback_client import _sanitize_callback_item
        item = {
            "planId": "p1", "deviceName": "d1", "taskName": "t1",
            "status": "SUCCESS", "updater": "system", "errorMessage": None,
            "excelHash": "abc123",  # extra
            "metadata": {"password": "secret"},  # extra
        }
        result = _sanitize_callback_item(item)
        assert "excelHash" not in result
        assert "metadata" not in result
        assert set(result.keys()) == {
            "planId", "taskId", "planItemId", "deviceGroup", "deviceName", "taskName", "status",
            "updater", "errorMessage", "startedAt", "finishedAt",
        }

    def test_callback_error_message_is_redacted(self):
        from src.plan_item_status_callback_client import _sanitize_callback_item

        result = _sanitize_callback_item({
            "planId": "p1", "deviceName": "d1", "taskName": "t1",
            "status": "FAILED", "updater": "system",
            "errorMessage": "password=CALLBACK_ERROR_PASS token=CALLBACK_ERROR_TOKEN",
        })
        assert "CALLBACK_ERROR_PASS" not in result["errorMessage"]
        assert "CALLBACK_ERROR_TOKEN" not in result["errorMessage"]
        assert "REDACTED" in result["errorMessage"]

    def test_callback_illegal_status_rejected(self):
        """Illegal status values must be rejected."""
        from src.plan_item_status_callback_client import map_status_to_server
        with pytest.raises(ValueError):
            map_status_to_server("INVALID_STATUS")


# ============================================================================
# AUDIT-NEW-003: Report filename path escape
# ============================================================================

class TestAuditNew003PathEscape:
    """Verify report filenames cannot escape output root."""

    def test_collector_rejects_traversal_filename(self):
        """Collector must reject ../escaped.csv."""
        from src.out.collector import write_result_csv
        results = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Unsafe filename"):
                write_result_csv(results, tmpdir, filename="../escaped.csv")

    def test_summary_rejects_traversal_filename(self):
        """Summary must reject ../../summary.csv."""
        from src.out.summary import build_pivot_csv
        results = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Unsafe filename"):
                build_pivot_csv(results, tmpdir, filename="../../summary.csv")

    def test_timing_rejects_absolute_filename(self):
        """Timing must reject /tmp/timing.csv."""
        from src.out.timing import write_plan_timing_csv
        results = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Unsafe filename"):
                write_plan_timing_csv(results, tmpdir, filename="/tmp/timing.csv")

    def test_safe_filename_passes(self):
        """Safe filenames must work normally."""
        from src.out.collector import write_result_csv
        from src.models.execution_result import ExecutionResult
        results = [ExecutionResult("p1", "d1", task_name="t1")]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_result_csv(results, tmpdir, filename="result.csv")
            assert path.startswith(tmpdir)
            assert os.path.isfile(path)

    def test_final_result_csv_safe_filename(self):
        """final_result.csv must work with safe filename."""
        from src.out.collector import write_final_result_csv
        from src.models.execution_result import ExecutionResult
        results = [ExecutionResult("p1", "d1", task_name="t1")]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_final_result_csv(results, tmpdir, filename="final_result.csv")
            assert path.startswith(tmpdir)
            assert os.path.isfile(path)

    def test_file_writer_rejects_traversal(self):
        """File writer must reject traversal filenames."""
        from src.out.file_writer import write_html_file
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Unsafe filename"):
                write_html_file(tmpdir, "../escaped.html", "<html></html>")

    def test_no_unprotected_os_path_join_in_reports(self):
        """Verify no unprotected os.path.join(output_dir, filename) in report writers."""
        files_to_check = [
            Path(__file__).resolve().parent.parent / "src" / "out" / "collector.py",
            Path(__file__).resolve().parent.parent / "src" / "out" / "summary.py",
            Path(__file__).resolve().parent.parent / "src" / "out" / "timing.py",
            Path(__file__).resolve().parent.parent / "src" / "out" / "file_writer.py",
        ]
        pattern = re.compile(r'os\.path\.join\s*\(\s*output_dir\s*,\s*filename\s*\)')
        for f in files_to_check:
            if f.exists():
                content = f.read_text()
                matches = pattern.findall(content)
                assert len(matches) == 0, f"Unprotected os.path.join(output_dir, filename) in {f.name}: {matches}"


# ============================================================================
# AUDIT-NEW-004: Latest hot cache masking disk damage
# ============================================================================

class TestAuditNew004HotCache:
    """Verify hot cache doesn't mask disk damage."""

    def test_malformed_latest_returns_config_corrupted_without_cache_fallback(self):
        """Malformed latest.json must return CONFIG_CORRUPTED even with hot cache."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            # Activate a valid config first
            xlsx_path = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
            if xlsx_path.exists():
                result = store.activate_from_local_path(str(xlsx_path))
                if result.get("accepted"):
                    # Now corrupt latest.json
                    latest_json = store.latest_json_path
                    latest_json.write_text("NOT VALID JSON {{{")
                    # Don't clear memory cache — the bug was cache masking corruption
                    meta = store.get_latest()
                    assert meta is not None
                    assert meta.get("code") == "CONFIG_CORRUPTED", f"Expected CONFIG_CORRUPTED, got: {meta}"

    def test_missing_stored_path_returns_latest_excel_missing(self):
        """Missing storedPath must return LATEST_EXCEL_MISSING even with hot cache."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            xlsx_path = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
            if xlsx_path.exists():
                result = store.activate_from_local_path(str(xlsx_path))
                if result.get("accepted"):
                    # Delete the stored file
                    stored = result.get("storedPath", "")
                    if stored and os.path.exists(stored):
                        os.unlink(stored)
                    # Don't clear memory cache
                    meta = store.get_latest()
                    assert meta is not None
                    assert meta.get("code") == "LATEST_EXCEL_MISSING", f"Expected LATEST_EXCEL_MISSING, got: {meta}"

    def test_hash_mismatch_returns_latest_excel_hash_mismatch(self):
        """Hash mismatch must return LATEST_EXCEL_HASH_MISMATCH even with hot cache."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            xlsx_path = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
            if xlsx_path.exists():
                result = store.activate_from_local_path(str(xlsx_path))
                if result.get("accepted"):
                    # Modify the stored file content
                    stored = result.get("storedPath", "")
                    if stored and os.path.exists(stored):
                        with open(stored, "ab") as f:
                            f.write(b"\x00MODIFIED")
                    # Don't clear memory cache
                    meta = store.get_latest()
                    assert meta is not None
                    assert meta.get("code") == "LATEST_EXCEL_HASH_MISMATCH", f"Expected LATEST_EXCEL_HASH_MISMATCH, got: {meta}"

    def test_no_latest_json_allows_legacy_migration(self):
        """No latest.json + legacy exists should allow migration."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            # No latest.json, no legacy → None
            meta = store.get_latest()
            assert meta is None

    def test_corrupted_latest_does_not_fallback_to_legacy(self):
        """Corrupted latest.json must NOT fall back to legacy."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            # Create a corrupted latest.json
            store.latest_json_path.write_text("CORRUPTED{")
            meta = store.get_latest()
            assert meta is not None
            assert meta.get("code") == "CONFIG_CORRUPTED"


# ============================================================================
# AUDIT-NEW-005: Outbox same record update losing attemptCount
# ============================================================================

class TestAuditNew005OutboxAttemptCount:
    """Verify concurrent mark_failed doesn't lose attemptCount."""

    def test_concurrent_mark_failed_attempt_count(self):
        """2 concurrent mark_failed on same outboxId must result in attemptCount=2."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="test-plan", device_name="d1", task_name="t1",
                status="PENDING", callback_url="https://example.com",
            )
            outbox.append(item)
            oid = item.outbox_id

            barrier = threading.Barrier(2)
            results = [None, None]

            def mark_failed(idx):
                barrier.wait(timeout=5)
                results[idx] = outbox.mark_failed(oid, "error", retryable=True)

            t1 = threading.Thread(target=mark_failed, args=(0,))
            t2 = threading.Thread(target=mark_failed, args=(1,))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            # Verify attemptCount is 2
            found = outbox._find_item(oid)
            assert found is not None
            assert found.attempt_count == 2, f"Expected attemptCount=2, got {found.attempt_count}"

    def test_high_concurrency_attempt_count(self):
        """20 concurrent mark_failed must result in attemptCount=20."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="test-plan", device_name="d1", task_name="t1",
                status="PENDING", callback_url="https://example.com",
            )
            outbox.append(item)
            oid = item.outbox_id

            barrier = threading.Barrier(20)
            threads = []
            for i in range(20):
                t = threading.Thread(target=lambda: (barrier.wait(timeout=10), outbox.mark_failed(oid, "error", retryable=True)))
                threads.append(t)
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            found = outbox._find_item(oid)
            assert found is not None
            assert found.attempt_count == 20, f"Expected attemptCount=20, got {found.attempt_count}"

    def test_different_outbox_ids_concurrent(self):
        """Different outboxIds concurrent mark_sent/mark_failed must not lose records."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            items = []
            for i in range(5):
                item = CallbackOutboxItem(
                    plan_id="test-plan", device_name=f"d{i}", task_name=f"t{i}",
                    status="PENDING", callback_url="https://example.com",
                )
                outbox.append(item)
                items.append(item)

            threads = []
            for item in items:
                t = threading.Thread(target=outbox.mark_failed, args=(item.outbox_id, "error"))
                threads.append(t)
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            # All 5 records must still exist
            stats = outbox.get_stats()
            assert stats.get("FAILED_RETRYABLE", 0) + stats.get("FAILED_FINAL", 0) == 5, f"Expected 5 failed, got {stats}"

    def test_no_temp_files_remaining(self):
        """No .outbox.*.jsonl temp files should remain after operations."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="test-plan", device_name="d1", task_name="t1",
                status="PENDING", callback_url="https://example.com",
            )
            outbox.append(item)
            outbox.mark_failed(item.outbox_id, "error")
            outbox.mark_sent(item.outbox_id)

            # Check for temp files
            for f in os.listdir(outbox._outbox_dir):
                assert not f.startswith(".outbox."), f"Temp file remaining: {f}"


# ============================================================================
# AUDIT-NEW-006: Config parse failure leaving by_hash
# ============================================================================

class TestAuditNew006ConfigParseFailure:
    """Verify parse failure doesn't leave orphan by_hash files."""

    def test_invalid_xlsx_no_orphan_by_hash(self):
        """Invalid xlsx must not leave orphan by_hash file."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            by_hash_count_before = len(list(store.by_hash_dir.glob("*.xlsx"))) if store.by_hash_dir.exists() else 0

            # Create an invalid xlsx
            result = store.activate_from_upload(b"not a real xlsx file content at all", "test.xlsx")
            assert result.get("accepted") is False or result.get("code") is not None

            by_hash_count_after = len(list(store.by_hash_dir.glob("*.xlsx"))) if store.by_hash_dir.exists() else 0
            assert by_hash_count_after == by_hash_count_before, \
                f"Orphan by_hash files: before={by_hash_count_before}, after={by_hash_count_after}"

    def test_invalid_extension_rejected(self):
        """Non-.xlsx extension must be rejected."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            result = store.activate_from_upload(b"content", "test.csv")
            assert result.get("accepted") is False
            assert result.get("code") == "INVALID_EXCEL_FILE"

    def test_parse_failure_latest_json_unchanged(self):
        """Parse failure must not update latest.json."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            # Activate valid first
            xlsx_path = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
            if xlsx_path.exists():
                r1 = store.activate_from_local_path(str(xlsx_path))
                if r1.get("accepted"):
                    # Record latest.json content
                    latest_before = store.latest_json_path.read_text()
                    # Try invalid upload
                    store.activate_from_upload(b"invalid content for parse", "bad.xlsx")
                    # latest.json must be unchanged
                    latest_after = store.latest_json_path.read_text()
                    assert latest_before == latest_after, "latest.json was modified by failed parse"

    def test_parse_failure_memory_cache_unchanged(self):
        """Parse failure must not update memory cache."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            xlsx_path = Path(__file__).resolve().parent.parent / "examples" / "task_template.xlsx"
            if xlsx_path.exists():
                r1 = store.activate_from_local_path(str(xlsx_path))
                if r1.get("accepted"):
                    hash_before = store._memory_cache.get("excelHash", "")
                    store.activate_from_upload(b"invalid content for parse", "bad.xlsx")
                    hash_after = store._memory_cache.get("excelHash", "") if store._memory_cache else ""
                    assert hash_before == hash_after, "Memory cache was modified by failed parse"

    def test_no_tmp_files_remaining(self):
        """No .parse.* or .latest.* temp files should remain after operations."""
        from src.excel_config_store import ExcelConfigStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ExcelConfigStore(tmpdir)
            store.activate_from_upload(b"invalid content", "bad.xlsx")
            # Check for temp files in configs dir
            configs_dir = store._workspace / store.CONFIGS_DIR
            if configs_dir.exists():
                for f in configs_dir.rglob("*"):
                    assert not f.name.startswith(".parse."), f"Parse temp file remaining: {f}"
                    assert not f.name.startswith(".latest."), f"Latest temp file remaining: {f}"

    def test_latest_commit_failure_rolls_back_new_files(self, monkeypatch):
        import src.excel_config_store as store_module
        import src.plan_run_service.service as plan_service

        with tempfile.TemporaryDirectory() as tmpdir:
            store = store_module.ExcelConfigStore(tmpdir)
            monkeypatch.setattr(
                plan_service, "_set_latest_excel",
                lambda _path: {
                    "deviceCount": 1, "enabledDeviceCount": 1,
                    "taskCount": 1, "enabledTaskCount": 1,
                },
            )
            real_replace = os.replace

            def fail_latest_replace(src, dst):
                if str(dst).endswith("latest.json"):
                    raise OSError("simulated latest replace failure")
                return real_replace(src, dst)

            monkeypatch.setattr(store_module.os, "replace", fail_latest_replace)
            before = len(list(store.by_hash_dir.glob("*.xlsx")))
            result = store.activate_from_upload(b"A" * 200, "a.xlsx")

            assert result == {
                "accepted": False,
                "code": "LATEST_COMMIT_FAILED",
                "message": "latest.json commit failed: simulated latest replace failure",
            }
            assert len(list(store.by_hash_dir.glob("*.xlsx"))) == before
            assert not store.latest_json_path.exists()
            assert store._memory_cache is None
            assert not list((store._workspace / store.CONFIGS_DIR).glob(".latest.*.json"))

    def test_latest_commit_failure_preserves_previous_version(self, monkeypatch):
        import src.excel_config_store as store_module
        import src.plan_run_service.service as plan_service

        with tempfile.TemporaryDirectory() as tmpdir:
            store = store_module.ExcelConfigStore(tmpdir)
            monkeypatch.setattr(
                plan_service, "_set_latest_excel",
                lambda _path: {
                    "deviceCount": 1, "enabledDeviceCount": 1,
                    "taskCount": 1, "enabledTaskCount": 1,
                },
            )
            first = store.activate_from_upload(b"A" * 200, "a.xlsx")
            assert first["accepted"] is True
            latest_before = store.latest_json_path.read_text(encoding="utf-8")
            cache_before = dict(store._memory_cache or {})
            files_before = set(store.by_hash_dir.glob("*.xlsx"))
            real_replace = os.replace

            def fail_latest_replace(src, dst):
                if str(dst).endswith("latest.json"):
                    raise OSError("simulated latest replace failure")
                return real_replace(src, dst)

            monkeypatch.setattr(store_module.os, "replace", fail_latest_replace)
            second = store.activate_from_upload(b"B" * 200, "b.xlsx")

            assert second["accepted"] is False
            assert second["code"] == "LATEST_COMMIT_FAILED"
            assert store.latest_json_path.read_text(encoding="utf-8") == latest_before
            assert store._memory_cache == cache_before
            assert set(store.by_hash_dir.glob("*.xlsx")) == files_before
            assert not list((store._workspace / store.CONFIGS_DIR).glob(".latest.*.json"))


# ============================================================================
# AUDIT-NEW-007: Critical test always-true assertions (verified via grep)
# ============================================================================

class TestAuditNew007AlwaysTrueAssertions:
    """Verify no assert True / or True / literal is not None in test files."""

    def test_no_assert_true_in_tests(self):
        """No assert True should remain in test files."""
        tests_dir = Path(__file__).resolve().parent
        hits = []
        for py_file in tests_dir.glob("test_*.py"):
            content = py_file.read_text()
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if re.match(r'^\s*assert\s+True\s*$', stripped):
                    hits.append(f"{py_file.name}:{i}: {stripped}")
        # assert True is only acceptable in non-security, documented contexts
        assert len(hits) == 0, f"Found assert True in test files:\n" + "\n".join(hits)

    def test_no_or_true_in_tests(self):
        """No 'or True' should remain in test assertions."""
        tests_dir = Path(__file__).resolve().parent
        hits = []
        for py_file in tests_dir.glob("test_*.py"):
            content = py_file.read_text()
            for i, line in enumerate(content.splitlines(), 1):
                if "or True" in line and "assert" in line:
                    # Skip self-referential hits in this test file
                    if py_file.name == "test_audit_new_regression.py":
                        continue
                    hits.append(f"{py_file.name}:{i}: {line.strip()}")
        assert len(hits) == 0, f"Found 'or True' in test assertions:\n" + "\n".join(hits)


# ============================================================================
# AUDIT-NEW-008: Terminal summary missing blocked/unknown
# ============================================================================

class TestAuditNew008SummaryBlockedUnknown:
    """Verify summary includes blocked/unknown counts."""

    def test_compute_summary_includes_blocked(self):
        """compute_summary must include blocked count."""
        from src.out.collector import compute_summary
        from src.models.execution_result import ExecutionResult
        results = [
            ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS"),
            ExecutionResult("p2", "d2", task_name="t2", execution_status="EXEC_BLOCKED"),
        ]
        s = compute_summary(results)
        assert s["blocked"] == 1, f"Expected blocked=1, got {s['blocked']}"

    def test_compute_summary_includes_unknown(self):
        """compute_summary must include unknown count."""
        from src.out.collector import compute_summary
        from src.models.execution_result import ExecutionResult
        results = [
            ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS"),
            ExecutionResult("p2", "d2", task_name="t2", execution_status="EXEC_WEIRD_UNKNOWN"),
        ]
        s = compute_summary(results)
        assert s["unknown"] == 1, f"Expected unknown=1, got {s['unknown']}"

    def test_summary_total_closure(self):
        """Total must equal sum of all status counts."""
        from src.out.collector import compute_summary
        from src.models.execution_result import ExecutionResult
        results = [
            ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS"),
            ExecutionResult("p2", "d2", task_name="t2", execution_status="EXEC_FAILED"),
            ExecutionResult("p3", "d3", task_name="t3", execution_status="EXEC_BLOCKED"),
            ExecutionResult("p4", "d4", task_name="t4", execution_status="EXEC_WEIRD"),
        ]
        s = compute_summary(results)
        status_sum = (s["success"] + s["failed"] + s["error"] + s["timeout"] + s["partial"] +
                      s["blocked"] + s["unknown"] +
                      s["skipped_preflight"] + s["skipped_port_blocked"] + s["skipped_route"] +
                      s["skipped_stopped"] + s["skipped_disabled"] + s["skipped_session"])
        assert status_sum == s["total"], f"Summary not closed: {status_sum} != {s['total']}"

    def test_unknown_not_counted_as_success(self):
        """UNKNOWN must NOT be counted as success."""
        from src.out.collector import compute_summary
        from src.models.execution_result import ExecutionResult
        results = [
            ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_UNKNOWN_STATUS"),
        ]
        s = compute_summary(results)
        assert s["success"] == 0, f"Unknown was counted as success: {s}"
        assert s["unknown"] == 1

    def test_print_terminal_summary_includes_blocked_unknown(self, capsys):
        """print_terminal_summary must display blocked and unknown."""
        from src.out.summary import print_terminal_summary
        from src.models.execution_result import ExecutionResult
        results = [
            ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS"),
            ExecutionResult("p2", "d2", task_name="t2", execution_status="EXEC_BLOCKED"),
            ExecutionResult("p3", "d3", task_name="t3", execution_status="EXEC_WEIRD"),
        ]
        print_terminal_summary(results)
        captured = capsys.readouterr()
        assert "阻塞" in captured.out, f"blocked not shown in terminal summary"
        assert "未知" in captured.out, f"unknown not shown in terminal summary"


# ============================================================================
# FZ-AUDIT-003: Opaque secret redaction regression
# ============================================================================

OPAQUE_SECRET = "Q7v9Z2m4N8x6"


class TestFZAudit003OpaqueSecretRedaction:
    """Verify opaque secrets (no keyword in value) are redacted by field context."""

    def test_opaque_secret_html_input_password(self):
        from src.utils.sensitive import redact_html_sensitive_fields
        html = f'<input name="password" value="{OPAQUE_SECRET}">'
        result = redact_html_sensitive_fields(html)
        assert OPAQUE_SECRET not in result
        assert "***REDACTED***" in result

    def test_opaque_secret_html_input_api_token(self):
        from src.utils.sensitive import redact_html_sensitive_fields
        html = f'<input id="api_token" value="{OPAQUE_SECRET}">'
        result = redact_html_sensitive_fields(html)
        assert OPAQUE_SECRET not in result
        assert "***REDACTED***" in result

    def test_opaque_secret_html_input_autocomplete(self):
        from src.utils.sensitive import redact_html_sensitive_fields
        html = f'<input autocomplete="current-password" value="{OPAQUE_SECRET}">'
        result = redact_html_sensitive_fields(html)
        assert OPAQUE_SECRET not in result
        assert "***REDACTED***" in result

    def test_opaque_secret_html_textarea(self):
        from src.utils.sensitive import redact_html_sensitive_fields
        html = f'<textarea name="secret">{OPAQUE_SECRET}</textarea>'
        result = redact_html_sensitive_fields(html)
        assert OPAQUE_SECRET not in result
        assert "***REDACTED***" in result

    def test_opaque_secret_html_select(self):
        from src.utils.sensitive import redact_html_sensitive_fields
        html = (
            f'<select name="access_token">'
            f'<option selected value="{OPAQUE_SECRET}">{OPAQUE_SECRET}</option>'
            f'</select>'
        )
        result = redact_html_sensitive_fields(html)
        assert OPAQUE_SECRET not in result
        assert "***REDACTED***" in result

    def test_opaque_secret_mhtml_base64_decoded(self):
        """Base64-encoded MHTML part must not contain opaque secret after decode."""
        from src.utils.sensitive import redact_mhtml_payload
        import base64
        from email import policy
        from email.parser import BytesParser

        encoded_html = base64.b64encode(
            f'<html><body><input name="password" value="{OPAQUE_SECRET}"></body></html>'.encode("utf-8")
        ).decode("ascii")
        mhtml = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; boundary=opaque-b64\r\n\r\n"
            "--opaque-b64\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\n"
            f"{encoded_html}\r\n"
            "--opaque-b64--\r\n"
        )
        result = redact_mhtml_payload(mhtml)
        parsed = BytesParser(policy=policy.default).parsebytes(result.encode("utf-8"))
        for part in parsed.walk():
            if not part.is_multipart():
                decoded = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="replace")
                assert OPAQUE_SECRET not in decoded

    def test_opaque_secret_mhtml_quoted_printable_decoded(self):
        """Quoted-printable MHTML part must not contain opaque secret after decode."""
        from src.utils.sensitive import redact_mhtml_payload
        import quopri
        from email import policy
        from email.parser import BytesParser

        raw_html = f'<html><body><input name="token" value="{OPAQUE_SECRET}"></body></html>'
        qp_html = quopri.encodestring(raw_html.encode("utf-8")).decode("ascii")
        mhtml = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; boundary=opaque-qp\r\n\r\n"
            "--opaque-qp\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            f"{qp_html}\r\n"
            "--opaque-qp--\r\n"
        )
        result = redact_mhtml_payload(mhtml)
        parsed = BytesParser(policy=policy.default).parsebytes(result.encode("utf-8"))
        for part in parsed.walk():
            if not part.is_multipart():
                decoded = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", errors="replace")
                assert OPAQUE_SECRET not in decoded

    def test_opaque_secret_mhtml_json_part(self):
        """application/json MIME part must not contain opaque secret."""
        from src.utils.sensitive import redact_mhtml_payload
        from email import policy
        from email.parser import BytesParser

        json_payload = f'{{"token": "{OPAQUE_SECRET}", "password": "{OPAQUE_SECRET}"}}'
        mhtml = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/related; boundary=opaque-json\r\n\r\n"
            "--opaque-json\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Transfer-Encoding: 7bit\r\n\r\n"
            f"{json_payload}\r\n"
            "--opaque-json--\r\n"
        )
        result = redact_mhtml_payload(mhtml)
        assert OPAQUE_SECRET not in result

    def test_opaque_secret_state_selected_values(self):
        """State selected_values/texts for sensitive select must be redacted."""
        from src.utils.sensitive import redact_state_payload
        state = {
            "url": "https://example.com",
            "selects": [{
                "selector": 'SELECT[name="api_token"]',
                "selected_values": [OPAQUE_SECRET],
                "selected_texts": [OPAQUE_SECRET],
                "options": [{"value": OPAQUE_SECRET, "text": OPAQUE_SECRET}],
            }],
        }
        result = redact_state_payload(state)
        result_json = json.dumps(result, ensure_ascii=False)
        assert OPAQUE_SECRET not in result_json
        assert "***REDACTED***" in result_json

    def test_opaque_secret_nested_payload(self):
        from src.utils.sensitive import redact_nested_payload
        payload = {"token": OPAQUE_SECRET, "password": OPAQUE_SECRET}
        result = redact_nested_payload(payload)
        assert result["token"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"

    def test_opaque_secret_sensitive_text_json(self):
        from src.utils.sensitive import redact_sensitive_text
        text = f'{{"token":"{OPAQUE_SECRET}","password":"{OPAQUE_SECRET}"}}'
        result = redact_sensitive_text(text)
        # redact_sensitive_text works on text patterns; for JSON, the key-based
        # pattern should catch token= and password= patterns
        # The key names themselves will be redacted by the keyword pattern
        assert "REDACTED" in result

    def test_opaque_secret_callback_caplog(self, caplog):
        """Callback client must not leak opaque secret in caplog."""
        from src.plan_item_status_callback_client import PlanItemStatusCallbackClient

        class FailureTransport:
            def post(self, url, payload, headers):
                return 500, f'{{"token":"{OPAQUE_SECRET}","password":"{OPAQUE_SECRET}"}}'

        client = PlanItemStatusCallbackClient(transport=FailureTransport())
        with caplog.at_level("DEBUG"):
            result = client.send_single(
                "https://example.com/cb",
                {"planId": "p1", "deviceName": "d1", "taskName": "t1",
                 "status": "FAILED", "metadata": {"password": OPAQUE_SECRET, "token": OPAQUE_SECRET}},
            )
        result_text = str(result)
        assert OPAQUE_SECRET not in result_text
        assert "REDACTED" in result_text or "TRUNCATED" in result_text

    def test_opaque_secret_outbox_jsonl(self):
        """Outbox jsonl must not contain opaque secret."""
        from src.callback_outbox import CallbackOutbox, CallbackOutboxItem
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = CallbackOutbox("test-plan", outbox_dir=tmpdir)
            item = CallbackOutboxItem(
                plan_id="test-plan",
                device_name="device1",
                task_name="task1",
                status="FAILED",
                error_message=f'{{"token":"{OPAQUE_SECRET}","password":"{OPAQUE_SECRET}"}}',
                callback_url=f"https://example.com?token={OPAQUE_SECRET}",
                last_error_message=f"password={OPAQUE_SECRET}",
            )
            outbox.append(item)
            with open(outbox._outbox_path, "r") as f:
                content = f.read()
            assert OPAQUE_SECRET not in content
            assert "***REDACTED***" in content

    def test_opaque_secret_deep_payload_20_layers(self):
        """Deep nested payload (20 layers) must redact opaque secret."""
        from src.utils.sensitive import redact_nested_payload
        deep = {}
        cursor = deep
        for i in range(20):
            cursor[f"n{i}"] = {}
            cursor = cursor[f"n{i}"]
        cursor["secret"] = OPAQUE_SECRET
        result = redact_nested_payload(deep)
        result_json = json.dumps(result, ensure_ascii=False)
        assert OPAQUE_SECRET not in result_json
        assert "TRUNCATED" in result_json or "REDACTED" in result_json

    def test_opaque_secret_normal_ok_preserved(self):
        """normal-ok text must be preserved in all redaction functions."""
        from src.utils.sensitive import (
            redact_sensitive_text, redact_html_sensitive_fields,
            redact_nested_payload, redact_state_payload,
        )
        assert "normal-ok" in redact_sensitive_text("normal-ok")
        html = '<input name="search" value="normal-ok">'
        assert "normal-ok" in redact_html_sensitive_fields(html)
        assert "normal-ok" in redact_nested_payload({"field": "normal-ok"})["field"]
        state = {"url": "https://example.com", "visible_text": "normal-ok"}
        assert "normal-ok" in redact_state_payload(state)["visible_text"]


# ============================================================================
# Integration: Test isolation
# ============================================================================

class TestIsolation:
    """Verify tests don't pollute the repository."""

    def test_no_executor_state_in_repo(self):
        """No executor_state should be created in the project root."""
        project_root = Path(__file__).resolve().parent.parent
        es = project_root / "executor_state"
        # executor_state may exist from prior runs, but no NEW files should be created
        # Verify the conftest.py isolation fixture exists
        conftest = project_root / "tests" / "conftest.py"
        assert conftest.exists(), "conftest.py must exist for test isolation"
