"""
PlanItemStatusCallbackClient — sends per-device-per-task status callbacks.

Payload format (strict, 6 fields only):
  {planId, deviceName, taskName, status, updater, errorMessage}
"""
from __future__ import annotations
import json
import logging
from typing import Any
from dataclasses import dataclass, field

logger = logging.getLogger("bmc_auto_capture.plan_item_cb")


@dataclass
class FakeCallbackTransport:
    """In-memory transport for testing."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    _simulate_failure: bool = False

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append({"url": url, "payload": dict(payload)})
        if self._simulate_failure:
            return 500, '{"error":"simulated"}'
        return 200, '{"ok":true}'

    def set_failure(self):
        self._simulate_failure = True


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


class PlanItemStatusCallbackClient:
    """Sends per-item status callbacks to itemStatusUrl."""

    def __init__(self, transport: Any = None):
        self._transport = transport or FakeCallbackTransport()

    @property
    def transport(self):
        return self._transport

    def send(self, url: str, plan_id: int | str, device_name: str, task_name: str,
             status: str, updater: str = "downstream-system",
             error_message: str | None = None,
             excel_hash: str | None = None) -> bool:
        payload = {
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
