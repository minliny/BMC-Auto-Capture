"""Task fingerprint helpers used for RulePack matching."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(value: object) -> str:
    text = "" if value is None else str(value)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return sha256_text(text)


def task_fingerprints(task_def: dict[str, Any]) -> dict[str, str]:
    """Return stable fingerprints for fields that bind rules to task intent."""
    command_or_url = task_def.get("command_or_url", "")
    actions_json = task_def.get("actions_json", "")
    result = {
        "command_fingerprint": sha256_text(command_or_url),
        "route_fingerprint": sha256_text(command_or_url),
    }
    if actions_json:
        try:
            parsed = json.loads(str(actions_json))
            result["actions_fingerprint"] = sha256_json(parsed)
        except (json.JSONDecodeError, TypeError):
            result["actions_fingerprint"] = sha256_text(actions_json)
    else:
        result["actions_fingerprint"] = sha256_text("")
    return result
