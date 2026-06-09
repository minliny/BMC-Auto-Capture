"""
Core models for plan_catalog — deterministic manifest, planned tasks, validation.
"""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Network test definition (from validation.json)
# ---------------------------------------------------------------------------

@dataclass
class NetworkTestDef:
    network_test_id: str = ""
    name: str = ""
    device_groups: list[str] = field(default_factory=list)
    execution_mode: str = "SSH_CMD"
    command: str = ""
    target_ip: str = "{inband_ip}"
    timeout_seconds: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_test_id": self.network_test_id,
            "name": self.name,
            "device_groups": list(self.device_groups),
            "execution_mode": self.execution_mode,
            "command": self.command,
            "target_ip": self.target_ip,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NetworkTestDef":
        return cls(
            network_test_id=d.get("network_test_id", ""),
            name=d.get("name", ""),
            device_groups=list(d.get("device_groups", [])),
            execution_mode=d.get("execution_mode", "SSH_CMD"),
            command=d.get("command", ""),
            target_ip=d.get("target_ip", "{inband_ip}"),
            timeout_seconds=int(d.get("timeout_seconds", 30)),
        )


# ---------------------------------------------------------------------------
# Validation report
# ---------------------------------------------------------------------------

@dataclass
class ValidationError:
    code: str
    message: str
    row_ref: str = ""
    severity: str = "error"  # "error" | "warning"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": self.message,
            "row_ref": self.row_ref, "severity": self.severity,
        }


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.is_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }


# ---------------------------------------------------------------------------
# PlanManifest — the deterministic output
# ---------------------------------------------------------------------------

@dataclass
class PlannedTask:
    """A single task in both the manifest and catalog."""
    task_id: str
    plan_id: str = ""
    task_no: str = ""
    task_name: str = ""
    task_type: str = ""
    execution_mode: str = ""
    device_group: str = ""
    device_key: str = ""       # stable device identifier (not device_name)
    lock_uri: str = ""
    enabled: bool = True
    source_row_ref: str = ""   # "excel:SheetName:row=N" or "validation.json:network_tests[0]"
    device_snapshot: dict[str, Any] = field(default_factory=dict)
    task_snapshot: dict[str, Any] = field(default_factory=dict)
    resource_lock: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "task_no": self.task_no,
            "task_name": self.task_name,
            "task_type": self.task_type,
            "execution_mode": self.execution_mode,
            "device_group": self.device_group,
            "device_key": self.device_key,
            "lock_uri": self.lock_uri,
            "enabled": self.enabled,
            "source_row_ref": self.source_row_ref,
        }

    def to_catalog_dict(self) -> dict[str, Any]:
        """Full record for task_catalog.json."""
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "device_snapshot": dict(self.device_snapshot),
            "task_snapshot": dict(self.task_snapshot),
            "resource_lock": dict(self.resource_lock),
            "output": dict(self.output),
            "source_row_ref": self.source_row_ref,
        }


class PlanManifest:
    """Deterministic manifest of all planned tasks."""

    def __init__(
        self,
        plan_id: str = "",
        plan_hash: str = "",
        planner_version: str = "0.1.0",
        excel_sha256: str = "",
        validation_json_sha256: str = "",
        generated_at: str = "",
        tasks: list[PlannedTask] | None = None,
    ):
        self.plan_id = plan_id
        self.plan_hash = plan_hash
        self.planner_version = planner_version
        self.excel_sha256 = excel_sha256
        self.validation_json_sha256 = validation_json_sha256
        self.generated_at = generated_at
        self._tasks: list[PlannedTask] = tasks or []

    @property
    def tasks(self) -> list[PlannedTask]:
        return list(self._tasks)

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    def add_task(self, t: PlannedTask):
        self._tasks.append(t)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "planner_version": self.planner_version,
            "excel_sha256": self.excel_sha256,
            "validation_json_sha256": self.validation_json_sha256,
            "generated_at": self.generated_at,
            "task_count": self.task_count,
            "tasks": [t.to_dict() for t in self._tasks],
        }

    def compute_hash(self) -> str:
        """Deterministic plan_hash — excludes generated_at and plan_id."""
        data = {
            "planner_version": self.planner_version,
            "excel_sha256": self.excel_sha256,
            "validation_json_sha256": self.validation_json_sha256,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "task_no": t.task_no,
                    "task_type": t.task_type,
                    "execution_mode": t.execution_mode,
                    "device_key": t.device_key,
                    "device_group": t.device_group,
                    "lock_uri": t.lock_uri,
                    "enabled": t.enabled,
                    "source_row_ref": t.source_row_ref,
                }
                for t in self._tasks
            ],
        }
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_hash(*parts: str) -> str:
    """Deterministic hash from ordered string parts."""
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def make_task_id(
    planner_version: str,
    excel_sha256: str,
    validation_json_sha256: str,
    device_group: str,
    device_key: str,
    task_no: str,
    task_name: str,
    task_type: str,
    execution_mode: str,
    source_row_ref: str = "",
) -> str:
    """Generate a stable, deterministic task_id. No UUID."""
    return _stable_hash(
        planner_version,
        excel_sha256,
        validation_json_sha256,
        device_group,
        device_key,
        task_no,
        task_name,
        task_type,
        execution_mode,
        source_row_ref,
    )


def make_device_key(device: Any) -> str:
    """Stable device identifier based on IPs, NOT device_name."""
    bmc = (getattr(device, "bmc_ip", "") or "").strip()
    inband = (getattr(device, "inband_ip", "") or "").strip()
    group = (getattr(device, "device_group", "") or "").strip()
    return _stable_hash(f"dev:{group}:{bmc}:{inband}")
