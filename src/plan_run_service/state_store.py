"""Persistent state file access for plan runs."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.sensitive import redact_sensitive_text

logger = logging.getLogger("bmc_auto_capture.plan_run.state")


def safe_state_id(value: str) -> str:
    """Return a path-safe id for run/plan state files."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("state id is empty")
    if ".." in raw or raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", raw):
        raise ValueError("state id contains path traversal")
    if any(sep and sep in raw for sep in (os.sep, os.altsep)):
        raise ValueError("state id contains path separator")
    return raw


class PlanRunStateStore:
    """Owns executor_state paths and atomic JSON state persistence."""

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root)
        self.state_root = self.workspace_root / "executor_state"
        self.runs_state_dir = self.state_root / "runs"
        self.plans_state_dir = self.state_root / "plans"

    def make_plan_output_root(self, plan_id: int | str, run_ts: str | None = None) -> str:
        safe_plan_id = safe_state_id(str(plan_id))
        timestamp = run_ts or datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = self.state_root / "outputs" / safe_plan_id / timestamp
        output_root.mkdir(parents=True, exist_ok=True)
        return str(output_root)

    def persist_run_state(self, run_id: str, plan_id: int | str, state: dict[str, Any]) -> None:
        safe_run_id = safe_state_id(run_id)
        safe_plan_id = safe_state_id(str(plan_id))
        self.runs_state_dir.mkdir(parents=True, exist_ok=True)
        (self.plans_state_dir / safe_plan_id).mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        for target in (
            self.runs_state_dir / f"{safe_run_id}.json",
            self.plans_state_dir / safe_plan_id / "latest_run.json",
        ):
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)

    def load_run_states(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.runs_state_dir.exists():
            return []

        states: list[tuple[str, dict[str, Any]]] = []
        for path in self.runs_state_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    states.append((path.name, data))
            except Exception as exc:
                logger.warning(
                    "PlanRun state load skipped for %s: %s",
                    path.name,
                    redact_sensitive_text(str(exc)),
                )
        return states
