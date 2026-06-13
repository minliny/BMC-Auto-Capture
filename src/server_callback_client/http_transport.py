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
    """Return a copy of payload safe for logging (no tokens/secrets).

    Uses recursive redaction via redact_nested_payload for deep coverage.
    """
    from ..utils.sensitive import redact_nested_payload
    return redact_nested_payload(payload)


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
        from ..utils.sensitive import redact_sensitive_text, redact_sensitive_url
        logger.debug(
            "Callback POST url=%s payload=%s headers=%s",
            redact_sensitive_url(url), json.dumps(safe_payload), safe_headers,
        )

        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                logger.debug(
                    "Callback response: status=%d body=%s",
                    status,
                    redact_sensitive_text(body)[:500],
                )
                return status, body
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning(
                "Callback HTTP error: status=%d url=%s body=%s",
                e.code, redact_sensitive_url(url), redact_sensitive_text(body)[:300],
            )
            return e.code, body
        except urllib.error.URLError as e:
            logger.error(
                "Callback URL/network error: url=%s reason=%s",
                redact_sensitive_url(url),
                redact_sensitive_text(str(e.reason)),
            )
            return 0, redact_sensitive_text(str(e.reason))
        except Exception as e:
            logger.error(
                "Callback unexpected error: url=%s error=%s",
                redact_sensitive_url(url),
                redact_sensitive_text(str(e)),
            )
            return 0, redact_sensitive_text(str(e))
