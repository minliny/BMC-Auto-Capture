"""
Summary builder — device × task pivot table for reporting.
"""


from __future__ import annotations
import csv
import logging
import os
from typing import Sequence

from ..models.execution_result import ExecutionResult

logger = logging.getLogger("bmc_auto_capture.summary")


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
    print("  BMC Auto-Capture v0.2.1 — Execution Summary")
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

    # Per-group connectivity summary
    print_connectivity_summary(results)


def _categorize_failure(status: str, reason: str) -> str:
    """Categorize an execution failure into a human-readable type."""
    if status == "EXEC_SUCCESS":
        return "OK"
    if "IP为空" in reason or "IP empty" in reason.lower():
        return "IP为空"
    if "认证失败" in reason or "Authentication" in reason or "Auth" in reason:
        return "账号/密码错误"
    if "登录失败" in reason or "Login fail" in reason.lower():
        return "登录失败"
    if "超时" in reason or "timeout" in reason.lower() or "Timeout" in reason:
        return "连接超时"
    if "拒绝" in reason or "refused" in reason.lower():
        return "连接被拒绝"
    if "拦截" in reason or "blocked" in reason.lower():
        return "端口被拦截"
    if "不可达" in reason or "unreachable" in reason.lower():
        return "网络不可达"
    if "DNS" in reason or "getaddrinfo" in reason or "resolve" in reason.lower():
        return "DNS解析失败"
    if "路由" in reason or "route" in reason.lower():
        return "路由变更"
    if status.startswith("EXEC_SKIPPED_PRECHECK"):
        return "预检不通"
    if status.startswith("EXEC_SKIPPED_PORT"):
        return "端口被拦截"
    return "其他错误"


def print_connectivity_summary(results: Sequence[ExecutionResult]) -> None:
    """Print per-device-group BMC/SSH connectivity summary with failure details."""
    from collections import defaultdict

    groups: dict[str, dict] = defaultdict(lambda: {
        "devices": set(),
        "bmc_devices_with_ip": set(),
        "bmc_ok": 0, "bmc_fail": defaultdict(list),
        "ssh_devices_with_ip": set(),
        "ssh_ok": 0, "ssh_fail": defaultdict(list),
    })

    for r in results:
        g = groups[r.device_group or "(unknown)"]
        g["devices"].add(r.device_name)

        if r.task_type in ("BMC", "BMC_URL", "BMC_ACTIONS"):
            if r.bmc_ip:
                g["bmc_devices_with_ip"].add(r.device_name)
            if r.execution_status == "EXEC_SUCCESS":
                g["bmc_ok"] += 1
            else:
                cat = _categorize_failure(r.execution_status, r.execution_failure_reason)
                g["bmc_fail"][cat].append(r)

        elif r.task_type in ("SSH", "SSH_CMD", "TELNET", "TELNET_CMD"):
            if r.inband_ip:
                g["ssh_devices_with_ip"].add(r.device_name)
            if r.execution_status == "EXEC_SUCCESS":
                g["ssh_ok"] += 1
            else:
                cat = _categorize_failure(r.execution_status, r.execution_failure_reason)
                g["ssh_fail"][cat].append(r)

    if not groups:
        print("\n  (No connectivity data available)")
        return

    print("\n" + "=" * 80)
    print("  Per-Group Connectivity Summary")
    print("=" * 80)

    for group_name in sorted(groups.keys()):
        g = groups[group_name]
        total_dev = len(g["devices"])
        bmc_with_ip = len(g["bmc_devices_with_ip"])
        ssh_with_ip = len(g["ssh_devices_with_ip"])

        print(f"\n  [{group_name}]  ({total_dev} devices)")

        # BMC section
        bmc_total = g["bmc_ok"] + sum(len(v) for v in g["bmc_fail"].values())
        print(f"    ── 带外 (BMC) ──")
        print(f"    设备总数: {total_dev}  有BMC IP: {bmc_with_ip}  任务总数: {bmc_total}")
        print(f"    连通成功: {g['bmc_ok']}")
        if g["bmc_fail"]:
            print(f"    不通过: {sum(len(v) for v in g['bmc_fail'].values())}")
            for cat, items in sorted(g["bmc_fail"].items(), key=lambda x: -len(x[1])):
                print(f"      └ {cat}: {len(items)} 台")
                # Show first 3 device examples
                for r in items[:3]:
                    reason = r.execution_failure_reason[:80] if r.execution_failure_reason else "(no detail)"
                    print(f"         · {r.device_name}: {reason}")
                if len(items) > 3:
                    print(f"         · ... and {len(items) - 3} more")
        else:
            print(f"    不通过: 0")

        # SSH section
        ssh_total = g["ssh_ok"] + sum(len(v) for v in g["ssh_fail"].values())
        print(f"    ── 带内 (SSH) ──")
        print(f"    设备总数: {total_dev}  有带内IP: {ssh_with_ip}  任务总数: {ssh_total}")
        print(f"    连通成功: {g['ssh_ok']}")
        if g["ssh_fail"]:
            print(f"    不通过: {sum(len(v) for v in g['ssh_fail'].values())}")
            for cat, items in sorted(g["ssh_fail"].items(), key=lambda x: -len(x[1])):
                print(f"      └ {cat}: {len(items)} 台")
                for r in items[:3]:
                    reason = r.execution_failure_reason[:80] if r.execution_failure_reason else "(no detail)"
                    print(f"         · {r.device_name}: {reason}")
                if len(items) > 3:
                    print(f"         · ... and {len(items) - 3} more")
        else:
            print(f"    不通过: 0")

    # Overall totals
    print(f"\n  {'─' * 70}")
    all_bmc_ok = sum(g["bmc_ok"] for g in groups.values())
    all_bmc_fail = sum(sum(len(v) for v in g["bmc_fail"].values()) for g in groups.values())
    all_ssh_ok = sum(g["ssh_ok"] for g in groups.values())
    all_ssh_fail = sum(sum(len(v) for v in g["ssh_fail"].values()) for g in groups.values())
    print(f"  TOTAL: BMC OK={all_bmc_ok} FAIL={all_bmc_fail}  |  SSH OK={all_ssh_ok} FAIL={all_ssh_fail}")
    print("=" * 80)


def write_connectivity_csv(results: Sequence[ExecutionResult], output_dir: str,
                           filename: str = "connectivity_summary.csv") -> str:
    """Write per-task failure categorization CSV."""
    path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "设备分类", "设备名称", "BMC IP", "带内IP",
            "任务名称", "任务类型", "执行状态", "失败分类", "失败原因",
        ])
        for r in sorted(results, key=lambda r: (r.device_group, r.device_name, r.task_name)):
            cat = _categorize_failure(r.execution_status, r.execution_failure_reason) if r.execution_status != "EXEC_SUCCESS" else "OK"
            writer.writerow([
                r.device_group, r.device_name, r.bmc_ip, r.inband_ip,
                r.task_name, r.task_type, r.execution_status, cat,
                r.execution_failure_reason,
            ])

    logger.info("Wrote connectivity summary to %s", path)
    return path
