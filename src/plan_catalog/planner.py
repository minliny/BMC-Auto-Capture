"""
PlanCatalogPlanner — deterministic plan generator from Excel + validation.json.

Generates stable task_ids and plan_hash. Same inputs always produce same output.
"""

from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    PlanManifest,
    PlannedTask,
    ValidationReport,
    ValidationError,
    NetworkTestDef,
    make_task_id,
    make_device_key,
)
from .store import TaskCatalogStore
from .validation_loader import (
    load_validation_json,
    parse_network_tests,
    parse_task_types,
    parse_required_sheets,
    parse_required_device_columns,
    parse_required_task_columns,
)

PLANNER_VERSION = "0.1.0"


class PlanCatalogPlanner:
    """Deterministic planner — same Excel + validation.json ⇒ same plan."""

    def __init__(self, excel_path: str | Path, validation_json_path: str | Path):
        self._excel_path = str(excel_path)
        self._validation_json_path = str(validation_json_path)
        self._excel_sha256 = _sha256_file(self._excel_path)
        self._validation_sha256 = _sha256_file(self._validation_json_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> tuple[PlanManifest, TaskCatalogStore, ValidationReport]:
        """Run the full planner pipeline. Returns (manifest, catalog, report)."""
        report = ValidationReport()

        # Load inputs
        raw_validation = load_validation_json(self._validation_json_path)
        devices, tasks = self._load_excel(report)
        task_types = parse_task_types(raw_validation)
        network_tests = parse_network_tests(raw_validation)

        # Validate
        self._validate_sheets(raw_validation, report)
        self._validate_devices(devices, raw_validation, report)
        self._validate_tasks(tasks, devices, raw_validation, task_types, report)

        # Compute plan_id from input hashes
        plan_id = hashlib.sha256(
            f"{PLANNER_VERSION}|{self._excel_sha256}|{self._validation_sha256}".encode()
        ).hexdigest()[:16]

        # Generate tasks
        manifest = PlanManifest(
            plan_id=plan_id,
            planner_version=PLANNER_VERSION,
            excel_sha256=self._excel_sha256,
            validation_json_sha256=self._validation_sha256,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        catalog = TaskCatalogStore()

        enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
        enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]

        # Filter devices by match_group
        for device in enabled_devices:
            device_group = (getattr(device, "device_group", "") or "").strip().lower()

            for task in enabled_tasks:
                match_group = getattr(task, "match_group", "") or ""
                allowed_groups = [
                    g.strip().lower()
                    for g in match_group.split("/")
                    if g.strip()
                ]
                if allowed_groups and device_group not in allowed_groups:
                    continue

                planned = self._plan_task(device, task, plan_id, report, source_prefix="excel")
                manifest.add_task(planned)
                catalog.add(planned)

        # Network tests
        for idx, nt in enumerate(network_tests):
            for device in enabled_devices:
                dg = getattr(device, "device_group", "") or ""
                if nt.device_groups and dg not in nt.device_groups:
                    continue
                planned = self._plan_network_test(device, nt, idx, plan_id, report)
                manifest.add_task(planned)
                catalog.add(planned)

        # Sort manifest tasks for deterministic order
        manifest._tasks.sort(key=lambda t: (
            t.device_key, t.task_type, t.source_row_ref, t.task_no,
        ))

        # Compute plan_hash
        manifest.plan_hash = manifest.compute_hash()

        return manifest, catalog, report

    # ------------------------------------------------------------------
    # Internal: Excel loading
    # ------------------------------------------------------------------

    def _load_excel(self, report: ValidationReport) -> tuple[list[Any], list[Any]]:
        from ..loader.excel_reader import load_all
        try:
            return load_all(self._excel_path)
        except Exception as e:
            report.errors.append(ValidationError(
                code="EXCEL_LOAD_FAILED",
                message=str(e),
                severity="error",
            ))
            return [], []

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_sheets(self, raw: dict, report: ValidationReport):
        required = parse_required_sheets(raw)
        if not required:
            return
        try:
            import openpyxl
            wb = openpyxl.load_workbook(self._excel_path, read_only=True)
            sheet_names = wb.sheetnames
            wb.close()
            for s in required:
                if s not in sheet_names:
                    report.errors.append(ValidationError(
                        code="MISSING_SHEET", message=f"Required sheet missing: {s}",
                        row_ref=f"validation.json:required_sheets", severity="error",
                    ))
        except Exception:
            pass  # Already captured by _load_excel

    def _validate_devices(self, devices: list[Any], raw: dict, report: ValidationReport):
        required_cols = parse_required_device_columns(raw)
        for i, d in enumerate(devices):
            row_ref = f"设备信息:row={i + 2}"
            # Check required columns exist as attributes
            for col in required_cols:
                if not hasattr(d, col) or not getattr(d, col, ""):
                    report.warnings.append(ValidationError(
                        code="MISSING_DEVICE_COLUMN",
                        message=f"Device column '{col}' is empty",
                        row_ref=row_ref, severity="warning",
                    ))
            # BMC-relevant checks: oob_ip required if any BMC task exists
            if not (getattr(d, "bmc_ip", "") or "").strip():
                report.warnings.append(ValidationError(
                    code="MISSING_OOB_IP",
                    message="Device missing oob_ip — BMC tasks will be skipped",
                    row_ref=row_ref, severity="warning",
                ))
            if not (getattr(d, "inband_ip", "") or "").strip():
                report.warnings.append(ValidationError(
                    code="MISSING_INBAND_IP",
                    message="Device missing inband_ip — SSH tasks will be skipped",
                    row_ref=row_ref, severity="warning",
                ))

    def _validate_tasks(
        self, tasks: list[Any], devices: list[Any], raw: dict,
        task_types: list[str], report: ValidationReport,
    ):
        required_cols = parse_required_task_columns(raw)
        for i, t in enumerate(tasks):
            row_ref = f"任务信息:row={i + 2}"
            for col in required_cols:
                if not hasattr(t, col) or not getattr(t, col, ""):
                    report.warnings.append(ValidationError(
                        code="MISSING_TASK_COLUMN",
                        message=f"Task column '{col}' is empty",
                        row_ref=row_ref, severity="warning",
                    ))
            tt = getattr(t, "task_type", "")
            if task_types and tt not in task_types:
                report.warnings.append(ValidationError(
                    code="UNKNOWN_TASK_TYPE",
                    message=f"Task type '{tt}' not in allowed list",
                    row_ref=row_ref, severity="warning",
                ))

    # ------------------------------------------------------------------
    # Task planning
    # ------------------------------------------------------------------

    def _plan_task(
        self, device: Any, task: Any, plan_id: str,
        report: ValidationReport, source_prefix: str = "excel",
    ) -> PlannedTask:
        dg = getattr(device, "device_group", "") or ""
        device_key = make_device_key(device)
        task_no = str(getattr(task, "sequence", ""))
        task_name = getattr(task, "task_name", "")
        task_type = getattr(task, "task_type", "")
        exec_mode = getattr(task, "execution_mode", "")
        bmc_ip = (getattr(device, "bmc_ip", "") or "").strip()
        inband_ip = (getattr(device, "inband_ip", "") or "").strip()
        device_name = getattr(device, "device_name", "")

        # Derive lock_uri
        lock_uri = self._derive_lock_uri(task_type, exec_mode, bmc_ip, inband_ip, device)

        # Validate lock_uri
        if not lock_uri:
            if task_type in ("BMC",) or exec_mode in ("BMC_URL", "BMC_ACTIONS"):
                report.errors.append(ValidationError(
                    code="BMC_MISSING_OOB_IP",
                    message=f"BMC task '{task_name}' requires oob_ip on device '{device_name}'",
                    row_ref=f"{source_prefix}:device={device_key}",
                    severity="error",
                ))
            elif task_type in ("SSH", "TELNET", "NETWORK_TEST") or exec_mode in ("SSH_CMD",):
                report.errors.append(ValidationError(
                    code="SSH_MISSING_INBAND_IP",
                    message=f"SSH task '{task_name}' requires inband_ip on device '{device_name}'",
                    row_ref=f"{source_prefix}:device={device_key}",
                    severity="error",
                ))

        row_ref = f"{source_prefix}:Sheet=device={device_name}:task={task_name}"

        task_id = make_task_id(
            PLANNER_VERSION, self._excel_sha256, self._validation_sha256,
            dg, device_key, task_no, task_name, task_type, exec_mode, row_ref,
        )

        # Build device_snapshot
        device_snapshot = {
            "device_name": device_name,
            "device_group": dg,
            "oob_ip": bmc_ip,
            "oob_port": 443,
            "oob_username": getattr(device, "bmc_username", "") or "",
            "oob_password_ref": _make_secret_ref("bmc", device_name),
            "inband_ip": inband_ip,
            "inband_port": 22,
            "inband_username": getattr(device, "inband_username", "") or "",
            "inband_password_ref": _make_secret_ref("ssh", device_name),
            "ssh_type": getattr(device, "ssh_type", "") or _derive_ssh_type(device),
        }

        # Build task_snapshot
        task_snapshot = {
            "task_id": task_id,
            "task_no": task_no,
            "task_name": task_name,
            "task_type": task_type,
            "execution_mode": exec_mode,
            "match_group": getattr(task, "match_group", "") or "",
            "command_or_url": getattr(task, "command_or_url", "") or "",
            "url": getattr(task, "command_or_url", "") or "",
            "ssh_cmd": getattr(task, "command_or_url", "") or "",
            "timeout_seconds": int(getattr(task, "timeout_seconds", 60) or 60),
            "retry_count": int(getattr(task, "retry_count", 0) or 0),
            "full_screenshot": bool(getattr(task, "full_screenshot", False)),
            "screenshot_mode": getattr(task, "screenshot_mode", "auto") or "auto",
            "output_dir_template": getattr(task, "output_dir_template", "{任务序号}.{任务名称}/{设备分类}") or "{任务序号}.{任务名称}/{设备分类}",
            "image_name_template": getattr(task, "image_name_template", "{TaskIP}-{任务名称}") or "{TaskIP}-{任务名称}",
        }

        resource_lock = {
            "lock_uri": lock_uri,
            "lock_exclusive": True,
            "lock_type": task_type if task_type in ("BMC",) else "SSH",
        }

        return PlannedTask(
            task_id=task_id,
            plan_id=plan_id,
            task_no=task_no,
            task_name=task_name,
            task_type=task_type,
            execution_mode=exec_mode,
            device_group=dg,
            device_key=device_key,
            lock_uri=lock_uri,
            enabled=getattr(device, "enabled", True) and getattr(task, "enabled", True),
            source_row_ref=row_ref,
            device_snapshot=device_snapshot,
            task_snapshot=task_snapshot,
            resource_lock=resource_lock,
            output={"output_dir_template": task_snapshot["output_dir_template"]},
        )

    def _plan_network_test(
        self, device: Any, nt: NetworkTestDef, idx: int,
        plan_id: str, report: ValidationReport,
    ) -> PlannedTask:
        dg = getattr(device, "device_group", "") or ""
        device_key = make_device_key(device)
        inband_ip = (getattr(device, "inband_ip", "") or "").strip()
        device_name = getattr(device, "device_name", "")

        # Render command template
        cmd = nt.command.replace("{inband_ip}", inband_ip)
        if nt.target_ip:
            cmd = cmd.replace("{target_ip}", nt.target_ip)

        task_no = f"NET-{idx}"
        task_type = "NETWORK_TEST"
        exec_mode = nt.execution_mode
        source_ref = f"validation.json:network_tests[{idx}]"

        lock_uri = self._derive_lock_uri(
            task_type, exec_mode, "", inband_ip, device,
        )

        if not inband_ip:
            report.errors.append(ValidationError(
                code="NETWORK_TEST_MISSING_INBAND_IP",
                message=f"Network test '{nt.name}' requires inband_ip on device '{device_name}'",
                row_ref=source_ref,
                severity="error",
            ))

        task_id = make_task_id(
            PLANNER_VERSION, self._excel_sha256, self._validation_sha256,
            dg, device_key, task_no, nt.name, task_type, exec_mode, source_ref,
        )

        device_snapshot = {
            "device_name": device_name, "device_group": dg,
            "oob_ip": "", "oob_port": 443, "oob_username": "", "oob_password_ref": "",
            "inband_ip": inband_ip, "inband_port": 22,
            "inband_username": getattr(device, "inband_username", "") or "",
            "inband_password_ref": _make_secret_ref("ssh", device_name),
            "ssh_type": _derive_ssh_type(device),
        }

        task_snapshot = {
            "task_id": task_id, "task_no": task_no, "task_name": nt.name,
            "task_type": task_type, "execution_mode": exec_mode,
            "command_or_url": cmd, "ssh_cmd": cmd,
            "timeout_seconds": nt.timeout_seconds, "retry_count": 0,
            "output_dir_template": "{device_name}/network_tests",
            "image_name_template": "{TaskIP}-{任务名称}",
        }

        resource_lock = {
            "lock_uri": lock_uri,
            "lock_exclusive": True,
            "lock_type": _derive_ssh_type(device),
        }

        return PlannedTask(
            task_id=task_id, plan_id=plan_id, task_no=task_no,
            task_name=nt.name, task_type=task_type, execution_mode=exec_mode,
            device_group=dg, device_key=device_key,
            lock_uri=lock_uri, enabled=True, source_row_ref=source_ref,
            device_snapshot=device_snapshot, task_snapshot=task_snapshot,
            resource_lock=resource_lock,
            output={"output_dir_template": task_snapshot["output_dir_template"]},
        )

    @staticmethod
    def _derive_lock_uri(
        task_type: str, exec_mode: str, bmc_ip: str, inband_ip: str, device: Any,
    ) -> str:
        """Derive lock_uri from task type and device IPs. Never uses device_name."""
        tt = (task_type or "").upper()
        em = (exec_mode or "").upper()
        if tt in ("BMC",) or em in ("BMC_URL", "BMC_ACTIONS"):
            return f"bmc://{bmc_ip}" if bmc_ip else ""
        if tt in ("SSH", "TELNET", "NETWORK_TEST") or em in ("SSH_CMD",):
            if not inband_ip:
                return ""
            st = _derive_ssh_type(device)
            if st == "SSH_VRP":
                return f"ssh-vrp://{inband_ip}"
            if st == "SSH_LINUX":
                return f"ssh-linux://{inband_ip}"
            return f"ssh://{inband_ip}"
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    """SHA256 hash of file contents."""
    if not os.path.exists(path):
        return "file-not-found"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _derive_ssh_type(device: Any) -> str:
    """Derive SSH type from device group or ssh_type attribute."""
    st = getattr(device, "ssh_type", "") or ""
    if st:
        return st
    dg = (getattr(device, "device_group", "") or "").upper().strip()
    if dg in ("L1", "L2"):
        return "SSH_VRP"
    return "SSH_LINUX"


def _make_secret_ref(prefix: str, device_name: str) -> str:
    """Build a stable secret_ref for a device."""
    safe_name = device_name.replace(" ", "_").replace("/", "_")
    return f"secret://{prefix}/{safe_name}"
