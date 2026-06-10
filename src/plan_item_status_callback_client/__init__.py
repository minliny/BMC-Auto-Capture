"""
PlanItemStatusCallbackClient — sends per-device-per-task status callbacks.

Supports two modes:
  - batch  (default): POST {items: [{planId, deviceName, taskName, status, updater, errorMessage}, ...]}
  - single:          POST {planId, deviceName, taskName, status, updater, errorMessage} per item

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

logger = logging.getLogger("bmc_auto_capture.plan_item_cb")


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

_STATUS_TO_SERVER: dict[str, str] = {
    "PENDING": "PENDING",
    "RUNNING": "IN_PROGRESS",
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
            return 0, str(e)


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
        """Legacy single-item send.  Prefer send_batch() or send_single()."""
        payload: dict[str, Any] = {
            "planId": plan_id,
            "deviceName": device_name,
            "taskName": task_name,
            "status": status,
            "updater": updater,
            "errorMessage": error_message,
        }
        if excel_hash:
            payload["excelHash"] = excel_hash
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, _body = self._transport.post(url, payload, headers)
            ok = 200 <= code < 300
            if not ok:
                logger.warning("Plan item callback failed: status=%d url=%s", code, url)
            return ok
        except Exception as e:
            logger.error("Plan item callback exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # Batch send
    # ------------------------------------------------------------------

    def send_batch(self, url: str, items: list[dict[str, Any]],
                   max_batch_size: int | None = None) -> CallbackResult:
        """Send items as batch {items: [...]}.

        - If items is empty: returns empty CallbackResult (no POST).
        - Chunks at max_batch_size (default 1000) if needed.
        - Aggregates results from all chunks.
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

            result = self._post_batch(url, chunk)
            agg.success += result.success
            agg.failed += result.failed
            if result.errors:
                agg.errors.extend(result.errors)
            if result.last_error:
                agg.last_error = result.last_error

        return agg

    def _post_batch(self, url: str, items: list[dict[str, Any]]) -> CallbackResult:
        """POST a single batch and parse the server response."""
        payload = {"items": items}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, body = self._transport.post(url, payload, headers)
        except Exception as e:
            logger.error("Batch callback transport exception: %s", e)
            return CallbackResult(
                total=len(items), failed=len(items), batches=1,
                last_error=f"CALLBACK_TRANSPORT_ERROR: {e}",
            )
        return self._parse_server_response(code, body, len(items))

    # ------------------------------------------------------------------
    # Single send
    # ------------------------------------------------------------------

    def send_single(self, url: str, item: dict[str, Any]) -> CallbackResult:
        """Send a single item as {planId, deviceName, taskName, status, updater, errorMessage}."""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            code, body = self._transport.post(url, dict(item), headers)
        except Exception as e:
            logger.error("Single callback transport exception: %s", e)
            return CallbackResult(
                total=1, failed=1, batches=1,
                last_error=f"CALLBACK_TRANSPORT_ERROR: {e}",
            )
        return self._parse_server_response(code, body, 1)

    # ------------------------------------------------------------------
    # Server response parsing
    # ------------------------------------------------------------------

    def _parse_server_response(self, status_code: int, body: str,
                                expected_count: int) -> CallbackResult:
        """Parse server response into CallbackResult with error classification."""

        # --- HTTP non-2xx ---
        if not (200 <= status_code < 300):
            classification = self._classify_http_error(status_code, body)
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error=classification,
                errors=[{"reason": classification, "httpStatus": status_code, "body": body[:500]}],
            )

        # --- Parse JSON ---
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error=f"CALLBACK_PARSE_ERROR: {e}",
                errors=[{"reason": "CALLBACK_PARSE_ERROR", "body": body[:500]}],
            )

        if not isinstance(data, dict):
            return CallbackResult(
                total=expected_count, failed=expected_count, batches=1,
                last_error="CALLBACK_PARSE_ERROR: response is not a JSON object",
            )

        # --- code != 0 ---
        code_val = data.get("code", -1)
        if code_val != 0:
            msg = data.get("message", "")
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
        server_errors = info.get("errors", [])

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
                         error_message: str | None = None) -> dict[str, Any]:
    """Build a single callback item dict with server-mapped status.

    The returned dict contains exactly 6 fields:
      {planId, deviceName, taskName, status, updater, errorMessage}

    No excelHash, runId, jobId, password, token, or secret is included.
    """
    return {
        "planId": str(plan_id),
        "deviceName": device_name,
        "taskName": task_name,
        "status": map_status_to_server(status),
        "updater": updater,
        "errorMessage": error_message,
    }
