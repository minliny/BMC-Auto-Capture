"""
Summary builder — device × task pivot table for reporting.
"""


from __future__ import annotations
import csv
import logging
import os
from collections import defaultdict
from typing import Sequence

from ..models.execution_result import ExecutionResult

logger = logging.getLogger("bmc_auto_capture.summary")


def build_pivot_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "summary_pivot.csv",
) -> str:
    """Generate a device(row) × task(column) pivot table CSV."""
    devices: dict[str, str] = {}
    tasks: list[str] = []
    task_set: set[str] = set()

    for r in results:
        if r.device_name not in devices:
            devices[r.device_name] = r.device_group
        if r.task_name not in task_set:
            task_set.add(r.task_name)
            tasks.append(r.task_name)

    lookup: dict[tuple[str, str], str] = {}
    for r in results:
        lookup[(r.device_name, r.task_name)] = r.execution_status

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
    """Print execution summary + per-task breakdown + per-group summary."""
    from .collector import compute_summary

    s = compute_summary(results)
    total = s["total"] or 1

    print("\n" + "=" * 60)
    print("  BMC Auto-Capture v0.2.1 — 执行汇总")
    print("=" * 60)
    print(f"  计划总数:       {s['total']:>6}")
    print(f"  成功:           {s['success']:>6}  ({s['success'] / total * 100:.1f}%)")
    print(f"  失败:           {s['failed']:>6}  ({s['failed'] / total * 100:.1f}%)")
    print(f"  错误:           {s['error']:>6}  ({s['error'] / total * 100:.1f}%)")
    print(f"  跳过(预检不通):  {s['skipped_preflight']:>6}")
    print(f"  跳过(端口拦截):  {s['skipped_port_blocked']:>6}")
    print(f"  跳过(路由变更):  {s['skipped_route']:>6}")
    print(f"  规则通过:       {s['rule_passed']:>6}")
    print(f"  规则失败:       {s['rule_failed']:>6}")
    print("=" * 60)

    print_execution_summary(results)


def _categorize_failure(status: str, reason: str) -> str:
    """Categorize an execution failure into a human-readable type."""
    if status == "EXEC_SUCCESS":
        return "OK"
    if "IP为空" in reason or "IP empty" in reason.lower():
        return "未配置IP"
    if "认证失败" in reason or "Authentication" in reason or "Auth" in reason:
        return "账号/密码错误"
    if "登录失败" in reason or "Login fail" in reason.lower():
        return "登录失败"
    if "页面无法访问" in reason or "Failed to reach" in reason:
        return "BMC页面无法访问"
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
    if status.startswith("EXEC_SKIPPED_PRECHECK"):
        return "预检不通"
    if status.startswith("EXEC_SKIPPED_PORT"):
        return "端口被拦截"
    return "其他错误"


def print_execution_summary(results: Sequence[ExecutionResult]) -> None:
    """Print task execution results: per-task breakdown + per-group summary."""
    if not results:
        return

    # Per-task stats
    task_stats: dict[str, dict] = defaultdict(lambda: {
        "ok": 0, "fail": defaultdict(list), "task_type": "", "total": 0,
    })
    group_stats: dict[str, dict] = defaultdict(lambda: {
        "devices": set(), "bmc_ok": 0, "bmc_fail": [], "ssh_ok": 0, "ssh_fail": [],
    })

    for r in results:
        tn = r.task_name
        ts = task_stats[tn]
        ts["task_type"] = r.task_type
        ts["total"] += 1
        if r.execution_status == "EXEC_SUCCESS":
            ts["ok"] += 1
        else:
            cat = _categorize_failure(r.execution_status, r.execution_failure_reason)
            ts["fail"][cat].append(r)

        g = group_stats[r.device_group or "(未知分组)"]
        g["devices"].add(r.device_name)
        if r.task_type in ("BMC", "BMC_URL", "BMC_ACTIONS"):
            if r.execution_status == "EXEC_SUCCESS":
                g["bmc_ok"] += 1
            else:
                g["bmc_fail"].append(r)
        elif r.task_type in ("SSH", "SSH_CMD", "TELNET", "TELNET_CMD"):
            if r.execution_status == "EXEC_SUCCESS":
                g["ssh_ok"] += 1
            else:
                g["ssh_fail"].append(r)

    # ====== Section 1: Per-task breakdown ======
    print("\n" + "=" * 80)
    print("  任务执行明细")
    print("=" * 80)

    bmc_tasks = [(n, s) for n, s in task_stats.items() if s["task_type"] in ("BMC", "BMC_URL", "BMC_ACTIONS")]
    ssh_tasks = [(n, s) for n, s in task_stats.items() if s["task_type"] in ("SSH", "SSH_CMD", "TELNET", "TELNET_CMD")]

    def _print_task_group(title, task_list):
        if not task_list:
            return
        print(f"\n  ── {title} ──")
        for name, stats in sorted(task_list):
            fail_total = sum(len(v) for v in stats["fail"].values())
            icon = "PASS" if fail_total == 0 else "FAIL"
            print(f"    [{icon}] {name}")
            print(f"          设备总数: {stats['total']}  成功: {stats['ok']}  失败: {fail_total}")
            if fail_total > 0:
                for cat, items in sorted(stats["fail"].items(), key=lambda x: -len(x[1])):
                    print(f"          └ {cat}: {len(items)} 台")
                    for r in items[:3]:
                        print(f"             · {r.device_name}: {r.execution_failure_reason[:100]}")
                    if len(items) > 3:
                        print(f"             · ... 及其他 {len(items) - 3} 台")

    _print_task_group("BMC 任务 (带外浏览器)", bmc_tasks)
    _print_task_group("SSH 任务 (带内命令行)", ssh_tasks)

    # ====== Section 2: Per-group summary ======
    if group_stats:
        print(f"\n  {'─' * 70}")
        print(f"  按设备分组汇总")
        print(f"  {'─' * 70}")
        for group_name in sorted(group_stats.keys()):
            g = group_stats[group_name]
            total_dev = len(g["devices"])
            bmc_ok, bmc_fail = g["bmc_ok"], len(g["bmc_fail"])
            ssh_ok, ssh_fail = g["ssh_ok"], len(g["ssh_fail"])
            print(f"\n  [{group_name}]  ({total_dev} 台设备)")
            if bmc_ok + bmc_fail > 0:
                print(f"    带外(BMC): 成功={bmc_ok}  失败={bmc_fail}")
            if ssh_ok + ssh_fail > 0:
                print(f"    带内(SSH): 成功={ssh_ok}  失败={ssh_fail}")

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
            if r.execution_status == "EXEC_SUCCESS":
                cat = "OK"
            elif "IP为空" in (r.execution_failure_reason or ""):
                cat = "未配置IP"
            else:
                cat = _categorize_failure(r.execution_status, r.execution_failure_reason)
            writer.writerow([
                r.device_group, r.device_name, r.bmc_ip, r.inband_ip,
                r.task_name, r.task_type, r.execution_status, cat,
                r.execution_failure_reason,
            ])

    logger.info("Wrote connectivity summary to %s", path)
    return path
