"""Filesystem storage for task-scoped RulePacks."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .capabilities import normalize_protocol
from .validator import validate_rule_pack


class RulePackStore:
    """Read and write RulePacks under config/rule_packs/{protocol}/."""

    ROOT_DIR = "config/rule_packs"

    def __init__(self, workspace_root: str | Path | None = None):
        if workspace_root is None:
            from ..excel_config_store import _resolve_workspace
            workspace_root = _resolve_workspace()
        self.workspace = Path(workspace_root)
        self.root = self.workspace / self.ROOT_DIR

    def capabilities_path(self) -> Path:
        return self.root

    def get(self, task_id: str) -> dict[str, Any] | None:
        for path in self._candidate_paths(task_id):
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return None
                return data if isinstance(data, dict) else None
        return None

    def put(self, rule_pack: dict[str, Any]) -> dict[str, Any]:
        report = validate_rule_pack(rule_pack)
        if not report.valid:
            raise ValueError("; ".join(m.message for m in report.errors))
        pack = report.normalized
        protocol = normalize_protocol(pack.get("protocol"))
        task_id = str(pack.get("task_id") or "")
        path = self._path(protocol, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(pack, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".rulepack.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return {
            "taskId": task_id,
            "protocol": protocol,
            "path": str(path),
            "rulePack": pack,
        }

    def list(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self.root.exists():
            return results
        for path in sorted(self.root.glob("*/*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict):
                results.append({
                    "taskId": data.get("task_id", ""),
                    "protocol": normalize_protocol(data.get("protocol", "")),
                    "path": str(path),
                    "rulePackId": data.get("rule_pack_id", ""),
                })
        return results

    def _candidate_paths(self, task_id: str) -> list[Path]:
        safe_id = _safe_task_id(task_id)
        return [
            self.root / protocol.lower() / f"{safe_id}.json"
            for protocol in ("BMC", "SSH", "TELNET")
        ] + [self.root / f"{safe_id}.json"]

    def _path(self, protocol: str, task_id: str) -> Path:
        safe_id = _safe_task_id(task_id)
        safe_protocol = normalize_protocol(protocol).lower() or "unknown"
        return self.root / safe_protocol / f"{safe_id}.json"


_default_store: RulePackStore | None = None


def get_default_rule_pack_store() -> RulePackStore:
    global _default_store
    if _default_store is None:
        _default_store = RulePackStore()
    return _default_store


def _safe_task_id(task_id: str) -> str:
    text = str(task_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return safe.strip("._") or "unknown"
