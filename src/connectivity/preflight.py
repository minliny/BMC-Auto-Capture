"""
Connectivity Preflight — TCP-level probe of BMC (443) and SSH (22) ports.

Uses Python socket only (satisfies security policy — Python is on the allowlist).
Distinguishes between: network-unreachable, connection-refused, port-blocked, timeout,
host-resolve-failed, ip-empty.
"""


from __future__ import annotations
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..models.device import Device
from ..models.task_plan import TaskPlan

logger = logging.getLogger("bmc_auto_capture.preflight")


class PreflightStatus:
    OK = "OK"
    UNREACHABLE = "UNREACHABLE"
    TIMEOUT = "TIMEOUT"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    PORT_BLOCKED = "PORT_BLOCKED"
    IP_EMPTY = "IP_EMPTY"
    HOST_RESOLVE_FAILED = "HOST_RESOLVE_FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class PreflightResult:
    device_name: str
    bmc_status: str = PreflightStatus.OK
    ssh_status: str = PreflightStatus.OK
    bmc_error: str = ""
    ssh_error: str = ""
    bmc_latency_ms: float = 0.0
    ssh_latency_ms: float = 0.0


@dataclass
class PreflightReport:
    results: list[PreflightResult] = field(default_factory=list)
    total: int = 0
    bmc_ok: int = 0
    bmc_fail: int = 0
    ssh_ok: int = 0
    ssh_fail: int = 0


def _resolve_host(raw: str) -> str:
    """Extract a valid hostname/IP from a raw field value.
    Handles URLs (https://...), paths (/...), and bare IPs.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        return parsed.hostname or ""
    # Bare IP or hostname
    if raw.startswith("/"):
        return ""
    return raw


def _tcp_probe(host_raw: str, port: int, timeout: float) -> tuple[str, str, float]:
    """Probe a single TCP endpoint. Returns (status, error_message, latency_ms)."""
    host = _resolve_host(host_raw)

    if not host:
        return PreflightStatus.IP_EMPTY, f"IP为空 (raw={host_raw[:60]!r})", 0.0

    start = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect((host, port))
        latency = (time.perf_counter() - start) * 1000
        return PreflightStatus.OK, "", latency
    except socket.timeout:
        return PreflightStatus.TIMEOUT, f"连接超时 ({timeout}s)", 0.0
    except ConnectionRefusedError:
        return PreflightStatus.CONNECTION_REFUSED, "连接被拒绝", 0.0
    except PermissionError:
        return PreflightStatus.PORT_BLOCKED, "端口被安全策略拦截 (EACCES)", 0.0
    except socket.gaierror as e:
        return PreflightStatus.HOST_RESOLVE_FAILED, f"DNS解析失败: {e}", 0.0
    except OSError as e:
        errno = getattr(e, "errno", 0) or getattr(e, "winerror", 0)
        if errno in (10013,):  # WSAEACCES on Windows
            return PreflightStatus.PORT_BLOCKED, "端口被安全策略拦截", 0.0
        if errno in (10051, 10065):  # Network unreachable / No route to host
            return PreflightStatus.UNREACHABLE, f"网络不可达: {e}", 0.0
        if errno in (11001,):  # WSAHOST_NOT_FOUND on Windows
            return PreflightStatus.HOST_RESOLVE_FAILED, f"DNS解析失败: {e}", 0.0
        return PreflightStatus.UNKNOWN, str(e), 0.0
    finally:
        sock.close()


def check_device(device: Device, timeout: float = 5.0) -> PreflightResult:
    bmc_status, bmc_error, bmc_lat = _tcp_probe(device.bmc_ip, 443, timeout)
    ssh_status, ssh_error, ssh_lat = _tcp_probe(device.inband_ip, 22, timeout)
    # Log with raw Excel values for debugging
    bmc_host = _resolve_host(device.bmc_ip) or "(empty)"
    ssh_host = _resolve_host(device.inband_ip) or "(empty)"
    logger.info(
        "Preflight %s: BMC raw=%r resolved=%s:443 status=%s  |  SSH raw=%r resolved=%s:22 status=%s",
        device.device_name,
        device.bmc_ip[:60], bmc_host, bmc_status,
        device.inband_ip[:60], ssh_host, ssh_status,
    )
    return PreflightResult(
        device_name=device.device_name,
        bmc_status=bmc_status,
        ssh_status=ssh_status,
        bmc_error=bmc_error,
        ssh_error=ssh_error,
        bmc_latency_ms=bmc_lat,
        ssh_latency_ms=ssh_lat,
    )


def check_all(devices: list[Device], timeout: float = 5.0,
              max_workers: int = 12) -> PreflightReport:
    # Dedup by device name — each unique device probed once
    seen: set[str] = set()
    unique: list[Device] = []
    for d in devices:
        if d.enabled and d.device_name not in seen:
            seen.add(d.device_name)
            unique.append(d)

    total = len(unique)
    logger.info("Preflight: probing %d unique devices (max %d parallel)", total, max_workers)
    print(f"  Probing {total} devices in parallel (workers={max_workers})...")

    report = PreflightReport(total=total)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preflight") as pool:
        future_to_device = {
            pool.submit(check_device, d, timeout): d for d in unique
        }
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                r = future.result()
            except Exception as e:
                logger.error("Preflight crashed for %s: %s", device.device_name, e)
                r = PreflightResult(
                    device_name=device.device_name,
                    bmc_status=PreflightStatus.UNKNOWN,
                    ssh_status=PreflightStatus.UNKNOWN,
                    bmc_error=str(e),
                    ssh_error=str(e),
                )
            report.results.append(r)
            if r.bmc_status != PreflightStatus.OK:
                report.bmc_fail += 1
            else:
                report.bmc_ok += 1
            if r.ssh_status != PreflightStatus.OK:
                report.ssh_fail += 1
            else:
                report.ssh_ok += 1

            completed += 1
            if completed % 10 == 0 or completed == total:
                print(f"  Progress: {completed}/{total} devices probed", flush=True)

    # Sort results by device name for consistent output
    report.results.sort(key=lambda r: r.device_name)

    logger.info(
        "Preflight complete: %d unique devices, BMC %d/%d OK, SSH %d/%d OK",
        total,
        report.bmc_ok, report.bmc_ok + report.bmc_fail,
        report.ssh_ok, report.ssh_ok + report.ssh_fail,
    )
    return report


def apply_preflight(
    plans: list[TaskPlan],
    report: PreflightReport,
) -> list[TaskPlan]:
    """Mark plans as skipped based on preflight results.

    - BMC unreachable / IP empty → skip BMC_URL / BMC_ACTIONS
    - SSH unreachable / port blocked / IP empty → skip SSH_CMD / TELNET_CMD

    Stores the specific failure reason on plan.skip_reason.
    """
    lookup: dict[str, PreflightResult] = {r.device_name: r for r in report.results}
    plan_device_names: set[str] = {p.device.device_name for p in plans}

    # Diagnostic: count devices with preflight failures that DO have plans
    bmc_fail_devices_with_plans = 0
    ssh_fail_devices_with_plans = 0
    bmc_skipped = 0
    ssh_skipped = 0

    # Warn about devices in preflight but not in any plan
    for name, pr in lookup.items():
        if name not in plan_device_names:
            if pr.bmc_status != PreflightStatus.OK or pr.ssh_status != PreflightStatus.OK:
                logger.info(
                    "Preflight: device '%s' has failures (BMC=%s SSH=%s) but no matching plans (group/tag filter)",
                    name, pr.bmc_status, pr.ssh_status,
                )

    for plan in plans:
        pr = lookup.get(plan.device.device_name)
        if not pr:
            logger.warning(
                "Preflight: device '%s' in plan but not in preflight report — plan not skipped",
                plan.device.device_name,
            )
            continue

        if plan.protocol == "BMC":
            if pr.bmc_status != PreflightStatus.OK:
                bmc_fail_devices_with_plans += 1
            if pr.bmc_status == PreflightStatus.PORT_BLOCKED:
                plan.status = "EXEC_SKIPPED_PORT_BLOCKED"
                plan.skip_reason = f"BMC端口被安全策略拦截: {pr.bmc_error}"
                bmc_skipped += 1
            elif pr.bmc_status != PreflightStatus.OK:
                plan.status = "EXEC_SKIPPED_PRECHECK_FAILED"
                plan.skip_reason = f"BMC预检失败({pr.bmc_status}): {pr.bmc_error}"
                bmc_skipped += 1

        elif plan.protocol == "SSH":
            if pr.ssh_status != PreflightStatus.OK:
                ssh_fail_devices_with_plans += 1
            if pr.ssh_status == PreflightStatus.PORT_BLOCKED:
                plan.status = "EXEC_SKIPPED_PORT_BLOCKED"
                plan.skip_reason = f"SSH端口被安全策略拦截: {pr.ssh_error}"
                ssh_skipped += 1
            elif pr.ssh_status != PreflightStatus.OK:
                plan.status = "EXEC_SKIPPED_PRECHECK_FAILED"
                plan.skip_reason = f"SSH预检失败({pr.ssh_status}): {pr.ssh_error}"
                ssh_skipped += 1

    logger.info(
        "Preflight apply: BMC plans skipped=%d (fail devices with plans=%d), "
        "SSH plans skipped=%d (fail devices with plans=%d)",
        bmc_skipped, bmc_fail_devices_with_plans,
        ssh_skipped, ssh_fail_devices_with_plans,
    )

    return plans
