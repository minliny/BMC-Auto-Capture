"""
CallbackOutbox — persistent outbox for plan-item status callbacks.

Writes callback items to executor_state/plans/{planId}/callback_outbox.jsonl
before attempting delivery.  Delivery failures do NOT affect local task status.

Statuses:
  PENDING             — written to outbox, not yet attempted
  SENDING             — delivery in progress
  SENT                — successfully delivered
  FAILED_RETRYABLE    — transient failure, can retry
  FAILED_FINAL        — permanent failure, no more retries
  URL_NOT_CONFIGURED  — no callback URL resolved, item kept for reference

Design rules:
  - Outbox items contain exactly the 6 callback fields + delivery metadata.
  - password/token/secret/Authorization/excelHash/configId/storedPath are FORBIDDEN.
  - Local plan/run status is never changed by callback failure.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bmc_auto_capture.callback_outbox")

_OUTBOX_LOCKS: dict[str, threading.RLock] = {}
_OUTBOX_LOCKS_GUARD = threading.Lock()


def _get_outbox_lock(path: str) -> threading.RLock:
    normalized = os.path.abspath(os.path.normpath(path))
    with _OUTBOX_LOCKS_GUARD:
        lock = _OUTBOX_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _OUTBOX_LOCKS[normalized] = lock
        return lock


# ---------------------------------------------------------------------------
# Sensitive value redaction for outbox persistence (P0-3)
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[re.Pattern] = [
    # Authorization headers
    re.compile(r'(Authorization\s*:\s*)(Bearer\s+\S+|Basic\s+\S+)', re.IGNORECASE),
    # URL query params with sensitive keys
    re.compile(r'((?:[?&])(?:token|secret|api_key|access_token|refresh_token|password|passwd|pwd)=)[^&]+'),
    # URL userinfo (user:pass@host)
    re.compile(r'(://)[^@]+@'),
    # JSON key-value patterns in error messages
    re.compile(r'("?(?:password|passwd|pwd|token|secret|api_key|access_token|refresh_token)"?\s*[=:]\s*")[^"]+(")'),
    re.compile(r"('?(?:password|passwd|pwd|token|secret|api_key|access_token|refresh_token)'?\s*[=:]\s*')[^']+(')"),
    # Plaintext password= / token= / secret= patterns
    re.compile(r'((?:password|passwd|pwd|token|secret|api_key|access_token|refresh_token)\s*[=:]\s*)\S+', re.IGNORECASE),
]


def _redact_sensitive(text: str) -> str:
    """Redact sensitive values from a string (URLs, tokens, passwords, auth headers).

    Returns text with sensitive values replaced by ***REDACTED***.
    """
    if not text:
        return text or ""
    from ..utils.sensitive import redact_nested_payload, redact_sensitive_text, redact_sensitive_url

    # Structured JSON redaction first — ensures sensitive key VALUES are redacted,
    # not the keys themselves.  Fixes opaque secret leak where regex-based
    # redaction would replace the key "token" instead of the value "Q7v9Z2m4N8x6".
    stripped = text.strip()
    if stripped and stripped[0] in ('{', '['):
        try:
            parsed = json.loads(stripped)
            redacted = redact_nested_payload(parsed)
            return json.dumps(redacted, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    # Fallback: text-based redaction for non-JSON strings
    result = redact_sensitive_url(text) if "://" in text else redact_sensitive_text(text)
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub(r'\1***REDACTED***', result)
    return redact_sensitive_text(result)


def _safe_plan_id(plan_id: str) -> str:
    """Sanitize plan_id for use as a directory name.

    Raises ValueError if plan_id is empty or contains path traversal characters.
    """
    # P0-2: reject empty, traversal, absolute paths, drive letters
    if not plan_id or not plan_id.strip():
        raise ValueError("INVALID_PLAN_ID_FOR_OUTBOX_PATH: plan_id is empty")
    if ".." in plan_id:
        raise ValueError(f"INVALID_PLAN_ID_FOR_OUTBOX_PATH: plan_id contains path traversal: {plan_id!r}")
    if plan_id.startswith("/") or plan_id.startswith("\\"):
        raise ValueError(f"INVALID_PLAN_ID_FOR_OUTBOX_PATH: plan_id is an absolute path: {plan_id!r}")
    if re.match(r'^[A-Za-z]:[/\\]', plan_id):
        raise ValueError(f"INVALID_PLAN_ID_FOR_OUTBOX_PATH: plan_id contains drive letter: {plan_id!r}")
    separators = [s for s in (os.sep, os.altsep) if s]
    if any(s in plan_id for s in separators):
        raise ValueError(f"INVALID_PLAN_ID_FOR_OUTBOX_PATH: plan_id contains path separator: {plan_id!r}")
    return plan_id

# ---------------------------------------------------------------------------
# Delivery status constants
# ---------------------------------------------------------------------------

PENDING = "PENDING"
SENDING = "SENDING"
SENT = "SENT"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED_FINAL = "FAILED_FINAL"
URL_NOT_CONFIGURED = "URL_NOT_CONFIGURED"

# Retryable error classifications (by callback client error prefix)
_RETRYABLE_ERRORS = {
    "CALLBACK_HTTP_ERROR",
    "CALLBACK_TRANSPORT_ERROR",
    "CALLBACK_REGISTRY_RESOLVE_FAILED",
    "CALLBACK_REGISTRY_HTTP_ERROR",
}

# Non-retryable (permanent) error classifications
_NON_RETRYABLE_ERRORS = {
    "CALLBACK_URL_NOT_CONFIGURED",
    "CALLBACK_PARSE_ERROR",
    "CALLBACK_BATCH_TOO_LARGE",
    "CALLBACK_PLAN_ID_MISMATCH",
    "CALLBACK_EMPTY_ITEMS",
    "CALLBACK_NOT_FOUND",
    "CALLBACK_SERVER_ERROR",
    "CALLBACK_PLAN_ID_MISSING",
}

MAX_RETRY_ATTEMPTS = 5
RETRY_BACKOFF_BASE = 5.0  # seconds


# ---------------------------------------------------------------------------
# Outbox item
# ---------------------------------------------------------------------------


@dataclass
class CallbackOutboxItem:
    """One callback item in the outbox plus delivery metadata."""

    # --- Callback body fields ---
    plan_id: str
    device_name: str
    task_name: str
    status: str
    task_id: str = ""
    plan_item_id: str = ""
    device_group: str = ""
    updater: str = "downstream-system"
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    payload_type: str = "item"  # "item" | "summary"
    summary: dict[str, Any] | None = None

    # --- Delivery metadata (NEVER sent to server) ---
    outbox_id: str = ""
    callback_url: str = ""
    delivery_status: str = PENDING
    attempt_count: int = 0
    last_error_code: int = 0
    last_error_message: str | None = None
    next_retry_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.outbox_id:
            self.outbox_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()

    def to_callback_body(self) -> dict[str, Any]:
        """Return the server-facing callback fields. No delivery metadata leaks."""
        if self.payload_type == "summary":
            return {
                "planId": str(self.plan_id),
                "summary": dict(self.summary or {}),
            }
        body = {
            "planId": str(self.plan_id),
            "taskId": self.task_id,
            "planItemId": self.plan_item_id,
            "deviceGroup": self.device_group,
            "deviceName": self.device_name,
            "taskName": self.task_name,
            "status": self.status,
            "updater": self.updater,
            "errorMessage": _redact_sensitive(self.error_message) if self.error_message else None,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
        }
        return body

    def to_outbox_dict(self) -> dict[str, Any]:
        """Full outbox record for persistence (delivery metadata included).

        P0-3: sensitive values (tokens, passwords, secrets, auth headers) are
        redacted from callbackUrl, errorMessage, and lastErrorMessage before
        being written to the jsonl file.
        """
        record = {
            "outboxId": self.outbox_id,
            "planId": str(self.plan_id),
            "taskId": self.task_id,
            "planItemId": self.plan_item_id,
            "deviceGroup": self.device_group,
            "deviceName": self.device_name,
            "taskName": self.task_name,
            "status": self.status,
            "updater": self.updater,
            "errorMessage": _redact_sensitive(self.error_message) if self.error_message else None,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "callbackUrl": _redact_sensitive(self.callback_url),
            "deliveryStatus": self.delivery_status,
            "attemptCount": self.attempt_count,
            "lastErrorCode": self.last_error_code,
            "lastErrorMessage": _redact_sensitive(self.last_error_message) if self.last_error_message else None,
            "nextRetryAt": self.next_retry_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.payload_type != "item":
            record["payloadType"] = self.payload_type
            record["summary"] = dict(self.summary or {})
        return record

    @classmethod
    def from_outbox_dict(cls, d: dict[str, Any]) -> CallbackOutboxItem:
        return cls(
            plan_id=str(d.get("planId", "")),
            device_group=str(d.get("deviceGroup", "")),
            device_name=str(d.get("deviceName", "")),
            task_name=str(d.get("taskName", "")),
            status=str(d.get("status", "")),
            task_id=str(d.get("taskId", "")),
            plan_item_id=str(d.get("planItemId", "")),
            updater=str(d.get("updater", "downstream-system")),
            error_message=d.get("errorMessage"),
            started_at=d.get("startedAt"),
            finished_at=d.get("finishedAt"),
            payload_type=str(d.get("payloadType", "item") or "item"),
            summary=d.get("summary") if isinstance(d.get("summary"), dict) else None,
            outbox_id=str(d.get("outboxId", "")),
            callback_url=str(d.get("callbackUrl", "")),
            delivery_status=str(d.get("deliveryStatus", PENDING)),
            attempt_count=int(d.get("attemptCount", 0)),
            last_error_code=int(d.get("lastErrorCode", 0)),
            last_error_message=d.get("lastErrorMessage"),
            next_retry_at=float(d.get("nextRetryAt", 0.0)),
            created_at=float(d.get("createdAt", 0.0)),
            updated_at=float(d.get("updatedAt", 0.0)),
        )


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


class CallbackOutbox:
    """Persistent outbox for callback items.

    Thread-safe for concurrent append + read.  Each plan gets its own outbox file:
      executor_state/plans/{planId}/callback_outbox.jsonl
        """

    def __init__(
        self,
        plan_id: str,
        outbox_dir: str | None = None,
        workspace_root: str | None = None,
    ):
        safe_id = _safe_plan_id(plan_id)
        self._plan_id = safe_id
        if outbox_dir:
            self._outbox_dir = outbox_dir
        else:
            if workspace_root is None:
                from ..excel_config_store import _resolve_workspace
                ws = _resolve_workspace()
            else:
                from pathlib import Path
                ws = Path(workspace_root).resolve()
            base = str(ws / "executor_state" / "plans")
            self._outbox_dir = os.path.join(base, safe_id)
        # P0-2: verify containment — outbox path must resolve under executor_state/plans
        self._outbox_path = os.path.join(self._outbox_dir, "callback_outbox.jsonl")
        resolved = os.path.abspath(os.path.normpath(self._outbox_path))
        expected_base = os.path.abspath(os.path.normpath(
            outbox_dir if outbox_dir
            else str(ws / "executor_state" / "plans")
        ))
        if not resolved.startswith(expected_base + os.sep) and resolved != expected_base:
            raise ValueError(
                f"INVALID_PLAN_ID_FOR_OUTBOX_PATH: {resolved!r} escapes base {expected_base!r}"
            )
        self._lock = _get_outbox_lock(resolved)
        os.makedirs(self._outbox_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, item: CallbackOutboxItem) -> CallbackOutboxItem:
        """Append an item to the outbox file. Returns the item with outbox_id set."""
        with self._lock:
            item.updated_at = time.time()
            record = item.to_outbox_dict()
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with open(self._outbox_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        return item

    def append_batch(self, items: list[CallbackOutboxItem]) -> list[CallbackOutboxItem]:
        """Append multiple items in one flush.

        P0-3: all sensitive values are redacted via to_outbox_dict() before write.
        """
        with self._lock:
            now = time.time()
            lines = []
            for item in items:
                item.updated_at = now
                record = item.to_outbox_dict()
                lines.append(json.dumps(record, ensure_ascii=False) + "\n")
            with open(self._outbox_path, "a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
        return items

    def mark_sent(self, outbox_id: str) -> bool:
        """Mark an item as successfully delivered."""
        return self._update_item(outbox_id, delivery_status=SENT)

    def mark_failed(
        self, outbox_id: str, error_message: str,
        error_code: int = 0, retryable: bool = True,
    ) -> bool:
        """Mark an item as failed, with retry classification.

        Atomic: read-modify-write is done within a single lock scope
        to prevent concurrent mark_failed calls from losing attemptCount.
        """
        with self._lock:
            all_items = self._read_all_locked()
            found = False
            for i, item in enumerate(all_items):
                if item.outbox_id == outbox_id:
                    item.attempt_count += 1
                    item.last_error_code = error_code
                    item.last_error_message = error_message
                    if not retryable or item.attempt_count >= MAX_RETRY_ATTEMPTS:
                        item.delivery_status = FAILED_FINAL
                        item.next_retry_at = 0.0
                    else:
                        item.delivery_status = FAILED_RETRYABLE
                        backoff = RETRY_BACKOFF_BASE * (2 ** (item.attempt_count - 1))
                        item.next_retry_at = time.time() + backoff
                    item.updated_at = time.time()
                    all_items[i] = item
                    found = True
                    break
            if not found:
                return False
            return self._rewrite_all_locked(all_items)

    def mark_url_not_configured(self, outbox_id: str) -> bool:
        """Mark item as URL_NOT_CONFIGURED — preserved but not sent."""
        return self._update_item(outbox_id, delivery_status=URL_NOT_CONFIGURED)

    def mark_retrying(self, outbox_id: str) -> bool:
        """Mark item as SENDING (about to retry)."""
        return self._update_item(outbox_id, delivery_status=SENDING)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_pending(self) -> list[CallbackOutboxItem]:
        """Get items that are PENDING or FAILED_RETRYABLE and due for retry."""
        all_items = self._read_all()
        now = time.time()
        return [
            it for it in all_items
            if it.delivery_status in (PENDING, FAILED_RETRYABLE)
            and (it.next_retry_at == 0.0 or it.next_retry_at <= now)
        ]

    def get_stats(self) -> dict[str, int]:
        """Return counts by delivery status."""
        all_items = self._read_all()
        stats: dict[str, int] = {}
        for it in all_items:
            stats[it.delivery_status] = stats.get(it.delivery_status, 0) + 1
        return stats

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_all(self) -> list[CallbackOutboxItem]:
        """Read all items from the outbox file. Thread-safe via self._lock."""
        with self._lock:
            return self._read_all_locked()

    def _find_item(self, outbox_id: str) -> CallbackOutboxItem | None:
        for item in self._read_all():
            if item.outbox_id == outbox_id:
                return item
        return None

    def _update_item(self, outbox_id: str, **kwargs) -> bool:
        item = self._find_item(outbox_id)
        if item is None:
            return False
        for key, value in kwargs.items():
            setattr(item, key, value)
        return self._update_existing(item)

    def _update_existing(self, item: CallbackOutboxItem) -> bool:
        """Rewrite the outbox file with the updated item. Atomic via temp+rename.

        Thread-safe via self._lock. Temp file always cleaned up.
        """
        with self._lock:
            all_items = self._read_all_locked()
            found = False
            for i, existing in enumerate(all_items):
                if existing.outbox_id == item.outbox_id:
                    item.updated_at = time.time()
                    all_items[i] = item
                    found = True
                    break
            if not found:
                return False
            return self._rewrite_all_locked(all_items)

    def _read_all_locked(self) -> list[CallbackOutboxItem]:
        """Read all items. Must be called within self._lock."""
        if not os.path.isfile(self._outbox_path):
            return []
        items: list[CallbackOutboxItem] = []
        with open(self._outbox_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    items.append(CallbackOutboxItem.from_outbox_dict(d))
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("CallbackOutbox: corrupt line in %s: %s", self._outbox_path, e)
        return items

    def _rewrite_all_locked(self, all_items: list[CallbackOutboxItem]) -> bool:
        """Rewrite the outbox file with all items. Must be called within self._lock.

        Atomic via temp+rename. Temp file always cleaned up.
        """
        os.makedirs(self._outbox_dir, exist_ok=True)
        import tempfile
        fd, tmp_path = tempfile.mkstemp(
            suffix=".jsonl", prefix=".outbox.", dir=self._outbox_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for it in all_items:
                    record = it.to_outbox_dict()
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
            os.replace(tmp_path, self._outbox_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def classify_callback_error(error_message: str | None) -> tuple[bool, str]:
    """Classify a callback error as retryable or not.

    Returns (is_retryable, error_classification).
    """
    if not error_message:
        return False, "CALLBACK_UNKNOWN_ERROR"
    msg = error_message or ""
    for prefix in _RETRYABLE_ERRORS:
        if msg.startswith(prefix):
            return True, prefix
    for prefix in _NON_RETRYABLE_ERRORS:
        if msg.startswith(prefix):
            return False, prefix
    return True, "CALLBACK_UNCLASSIFIED_ERROR"


def build_outbox_item_from_callback_body(
    plan_id: str, device_name: str, task_name: str,
    status: str, updater: str = "downstream-system",
    error_message: str | None = None,
    callback_url: str = "",
    device_group: str = "",
    task_id: str = "",
    plan_item_id: str = "",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> CallbackOutboxItem:
    """Factory: create a CallbackOutboxItem from callback fields."""
    return CallbackOutboxItem(
        plan_id=plan_id,
        device_group=device_group,
        device_name=device_name,
        task_name=task_name,
        task_id=task_id,
        plan_item_id=plan_item_id,
        status=status,
        updater=updater,
        error_message=error_message,
        callback_url=callback_url,
        started_at=started_at,
        finished_at=finished_at,
    )


def build_outbox_summary_from_callback_body(
    plan_id: str,
    summary: dict[str, Any],
    callback_url: str = "",
) -> CallbackOutboxItem:
    """Factory: create a final-summary callback outbox record."""
    return CallbackOutboxItem(
        plan_id=plan_id,
        device_group="",
        device_name="",
        task_name="",
        status="SUMMARY",
        callback_url=callback_url,
        payload_type="summary",
        summary=dict(summary),
    )
