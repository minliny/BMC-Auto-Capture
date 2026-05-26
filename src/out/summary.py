"""
Summary builder — device × task pivot table for reporting.
"""


from __future__ import annotations
import csv
import os
from typing import Sequence

from ..models.execution_result import ExecutionResult


def build_pivot_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "summary_pivot.csv",
) -> str:
    """Generate a device(row) × task(column) pivot table CSV.

    Each cell shows execution_status.
    """
    # Collect unique devices and tasks
    devices: dict[str, str] = {}  # device_name -> device_group
    tasks: list[str] = []
    task_set: set[str] = set()

    for r in results:
        if r.device_name not in devices:
            devices[r.device_name] = r.device_group
        if r.task_name not in task_set:
            task_set.add(r.task_name)
            tasks.append(r.task_name)

    # Build lookup: (device, task) -> status
    lookup: dict[tuple[str, str], str] = {}
    for r in results:
        lookup[(r.device_name, r.task_name)] = r.execution_status

    # Sort devices by group then name
    sorted_devices = sorted(devices.items(), key=lambda x: (x[1], x[0]))

    path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header = ["设备分组", "设备名称"] + tasks
        writer.writerow(header)

        for dev_name, dev_group in sorted_devices:
            row = [dev_group, dev_name]
            for task_name in tasks:
                status = lookup.get((dev_name, task_name), "-")
                row.append(status)
            writer.writerow(row)

    return path


def print_terminal_summary(results: Sequence[ExecutionResult]) -> None:
    """Print a human-readable summary to stdout."""
    from .collector import compute_summary

    s = compute_summary(results)
    total = s["total"] or 1

    print("\n" + "=" * 60)
    print("  BMC Auto-Capture v2.0 — Execution Summary")
    print("=" * 60)
    print(f"  Total plans:    {s['total']:>6}")
    print(f"  Success:        {s['success']:>6}  ({s['success'] / total * 100:.1f}%)")
    print(f"  Failed:         {s['failed']:>6}  ({s['failed'] / total * 100:.1f}%)")
    print(f"  Error:          {s['error']:>6}  ({s['error'] / total * 100:.1f}%)")
    print(f"  Skipped (preflight): {s['skipped_preflight']:>6}")
    print(f"  Skipped (port):      {s['skipped_port_blocked']:>6}")
    print(f"  Skipped (route):     {s['skipped_route']:>6}")
    print(f"  Rule passed:    {s['rule_passed']:>6}")
    print(f"  Rule failed:    {s['rule_failed']:>6}")
    print("=" * 60)
