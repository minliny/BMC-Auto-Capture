"""Workspace task binding validation for RulePack import/update."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .validator import RulePackValidationReport, validate_rule_pack


def validate_rule_pack_for_workspace(
    raw: dict[str, Any],
    *,
    workspace_root: str | Path | None = None,
) -> RulePackValidationReport:
    """Validate a RulePack and, when available, bind it to current tasks.json."""
    task_id = str(raw.get("task_id") or "") if isinstance(raw, dict) else ""
    task_defs, source_path, load_error = load_workspace_task_defs(workspace_root)
    task_def = task_defs.get(task_id) if task_id else None

    report = validate_rule_pack(raw, task_def=task_def)
    if load_error:
        report.add_error("RULEPACK_TASKS_JSON_INVALID", load_error)
    elif source_path is None:
        report.add_warning(
            "RULEPACK_TASKS_JSON_NOT_FOUND",
            "tasks.json was not found; RulePack was validated without task binding",
        )
    elif task_id and task_def is None:
        report.add_error(
            "RULEPACK_TASK_NOT_FOUND",
            f"RulePack task_id {task_id!r} was not found in {source_path.name}",
            "task_id",
        )
    return report


def load_workspace_task_defs(
    workspace_root: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], Path | None, str]:
    """Load raw task definitions without invoking the runtime loader.

    This deliberately avoids src.loader.excel_reader._load_task_defs because
    that loader merges RulePacks, while import/update validation needs the
    unmodified task definition as the binding source of truth.
    """
    if workspace_root is None:
        from ..excel_config_store import _resolve_workspace
        workspace_root = _resolve_workspace()

    root = Path(workspace_root)
    candidates = [
        root / "tasks.json",
        root / "app" / "tasks.json",
        root / "_internal" / "tasks.json",
    ]

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {}, path, f"Failed to read {path.name}: {exc}"

        tasks = data.get("tasks", {})
        if not isinstance(tasks, dict):
            return {}, path, f"{path.name} field 'tasks' must be an object"
        return _index_task_defs(tasks), path, ""

    return {}, None, ""


def _index_task_defs(tasks: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for key, value in tasks.items():
        if not isinstance(value, dict):
            continue
        task_def = dict(value)
        task_def.setdefault("_config_key", str(key))
        for candidate in (task_def.get("task_id"), key):
            task_id = str(candidate or "")
            if task_id:
                indexed[task_id] = task_def
    return indexed
