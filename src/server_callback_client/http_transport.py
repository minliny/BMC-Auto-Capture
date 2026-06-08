"""
HttpCallbackTransport — real HTTP POST via stdlib urllib.request.
No external dependencies. Never logs auth tokens or credentials.
"""

from __future__ import annotations
import json
import logging
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger("bmc_auto_capture.callback.http")

_REDACTED = "***REDACTED***"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with sensitive values redacted for logging."""
    sensitive = {"authorization", "x-auth-token", "cookie", "set-cookie"}
    return {
        k: _REDACTED if k.lower() in sensitive else v
        for k, v in headers.items()
    }


def _redact_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of payload safe for logging (no tokens/secrets)."""
    safe = dict(payload)
    for key in list(safe.keys()):
        if any(s in key.lower() for s in ("password", "secret", "token", "credential")):
            safe[key] = _REDACTED
    if "error" in safe and isinstance(safe["error"], dict):
        safe["error"] = dict(safe["error"])
    return safe


class HttpCallbackTransport:
    """Real HTTP callback transport using stdlib urllib.

    Compatible with the CallbackTransport protocol used by ServerCallbackClient.
    """

    def __init__(self, timeout_seconds: float = 30.0):
        self._timeout = timeout_seconds

    def post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, str]:
        """POST JSON payload to url. Returns (status_code, response_body)."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req_headers = {
            "Content-Type": "application/json; charset=utf-8",
            **headers,
        }

        safe_headers = _redact_headers(req_headers)
        safe_payload = _redact_payload_for_log(payload)
        logger.debug(
            "Callback POST url=%s payload=%s headers=%s",
            url, json.dumps(safe_payload), safe_headers,
        )

        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                logger.debug("Callback response: status=%d body=%s", status, body[:500])
                return status, body
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning(
                "Callback HTTP error: status=%d url=%s body=%s",
                e.code, url, body[:300],
            )
            return e.code, body
        except urllib.error.URLError as e:
            logger.error("Callback URL/network error: url=%s reason=%s", url, e.reason)
            return 0, str(e.reason)
        except Exception as e:
            logger.error("Callback unexpected error: url=%s error=%s", url, e)
            return 0, str(e)
