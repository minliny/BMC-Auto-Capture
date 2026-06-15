"""
PlanItemStatusCallbackClient — sends per-device-per-task status callbacks.

Supports two modes:
  - batch  (default): POST {planId, items: [{planId, deviceGroup, deviceName, taskName, status, updater, errorMessage}, ...]}
  - single:          POST {planId, deviceGroup, deviceName, taskName, status, updater, errorMessage} per item
  - summary:         POST {planId, summary} after the plan batch finishes

Server response format:
  {"code": 0, "message": "success", "data": {"total": N, "success": N, "failed": 0, "errors": []}}

Status mapping (internal → server):
  PENDING  → PENDING
  RUNNING  → IN_PROGRESS
  SUCCESS  → SUCCESS
  FAILED   → FAILED
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..utils.sensitive import (
    redact_nested_payload,
    redact_sensitive_text,
    redact_url_for_log,
)


# ---------------------------------------------------------------------------
# Allowed callback body fields (whitelist)
# ---------------------------------------------------------------------------

_ALLOWED_CALLBACK_FIELDS = frozenset({
    "planId", "deviceGroup", "deviceName", "taskName",
    "status", "updater", "errorMessage",
    "startedAt", "finishedAt",
})

logger = logging.getLogger("bmc_auto_capture.plan_item_cb")


def validate_callback_url(url: str) -> tuple[bool, str]:
    """Validate callback URL before the executor performs an HTTP POST.

    Default policy:
      - allow only http/https
      - reject URLs with userinfo
      - require a parseable host
      - allow DNS names and literal IPs, including private intranet addresses
    """
    if not url:
        return True, ""
    try:
        parsed = urlsplit(url)
    except Exception:
        return False, "CALLBACK_INVALID_URL"
    if parsed.scheme not in ("http", "https"):
        return False, "CALLBACK_INVALID_SCHEME"
    try:
        hostname = parsed.hostname
    except ValueError:
        return False, "CALLBACK_INVALID_URL"
    if not hostname:
        return False, "CALLBACK_HOST_REQUIRED"
    if parsed.username or parsed.password:
        return False, "CALLBACK_USERINFO_FORBIDDEN"

    return True, ""


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

_STATUS_TO_SERVER: dict[str, str] = {
    "PENDING": "PENDING",
    "RUNNING": "IN_PROGRESS",       # Legacy compat (PlanItem now uses IN_PROGRESS natively)
    "IN_PROGRESS": "IN_PROGRESS",   # Identity for PlanItem server-aligned status
    "SUCCESS": "SUCCESS",
    "FAILED": "FAILED",
}


def map_status_to_server(internal_status: str) -> str:
    """Map internal status to server-expected status value.

    Returns the mapped status, or raises ValueError for unknown input.
    """
    mapped = _STATUS_TO_SERVER.get(internal_status.upper() if internal_status else "")
    if mapped is None:
        raise ValueError(
            f"CALLBACK_STATUS_MAPPING_ERROR: unknown internal status {internal_status!r}"
        )
    return mapped


# ---------------------------------------------------------------------------
# Callback result
# ---------------------------------------------------------------------------

@dataclass
class CallbackResult:
    """Aggregated result of one or more callback POSTs."""
    total: int = 0
    success: int = 0
    failed: int = 0
    batches: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.last_error is None


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

@dataclass
class FakeCallbackTransport:
    """In-memory transport for testing."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    _simulate_failure: bool = False
    _simulate_status: int = 200
    _simulate_body: str = '{"code":0,"message":"success","data":{"total":0,"success":0,"failed":0,"errors":[]}}'

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append({"url": url, "payload": dict(payload), "headers": dict(headers)})
        if self._simulate_failure:
            return 500, '{"error":"simulated"}'
        return self._simulate_status, self._simulate_body

    def set_failure(self):
        self._simulate_failure = True

    def configure_response(self, status: int = 200, body: str = ""):
        self._simulate_status = status
        self._simulate_body = body


class HttpCallbackTransport:
    """Real HTTP transport via stdlib urllib."""

    def __init__(self, timeout_seconds: float = 30.0):
        self._timeout = timeout_seconds

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        ok, reason = validate_callback_url(url)
        if not ok:
            return 0, reason
        import urllib.request
        import urllib.error
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return e.code, body
        except Exception as e:
            return 0, redact_sensitive_text(str(e))


# ---------------------------------------------------------------------------
# Callback client
# ---------------------------------------------------------------------------

class PlanItemStatusCallbackClient:
    """Sends per-item status callbacks to itemStatusUrl.

    Two send modes:
      - send_batch():  batch  payload {items: [...]}   (chunked at 1000)
      - send_single(): single payload {planId, deviceName, taskName, ...}
    """

    MAX_BATCH_SIZE = 1000

    def __init__(self, transport: Any = None):
        self._transport = transport or FakeCallbackTransport()

    @property
    def transport(self):
        return self._transport

    # ------------------------------------------------------------------
    # Low-level send (kept for backward compat — single-item with excelHash)
    # ------------------------------------------------------------------

    def send(self, url: str, plan_id: int | str, device_name: str, task_name: str,
             status: str, updater: str = "downstream-system",
             error_message: str | None = None,
             excel_hash: str | None = None) -> bool:
        """Legacy single-item send.  Prefer send_batch() or send_single().

        NOTE: excel_hash parameter is accepted for backward compatibility only
        but is NEVER serialized to the server callback body.  The callback body
        contains the canonical task identity/status fields.  See
        build_callback_item() for the current callback item shape.
        """
        # P0-5: excel_hash is deprecated compatibility only; never sent to server.
        _ = excel_hash  # explicitly ignored
        payload = _sanitize_callback_item({
            "planId": plan_id,
            "deviceName": device_name,
            "taskName": task_name,
            "status": status,
            "updater": updater,
            "errorMessage": error_message,
        })
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, _body = self._transport.post(url, payload, headers)
            ok = 200 <= code < 300
            if not ok:
                logger.warning(
                    "Plan item callback failed: status=%d url=%s",
                    code,
                    redact_url_for_log(url),
                )
            return ok
        except Exception as e:
            logger.error(
                "Plan item callback exception: %s",
                redact_sensitive_text(str(e)),
            )
            return False

    # ------------------------------------------------------------------
    # Batch send
    # ------------------------------------------------------------------

    def send_batch(self, url: str, items: list[dict[str, Any]],
                   max_batch_size: int | None = None,
                   run_id: str = "", summary: dict[str, Any] | None = None) -> CallbackResult:
        """Send items as batch {planId, items, summary}.

        - If items is empty: returns empty CallbackResult (no POST).
        - Chunks at max_batch_size (default 1000) if needed.
        - Aggregates results from all chunks.
        - run_id is accepted for backward-compatible callers but is not
          serialized into the server-facing payload.
        """
        if max_batch_size is None:
            max_batch_size = self.MAX_BATCH_SIZE

        if not items:
            logger.info("send_batch: no items to send (empty list)")
            return CallbackResult(total=0, success=0, failed=0, batches=0)

        chunks = [items[i:i + max_batch_size] for i in range(0, len(items), max_batch_size)]
        agg = CallbackResult(total=len(items), batches=len(chunks))

        for ci, chunk in enumerate(chunks):
            plan_ids = {str(it.get("planId", "")) for it in chunk}
            if len(plan_ids) > 1:
                logger.warning(
                    "send_batch chunk %d/%d: multiple planIds in batch: %s",
                    ci + 1, len(chunks), sorted(plan_ids),
                )

            # Include summary in every chunk so receivers can process chunks
            # independently when a large plan is split.
            chunk_summary = summary
            result = self._post_batch(url, chunk, run_id=run_id, summary=chunk_summary)
            agg.success += result.success
            agg.failed += result.failed
            if result.errors:
                agg.errors.extend(result.errors)
            if result.last_error:
                agg.last_error = result.last_error

        return agg

    def _post_batch(self, url: str, items: list[dict[str, Any]],
                    run_id: str = "", summary: dict[str, Any] | None = None) -> CallbackResult:
        """POST a single batch and parse the server response.

        All items are sanitized to the allowed callback fields.
        The payload includes planId, items, and optionally summary at the top level.
        run_id is intentionally ignored: planId is the public batch identifier.
        """
        _ = run_id
        sanitized_items = [_sanitize_callback_item(it) for it in items]
        payload: dict[str, Any] = {"items": sanitized_items}
        # Add planId at top level from first item (all items share the same planId)
        if sanitized_items and "planId" in sanitized_items[0]:
            payload["planId"] = sanitized_items[0]["planId"]
        if summary is not None:
            payload["summary"] = summary
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, body = self._transport.post(url, payload, headers)
        except Exception as e:
            safe_error = redact_sensitive_text(str(e))
            logger.error("Batch callback transport exception: %s", safe_error)
            return CallbackResult(
                total=len(items), failed=len(items), batches=1,
                last_error=f"CALLBACK_TRANSPORT_ERROR: {safe_error}",
            )
        return self._parse_server_response(code, body, len(items))

    def send_summary(self, url: str, plan_id: int | str,
                     summary: dict[str, Any]) -> CallbackResult:
        """Send the final plan summary as {planId, summary}.

        The summary callback has no item directory fields.  It is intentionally
        keyed only by planId so schedulers do not need to understand runId.
        """
        payload = {"planId": str(plan_id), "summary": dict(summary)}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, body = self._transport.post(url, payload, headers)
        except Exception as e:
            safe_error = redact_sensitive_text(str(e))
            logger.error("Summary callback transport exception: %s", safe_error)
            return CallbackResult(
                total=1, failed=1, batches=1,
                last_error=f"CALLBACK_TRANSPORT_ERROR: {safe_error}",
            )
        return self._parse_server_response(code, body, 1)

    # ------------------------------------------------------------------
    # Single send
    # ------------------------------------------------------------------

    def send_single(self, url: str, item: dict[str, Any]) -> CallbackResult:
        """Send a single item with the allowed callback fields.

        The item dict may contain: planId, deviceGroup, deviceName, taskName,
        status, updater, errorMessage, startedAt, finishedAt.
        Extra fields beyond _ALLOWED_CALLBACK_FIELDS are stripped via
        _sanitize_callback_item().
        """
        headers = {"Content-Type": "application/json; charset=utf-8"}
        sanitized = _sanitize_callback_item(item)
        try:
            code, body = self._transport.post(url, sanitized, headers)
        except Exception as e:
            safe_error = redact_sensitive_text(str(e))
            logger.error("Single callback transport exception: %s", safe_error)
            return CallbackResult(
                total=1, failed=1, batches=1,
                last_error=f"CALLBACK_TRANSPORT_ERROR: {safe_error}",
            )
        return self._parse_server_response(code, body, 1)

    # ------------------------------------------------------------------
    # Server response parsing
    # ------------------------------------------------------------------

    def _parse_server_response(self, status_code: int, body: str,
                                expected_count: int) -> CallbackResult:
        """Parse server response into CallbackResult with error classification."""
        safe_body = redact_sensitive_text(body)

        # --- HTTP non-2xx ---
        if not (200 <= status_code < 300):
            classification = self._classify_http_error(status_code, safe_body)
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error=classification,
                errors=[{"reason": classification, "httpStatus": status_code, "body": safe_body[:500]}],
            )

        # --- Parse JSON ---
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error=f"CALLBACK_PARSE_ERROR: {e}",
                errors=[{"reason": "CALLBACK_PARSE_ERROR", "body": safe_body[:500]}],
            )

        if not isinstance(data, dict):
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error="CALLBACK_PARSE_ERROR: response is not a JSON object",
            )

        # --- code != 0 ---
        code_val = data.get("code", -1)
        if code_val != 0:
            msg = redact_sensitive_text(str(data.get("message", "")))
            classification = self._classify_error_message(msg)
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error=classification,
                errors=[{"reason": classification, "code": code_val, "message": msg}],
            )

        # --- code == 0 ---
        info = data.get("data", {})
        if not isinstance(info, dict):
            info = {}
        server_total = info.get("total", expected_count)
        server_success = info.get("success", 0)
        server_failed = info.get("failed", 0)
        server_errors = redact_nested_payload(info.get("errors", []))

        result = CallbackResult(
            total=server_total,
            success=server_success,
            failed=server_failed,
            batches=1,
            errors=list(server_errors) if isinstance(server_errors, list) else [],
        )

        if server_failed > 0:
            result.last_error = "CALLBACK_PARTIAL_FAILURE"
            logger.warning(
                "Batch callback partial failure: total=%d success=%d failed=%d",
                server_total, server_success, server_failed,
            )

        return result

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_http_error(status_code: int, body: str) -> str:
        """Classify non-2xx HTTP errors."""
        if status_code == 400:
            return PlanItemStatusCallbackClient._classify_error_message(body)
        if status_code == 0:
            return "CALLBACK_HTTP_ERROR: network/transport failure"
        return f"CALLBACK_HTTP_ERROR: HTTP {status_code}"

    @staticmethod
    def _classify_error_message(text: str) -> str:
        """Classify error by message content."""
        lower = text.lower() if text else ""
        if "batch size exceeds maximum" in lower or "batch size exceeds" in lower:
            return "CALLBACK_BATCH_TOO_LARGE"
        if "all items must belong to the same plan" in lower:
            return "CALLBACK_PLAN_ID_MISMATCH"
        if "items list cannot be empty" in lower or "items cannot be empty" in lower:
            return "CALLBACK_EMPTY_ITEMS"
        if "not found" in lower:
            return "CALLBACK_NOT_FOUND"
        return f"CALLBACK_SERVER_ERROR: {text[:200]}"


# ---------------------------------------------------------------------------
# Helper: build a single callback item dict (used by PlanRunService)
# ---------------------------------------------------------------------------

def build_callback_item(plan_id: str, device_name: str, task_name: str,
                         status: str, updater: str = "downstream-system",
                         error_message: str | None = None,
                         started_at: str | None = None,
                         finished_at: str | None = None,
                         device_group: str = "") -> dict[str, Any]:
    """Build a single callback item dict with server-mapped status.

    The returned dict contains up to 9 fields:
      {planId, deviceGroup, deviceName, taskName, status, updater, errorMessage,
       startedAt, finishedAt}

    No excelHash, jobId, password, token, or secret is included.
    """
    item = _sanitize_callback_item({
        "planId": str(plan_id),
        "deviceGroup": device_group,
        "deviceName": device_name,
        "taskName": task_name,
        "status": map_status_to_server(status),
        "updater": updater,
        "errorMessage": error_message,
    })
    if started_at is not None:
        item["startedAt"] = started_at
    if finished_at is not None:
        item["finishedAt"] = finished_at
    return item


def _redact_sensitive_value(text: str) -> str:
    """Redact sensitive values from a string, preferring JSON-structured redaction."""
    if not text:
        return text or ""
    stripped = text.strip()
    if stripped and stripped[0] in ('{', '['):
        try:
            parsed = json.loads(stripped)
            redacted = redact_nested_payload(parsed)
            return json.dumps(redacted, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return redact_sensitive_text(text)


def _sanitize_callback_item(item: dict[str, Any]) -> dict[str, Any]:
    """Enforce the public callback item schema: discard extra fields.

    Allowed fields: planId, deviceGroup, deviceName, taskName, status,
    updater, errorMessage, startedAt, finishedAt.

    Any field not in _ALLOWED_CALLBACK_FIELDS is silently dropped.
    This prevents accidental leakage of excelHash/runId/storedPath/metadata
    into the callback body, even if the caller passes extra data.
    """
    result: dict[str, Any] = {}
    for field in _ALLOWED_CALLBACK_FIELDS:
        if field in item:
            result[field] = item[field]
        elif field == "errorMessage":
            result[field] = None
        else:
            result[field] = ""
    # Ensure planId is always a string
    if "planId" in result and result["planId"] is not None:
        result["planId"] = str(result["planId"])
    if result.get("errorMessage") is not None:
        result["errorMessage"] = _redact_sensitive_value(str(result["errorMessage"]))
    result["status"] = map_status_to_server(str(result.get("status") or ""))
    return result
