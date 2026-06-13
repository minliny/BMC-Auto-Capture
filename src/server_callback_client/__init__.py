"""
Server callback client — calls back to server after job execution.

Supports:
  - Real HTTP POST via stdlib urllib (HttpCallbackTransport)
  - Fake transport for testing (FakeCallbackTransport)
  - Auth token in headers, never logged
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .http_transport import HttpCallbackTransport  # noqa: F401

logger = logging.getLogger("bmc_auto_capture.callback")


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------

class CallbackTransport(Protocol):
    """Protocol for sending callback HTTP requests."""

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        """POST to url. Returns (status_code, response_body)."""
        ...


# ---------------------------------------------------------------------------
# Fake transport for testing
# ---------------------------------------------------------------------------

@dataclass
class FakeCallbackTransport:
    """In-memory callback recorder for testing. Never makes real HTTP calls."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    _simulate_failure: bool = False
    _simulate_status: int = 200

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        call = {
            "url": url,
            "payload": dict(payload),
            "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},
        }
        self.calls.append(call)
        if self._simulate_failure:
            return (500, '{"error": "simulated failure"}')
        return (self._simulate_status, '{"ok": true}')

    def set_failure(self):
        self._simulate_failure = True

    def set_status(self, status: int):
        self._simulate_status = status

    @property
    def last_call(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# Callback client
# ---------------------------------------------------------------------------

class ServerCallbackClient:
    """Sends job status callbacks to the server's status_url.

    Uses a transport (real HTTP or fake) to POST status updates.
    Never logs auth tokens.
    """

    def __init__(
        self,
        executor_id: str = "exec-win-001",
        transport: CallbackTransport | None = None,
    ):
        self.executor_id = executor_id
        self._transport = transport or FakeCallbackTransport()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def callback_job_started(
        self,
        external_task_id: str,
        job_id: str,
        status_url: str,
        auth_token: str = "",
    ) -> bool:
        """Notify server that job execution has started."""
        payload = self._build_base_payload(external_task_id, job_id)
        payload["status"] = "RUNNING"
        return self._send(status_url, payload, auth_token)

    def callback_job_finished(
        self,
        external_task_id: str,
        job_id: str,
        status_url: str,
        result: dict[str, Any],
        duration_ms: int = 0,
        artifacts: list[dict[str, Any]] | None = None,
        auth_token: str = "",
    ) -> bool:
        """Notify server that job execution completed successfully."""
        payload = self._build_base_payload(external_task_id, job_id)
        payload["status"] = "SUCCEEDED"
        payload["duration_ms"] = duration_ms
        payload["result"] = result
        payload["error"] = None
        payload["artifacts"] = artifacts or []
        return self._send(status_url, payload, auth_token)

    def callback_job_failed(
        self,
        external_task_id: str,
        job_id: str,
        status_url: str,
        status: str,
        duration_ms: int = 0,
        error: dict[str, Any] | None = None,
        auth_token: str = "",
    ) -> bool:
        """Notify server that job execution failed or timed out."""
        payload = self._build_base_payload(external_task_id, job_id)
        payload["status"] = status  # FAILED or TIMEOUT
        payload["duration_ms"] = duration_ms
        payload["result"] = None
        payload["error"] = error
        payload["artifacts"] = []
        return self._send(status_url, payload, auth_token)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_base_payload(self, external_task_id: str, job_id: str) -> dict[str, Any]:
        from datetime import datetime, timezone
        return {
            "external_task_id": external_task_id,
            "job_id": job_id,
            "executor_id": self.executor_id,
            "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def _send(self, url: str, payload: dict[str, Any], auth_token: str) -> bool:
        headers = {
            "Content-Type": "application/json",
            "X-Idempotency-Key": f"{payload.get('external_task_id')}-{payload.get('job_id')}-{payload.get('status')}",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        # NEVER log the auth token
        safe_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        from ..utils.sensitive import (
            redact_nested_payload,
            redact_sensitive_text,
            redact_sensitive_url,
        )

        def _redact_body(text: str) -> str:
            """Redact response body, preferring JSON-structured redaction."""
            if not text:
                return text
            stripped = text.strip()
            if stripped and stripped[0] in ('{', '['):
                try:
                    parsed = json.loads(stripped)
                    redacted = redact_nested_payload(parsed)
                    return json.dumps(redacted, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            return redact_sensitive_text(text)

        safe_url = redact_sensitive_url(url)
        safe_payload = redact_nested_payload(payload)
        logger.debug("Callback POST %s payload=%s headers=%s", safe_url, json.dumps(safe_payload), safe_headers)

        try:
            status_code, body = self._transport.post(url, payload, headers)
            ok = 200 <= status_code < 300
            if not ok:
                logger.warning(
                    "Callback to %s failed: status=%d body=%s",
                    safe_url,
                    status_code,
                    _redact_body(body)[:200],
                )
            return ok
        except Exception as e:
            logger.error(
                "Callback to %s exception: %s",
                safe_url,
                redact_sensitive_text(str(e)),
            )
            return False
