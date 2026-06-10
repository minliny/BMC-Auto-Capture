"""
Server registry client — discovers active service endpoint via master_registry.

Priority chain (implemented in PlanRunService._resolve_callback_url):
  1. master_registry active server  (highest)
  2. request callback.itemStatusUrl
  3. env EXECUTOR_PLAN_ITEM_STATUS_URL
  4. none → CALLBACK_URL_NOT_CONFIGURED

Environment variables:
  EXECUTOR_MASTER_REGISTRY_URL   — full POST URL for master_registry
  EXECUTOR_MASTER_REGISTRY_AUTH  — Authorization header value (redacted in logs)
"""
from __future__ import annotations
import json
import logging
import os

logger = logging.getLogger("bmc_auto_capture.server_registry")

# Active value aliases (case-insensitive)
_ACTIVE_VALUES = {"true", "1", "y", "yes", "是"}


def _is_active(value: str | bool | int | None) -> bool:
    """Check if a registry record is active."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value).strip().lower() in _ACTIVE_VALUES


def discover_callback_url() -> str | None:
    """Discover the active service callback URL from master_registry.

    Returns:
        Full callback URL like http://{host_ip}:{service_port}/api/plans/items/status,
        or None if no active server found or registry is not configured.

    Raises:
        Does not raise — all errors are logged and return None.
    """
    registry_url = os.environ.get("EXECUTOR_MASTER_REGISTRY_URL", "")
    if not registry_url:
        logger.debug("Registry not configured (EXECUTOR_MASTER_REGISTRY_URL empty)")
        return None

    auth = os.environ.get("EXECUTOR_MASTER_REGISTRY_AUTH", "")

    # Build request
    payload = {
        "device_id": "",
        "host_ip": "",
        "host_name": "",
        "service_port": "",
        "active": "",
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
    }
    if auth:
        headers["Authorization"] = auth

    # --- POST ---
    import urllib.request
    import urllib.error

    redacted_auth = f"{auth[:6]}***<redacted>" if len(auth) > 6 else "<redacted>"
    logger.info("Registry request: url=%s auth=%s", registry_url, redacted_auth)

    req = urllib.request.Request(registry_url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status_code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.error("CALLBACK_REGISTRY_HTTP_ERROR: HTTP %d body=%s", e.code, body[:500])
        return None
    except Exception as e:
        logger.error("CALLBACK_REGISTRY_HTTP_ERROR: %s", e)
        return None

    # --- Parse JSON ---
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("CALLBACK_REGISTRY_PARSE_ERROR: %s body=%s", e, raw[:500])
        return None

    # --- Extract records ---
    # Response may be a list, or {data: [...]}, or a single dict
    records: list[dict] = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        # Common patterns: {"data": [...]}, {"records": [...]}, {"result": [...]}
        for key in ("data", "records", "result", "items"):
            val = data.get(key)
            if isinstance(val, list):
                records = val
                break
        if not records and all(isinstance(v, (str, int, float, bool, type(None))) for v in data.values()):
            # Single record
            records = [data]

    if not records:
        logger.warning("CALLBACK_REGISTRY_NO_ACTIVE_MASTER: no records in response")
        return None

    # --- Find active servers ---
    active_records = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        active_val = rec.get("active")
        if _is_active(active_val):
            host_ip = str(rec.get("host_ip", "")).strip()
            service_port = str(rec.get("service_port", "")).strip()
            if host_ip and service_port:
                active_records.append((host_ip, service_port))
            else:
                logger.warning(
                    "CALLBACK_REGISTRY_INVALID_RECORD: active=True but host_ip=%r service_port=%r",
                    host_ip, service_port,
                )

    if not active_records:
        logger.warning("CALLBACK_REGISTRY_NO_ACTIVE_MASTER: no active server with valid host_ip+service_port")
        return None

    if len(active_records) > 1:
        logger.warning(
            "Multiple active servers in registry (%d), using first: %s:%s",
            len(active_records), active_records[0][0], active_records[0][1],
        )

    host_ip, service_port = active_records[0]
    callback_url = f"http://{host_ip}:{service_port}/api/plans/items/status"
    logger.info(
        "Registry resolved callback URL: %s (from %s:%s)",
        callback_url, host_ip, service_port,
    )
    return callback_url
