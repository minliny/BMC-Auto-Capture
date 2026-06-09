"""
Load and parse validation.json for plan_catalog.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from .models import NetworkTestDef


def load_validation_json(path: str | Path) -> dict[str, Any]:
    """Load validation.json. Returns the raw dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_network_tests(raw: dict[str, Any]) -> list[NetworkTestDef]:
    """Extract network_tests from validation.json dict."""
    tests = raw.get("network_tests", [])
    if not isinstance(tests, list):
        return []
    return [NetworkTestDef.from_dict(t) for t in tests]


def parse_task_types(raw: dict[str, Any]) -> list[str]:
    """Extract allowed task_types."""
    return list(raw.get("task_types", []))


def parse_required_sheets(raw: dict[str, Any]) -> list[str]:
    return list(raw.get("required_sheets", []))


def parse_required_device_columns(raw: dict[str, Any]) -> list[str]:
    return list(raw.get("required_device_columns", []))


def parse_required_task_columns(raw: dict[str, Any]) -> list[str]:
    return list(raw.get("required_task_columns", []))
