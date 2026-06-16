"""
Summary builder — device × task pivot table + clean terminal output.
"""


from __future__ import annotations
import csv
import logging
import os
from collections import defaultdict
from typing import Sequence

from ..checks import CHECK_FAILED_STATUSES, CheckResult, CheckStage
from ..models.execution_result import ExecutionResult
from ..utils.path_safety import safe_join_under_root, is_safe_path_component
from .failure_classification import classify_failure, normalized_failure_reason

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
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header = ["设备分组", "设备名称"] + tasks
        writer.writerow(header)
        for dev_name, dev_group in sorted_devices:
            row = [dev_group, dev_name]
            for task_name in tasks:
                row.append(lookup.get((dev_name, task_name), "-"))
            writer.writerow(row)
    return path


def print_terminal_summary(results: Sequence[ExecutionResult]) -> None:
    """Print a single clean summary: overall stats + per-task table."""
    if not results:
        print("\n  无执行结果。")
        return

    from .collector import compute_summary

    s = compute_summary(results)
    total = s["total"] or 1

    # ====== Overall stats (one line each) ======
    print("\n" + "=" * 70)
    print(f"  执行完成: 共 {total} 个计划")
    print(f"  成功: {s['success']}  ({s['success'] / total * 100:.0f}%)")
    not_pass = (s['failed'] + s['error'] + s['timeout'] +
                s.get('rule_failed_execution', 0) + s['partial'] +
                s['blocked'] + s['unknown'] +
                s['skipped_preflight'] + s['skipped_port_blocked'] + s['skipped_route'] +
                s['skipped_stopped'] + s['skipped_session'] + s['skipped_disabled'])
    if not_pass > 0:
        parts = []
        if s['failed']: parts.append(f"失败: {s['failed']}")
        if s['error']: parts.append(f"错误: {s['error']}")
        if s['timeout']: parts.append(f"超时: {s['timeout']}")
        if s.get('rule_failed_execution', 0): parts.append(f"规则失败: {s['rule_failed_execution']}")
        if s['partial']: parts.append(f"部分完成: {s['partial']}")
        if s['blocked']: parts.append(f"阻塞: {s['blocked']}")
        if s['unknown']: parts.append(f"未知: {s['unknown']}")
        if s['skipped_preflight']: parts.append(f"预检跳过: {s['skipped_preflight']}")
        if s['skipped_port_blocked']: parts.append(f"端口拦截: {s['skipped_port_blocked']}")
        if s['skipped_route']: parts.append(f"路由变更: {s['skipped_route']}")
        if s['skipped_stopped']: parts.append(f"调度停止: {s['skipped_stopped']}")
        if s['skipped_session']: parts.append(f"会话失败: {s['skipped_session']}")
        if s['skipped_disabled']: parts.append(f"已禁用: {s['skipped_disabled']}")
        print(f"  未通过: {'  '.join(parts)}")

    # ====== Per-task result table ======
    # Group results by task_name → count OK/FAIL by category
    task_data: dict[str, dict] = defaultdict(lambda: {"type": "", "total": 0, "ok": 0, "fail": defaultdict(list)})

    for r in results:
        td = task_data[r.task_name]
        td["type"] = r.task_type
        td["total"] += 1
        if r.execution_status == "EXEC_SUCCESS":
            td["ok"] += 1
        else:
            cat = _categorize_failure(r)
            td["fail"][cat].append(r)

    # Calculate display width for CJK text
    def _disp_width(s: str) -> int:
        """Approximate terminal display width (CJK chars count as 2)."""
        w = 0
        for c in s:
            if '一' <= c <= '鿿' or '　' <= c <= '〿' or \
               '＀' <= c <= '￯':
                w += 2
            else:
                w += 1
        return w

    def _pad(s: str, width: int) -> str:
        """Pad string to a fixed display width."""
        return s + ' ' * max(0, width - _disp_width(s))

    col_w = max(_disp_width(n) for n in task_data.keys()) if task_data else 20
    col_w = min(col_w, 60)  # cap
    col_w = max(col_w, 20)  # minimum

    print(f"\n{_pad('任务名称', col_w)} {'类型':>5s} {'总数':>4s} {'成功':>4s} {'失败':>4s}  失败原因")
    print("-" * max(70, col_w + 40))

    for name in sorted(task_data.keys()):
        td = task_data[name]
        fail_count = sum(len(v) for v in td["fail"].values())
        status = "OK" if fail_count == 0 else "!!"
        ttype = "BMC" if td["type"] in ("BMC", "BMC_URL", "BMC_ACTIONS") else "SSH"
        pad_name = _pad(name, col_w)
        print(f"{pad_name} {ttype:>5s} {td['total']:>4d} {td['ok']:>4d} {fail_count:>4d}  ", end="")
        if fail_count > 0:
            reasons = []
            for cat, items in sorted(td["fail"].items(), key=lambda x: -len(x[1])):
                reasons.append(f"{cat}:{len(items)}台")
            print("  ".join(reasons[:3]))
        else:
            print()

    print("=" * 70)


def _categorize_failure(result_or_status, reason: str | None = None) -> str:
    """Categorize a failure reason into a short label."""
    if isinstance(result_or_status, ExecutionResult):
        if result_or_status.rule_status == "RULE_PARSE_FAILED":
            return "规则解析失败"
        if result_or_status.rule_status == "RULE_FAILED":
            return "规则失败"
        if result_or_status.checkpoint_status == "CHECK_FAIL":
            return "检查点失败"
        stable = classify_failure(result_or_status)
        if stable:
            return stable
        status = result_or_status.execution_status
        reason = result_or_status.execution_failure_reason
    else:
        status = str(result_or_status or "")
        reason = reason or ""

    if status == "EXEC_SUCCESS":
        return "OK"
    if "IP为空" in reason or "IP empty" in reason.lower():
        return "未配IP"
    if "认证失败" in reason or "Authentication" in reason:
        return "认证失败"
    if "登录失败" in reason:
        return "登录失败"
    if "页面无法访问" in reason or "Failed to reach" in reason:
        return "页面不通"
    if "超时" in reason or "timeout" in reason.lower():
        return "超时"
    if "拒绝" in reason or "refused" in reason.lower():
        return "连接拒绝"
    if status.startswith("EXEC_SKIPPED_PRECHECK"):
        return "预检跳过"
    if status.startswith("EXEC_SKIPPED_PORT"):
        return "端口拦截"
    if status.startswith("EXEC_SKIPPED_SESSION"):
        return "会话失败"
    if status.startswith("EXEC_SKIPPED_DISABLED"):
        return "已禁用"
    if status.startswith("EXEC_SKIPPED_STOPPED"):
        return "调度停止"
    if status.startswith("EXEC_SKIPPED_ROUTE"):
        return "路由变更"
    if status == "EXEC_TIMEOUT":
        return "超时"
    if status == "EXEC_SUCCESS_RULE_FAILED":
        return "规则失败"
    if status == "EXEC_PARTIAL":
        return "部分完成"
    if status == "EXEC_BLOCKED":
        return "阻塞"
    if status.startswith("EXEC_") and "UNKNOWN" in status.upper():
        return "未知"
    return "其他"


def write_failure_csv(results: Sequence[ExecutionResult], output_dir: str,
                      filename: str = "failure_detail.csv") -> str:
    """Write failed tasks with reasons to CSV for detailed review."""
    failed = [
        r for r in results
        if r.execution_status != "EXEC_SUCCESS"
        or r.rule_status in ("RULE_FAILED", "RULE_PARSE_FAILED")
        or r.checkpoint_status == "CHECK_FAIL"
        or _has_blocking_check_failure(r)
    ]
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "计划ID", "任务ID", "执行项ID",
            "设备分组", "设备名称", "任务名称", "任务类型",
            "执行状态", "规则状态", "检查点状态", "失败分类", "失败原因",
            "检查失败明细",
        ])
        for r in sorted(failed, key=lambda r: (r.device_group, r.device_name, r.task_id, r.task_name)):
            cat = _categorize_failure(r)
            writer.writerow([
                r.plan_id, r.task_id, r.plan_item_id,
                r.device_group, r.device_name, r.task_name, r.task_type,
                r.execution_status, r.rule_status, r.checkpoint_status,
                cat, normalized_failure_reason(r), r.check_failure_summary(),
            ])

    logger.info("Wrote %d failures to %s", len(failed), path)
    return path


def _has_blocking_check_failure(result: ExecutionResult) -> bool:
    for cr in getattr(result, "check_results", None) or ():
        if isinstance(cr, dict):
            cr = CheckResult.from_dict(cr)
        if cr.stage == CheckStage.POST_AUDIT:
            continue
        if cr.status in CHECK_FAILED_STATUSES and cr.severity == "ERROR":
            return True
    return False
