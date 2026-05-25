"""
Connectivity Preflight — TCP-level probe of BMC (443) and SSH (22) ports.

Uses Python socket only (satisfies security policy — Python is on the allowlist).
Distinguishes between: network-unreachable, connection-refused, port-blocked, timeout.
"""


from __future__ import annotations
import logging
import socket
import time
from dataclasses import dataclass, field

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


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[str, str, float]:
    """Probe a single TCP endpoint. Returns (status, error_message, latency_ms)."""
    if not host.strip():
        return PreflightStatus.IP_EMPTY, "IP为空", 0.0

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
    except OSError as e:
        errno = getattr(e, "errno", 0) or getattr(e, "winerror", 0)
        if errno == 10013:  # WSAEACCES on Windows
            return PreflightStatus.PORT_BLOCKED, "端口被安全策略拦截", 0.0
        if errno in (10051, 10065):  # Network unreachable / No route to host
            return PreflightStatus.UNREACHABLE, f"网络不可达: {e}", 0.0
        return PreflightStatus.UNKNOWN, str(e), 0.0
    finally:
        sock.close()


def check_device(device: Device, timeout: float = 5.0) -> PreflightResult:
    bmc_status, bmc_error, bmc_lat = _tcp_probe(device.bmc_ip, 443, timeout)
    ssh_status, ssh_error, ssh_lat = _tcp_probe(device.inband_ip, 22, timeout)
    return PreflightResult(
        device_name=device.device_name,
        bmc_status=bmc_status,
        ssh_status=ssh_status,
        bmc_error=bmc_error,
        ssh_error=ssh_error,
        bmc_latency_ms=bmc_lat,
        ssh_latency_ms=ssh_lat,
    )


def check_all(devices: list[Device], timeout: float = 5.0) -> PreflightReport:
    report = PreflightReport(total=len(devices))
    for device in devices:
        if not device.enabled:
            continue
        r = check_device(device, timeout)
        report.results.append(r)
        if r.bmc_status != PreflightStatus.OK:
            report.bmc_fail += 1
            logger.info("BMC %s → %s: %s", device.device_name, r.bmc_status, r.bmc_error)
        else:
            report.bmc_ok += 1

        if r.ssh_status != PreflightStatus.OK:
            report.ssh_fail += 1
            logger.info("SSH %s → %s: %s", device.device_name, r.ssh_status, r.ssh_error)
        else:
            report.ssh_ok += 1

    logger.info(
        "Preflight complete: BMC %d/%d OK, SSH %d/%d OK",
        report.bmc_ok, report.bmc_ok + report.bmc_fail,
        report.ssh_ok, report.ssh_ok + report.ssh_fail,
    )
    return report


def apply_preflight(
    plans: list[TaskPlan],
    report: PreflightReport,
) -> list[TaskPlan]:
    """Mark plans as skipped based on preflight results.

    - BMC unreachable → skip BMC_URL / BMC_ACTIONS
    - SSH unreachable / port blocked → skip SSH_CMD / TELNET_CMD
    """
    lookup: dict[str, PreflightResult] = {r.device_name: r for r in report.results}

    for plan in plans:
        pr = lookup.get(plan.device.device_name)
        if not pr:
            continue

        if plan.protocol == "BMC":
            if pr.bmc_status == PreflightStatus.PORT_BLOCKED:
                plan.status = "EXEC_SKIPPED_PORT_BLOCKED"
            elif pr.bmc_status != PreflightStatus.OK:
                plan.status = "EXEC_SKIPPED_PRECHECK_FAILED"

        elif plan.protocol == "SSH":
            if pr.ssh_status == PreflightStatus.PORT_BLOCKED:
                plan.status = "EXEC_SKIPPED_PORT_BLOCKED"
            elif pr.ssh_status != PreflightStatus.OK:
                plan.status = "EXEC_SKIPPED_PRECHECK_FAILED"

    return plans
