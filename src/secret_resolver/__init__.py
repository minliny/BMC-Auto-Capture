"""
Secret resolver — lightweight credential resolution for API v0.1.

Supports:
  - env:VAR_NAME  → os.environ['VAR_NAME']
  - Plaintext placeholder → (v0.2: vault, file, etc.)

Never logs resolved values.
"""

from __future__ import annotations
import logging
import os
from typing import Any

logger = logging.getLogger("bmc_auto_capture.secret")


class SecretError(ValueError):
    """Base error for secret resolution failures."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SECRET_REF_MISSING = "SECRET_REF_MISSING"
SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
SECRET_RESOLVE_FAILED = "SECRET_RESOLVE_FAILED"


def resolve_secret(secret_ref: str) -> str:
    """Resolve a secret_ref string to its actual value.

    Rules:
      - "" or empty → SECRET_REF_MISSING
      - "env:VAR_NAME" → os.environ['VAR_NAME']
      - plain string (no prefix) → returned as-is (v0.1 placeholder mode)

    Raises SecretError on failure. Never logs the resolved value.
    """
    ref = (secret_ref or "").strip()

    if not ref:
        raise SecretError(SECRET_REF_MISSING, "secret_ref is empty or missing")

    if ref.startswith("env:"):
        var_name = ref[4:].strip()
        if not var_name:
            raise SecretError(SECRET_REF_MISSING, "env: prefix but no variable name")
        value = os.environ.get(var_name)
        if value is None:
            logger.error("Secret env var not set: %s (ref=%s)", var_name, _mask_ref(ref))
            raise SecretError(
                SECRET_NOT_FOUND,
                f"Environment variable '{var_name}' is not set (ref: {_mask_ref(ref)})",
            )
        logger.debug("Resolved secret from env: %s", var_name)
        return value

    # v0.1: plaintext fallback (for transition from existing configs)
    logger.debug("Secret ref treated as plaintext placeholder: %s", _mask_ref(ref))
    return ref


def resolve_secrets(device_snapshot: dict[str, Any]) -> dict[str, str]:
    """Resolve oob_password_ref and inband_password_ref from a device_snapshot dict.

    Returns {"oob_password": ..., "inband_password": ...}.
    Empty refs are treated as empty passwords (not errors).
    Never returns the password in logs.
    """
    result: dict[str, str] = {}

    oob_ref = (device_snapshot.get("oob_password_ref", "") or "").strip()
    if oob_ref:
        result["oob_password"] = resolve_secret(oob_ref)
    else:
        result["oob_password"] = ""

    inband_ref = (device_snapshot.get("inband_password_ref", "") or "").strip()
    if inband_ref:
        result["inband_password"] = resolve_secret(inband_ref)
    else:
        result["inband_password"] = ""

    return result


def _mask_ref(ref: str) -> str:
    if len(ref) <= 8:
        return "***"
    return ref[:4] + "***" + ref[-4:]
