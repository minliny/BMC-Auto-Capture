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


_INVALID_HOST_VALUES = {"是", "否", "启用", "禁用", "yes", "no", "true", "false", "1", "0"}


def _resolve_host(raw: str) -> str:
    """Extract a valid hostname/IP from a raw field value.
    Handles URLs (https://...), paths (/...), and bare IPs.
    Returns empty string for values that look like boolean flags
    (likely column misalignment in Excel).
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw.lower() in _INVALID_HOST_VALUES:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        return parsed.hostname or ""
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


def check_device(device: Device, timeout: float = 5.0,
                 target: str = "all") -> PreflightResult:
    """Probe BMC and/or SSH connectivity for a single device.

    target: "all" — probe both BMC(443) and SSH(22)
            "bmc" — probe BMC only
            "ssh" — probe SSH only
    """
    bmc_status, bmc_error, bmc_lat = PreflightStatus.IP_EMPTY, "", 0.0
    ssh_status, ssh_error, ssh_lat = PreflightStatus.IP_EMPTY, "", 0.0

    if target in ("all", "bmc"):
        bmc_status, bmc_error, bmc_lat = _tcp_probe(device.bmc_ip, 443, timeout)
    if target in ("all", "ssh"):
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
              max_workers: int = 12,
              target: str = "all") -> PreflightReport:
    """Probe connectivity for all unique enabled devices.

    target: "all" — probe both BMC(443) and SSH(22)
            "bmc" — probe BMC only
            "ssh" — probe SSH only
    """
    # Dedup by device name — each unique device probed once
    seen: set[str] = set()
    unique: list[Device] = []
    for d in devices:
        if d.enabled and d.device_name not in seen:
            seen.add(d.device_name)
            unique.append(d)

    total = len(unique)
    logger.info("预检: 正在探测 %d unique devices (max %d parallel, target=%s)",
                total, max_workers, target)
    print(f"  Probing {total} devices in parallel (workers={max_workers}, target={target})...")

    report = PreflightReport(total=total)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preflight") as pool:
        future_to_device = {
            pool.submit(check_device, d, timeout, target): d for d in unique
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
        "预检完成:  %d unique devices, BMC %d/%d OK, SSH %d/%d OK",
        total,
        report.bmc_ok, report.bmc_ok + report.bmc_fail,
        report.ssh_ok, report.ssh_ok + report.ssh_fail,
    )
    return report


def _check_ssh_auth(device: Device, timeout: float) -> tuple[str, str]:
    """Try SSH authentication via paramiko. Returns (status, error_msg)."""
    import socket as _socket
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=device.inband_ip,
            port=22,
            username=device.inband_username,
            password=device.inband_password,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        client.close()
        return "OK", ""
    except paramiko.AuthenticationException:
        return "AUTH_FAILED", "SSH认证失败: 用户名或密码错误"
    except _socket.timeout:
        return "TIMEOUT", f"SSH连接超时 ({timeout}s)"
    except _socket.error as e:
        return "UNREACHABLE", f"SSH不可达: {e}"
    except Exception as e:
        return "ERROR", f"SSH检测异常: {e}"


def _check_bmc_auth(device: Device, timeout: float) -> tuple[str, str]:
    """Try BMC web interface connectivity via HTTP. Returns (status, error_msg)."""
    import socket as _socket
    bmc_ip = device.bmc_ip
    if not bmc_ip:
        return "IP_EMPTY", "BMC IP为空"
    try:
        import urllib.request as _req
        url = f"https://{bmc_ip}"
        r = _req.urlopen(url, timeout=timeout)
        if r.status == 200 or r.status == 302:
            return "OK", ""
        return f"HTTP_{r.status}", f"BMC返回HTTP {r.status}"
    except _socket.timeout:
        return "TIMEOUT", f"BMC连接超时 ({timeout}s)"
    except _socket.error as e:
        return "UNREACHABLE", f"BMC不可达: {e}"
    except Exception as e:
        return "ERROR", f"BMC检测异常: {e}"


def check_auth_all(devices: list[Device], timeout: float = 10.0,
                   max_workers: int = 12,
                   target: str = "all") -> PreflightReport:
    """Probe credential validity for all unique enabled devices.

    target: "all" — check both BMC and SSH credentials
            "bmc" — check BMC only
            "ssh" — check SSH only
    """
    seen: set[str] = set()
    unique: list[Device] = []
    for d in devices:
        if d.enabled and d.device_name not in seen:
            seen.add(d.device_name)
            unique.append(d)

    total = len(unique)
    logger.info("预检(账户): 正在验证 %d unique devices (target=%s)", total, target)
    print(f"  Verifying {total} device credentials (target={target})...")
    print(f"  BMC: HTTP/HTTPS get  |  SSH: paramiko login")
    print()

    results: list[PreflightResult] = []

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="auth") as pool:
        futures = {}
        for d in unique:
            if target in ("all", "bmc"):
                futures[pool.submit(_check_bmc_auth, d, timeout)] = (d, "bmc")
            if target in ("all", "ssh"):
                futures[pool.submit(_check_ssh_auth, d, timeout)] = (d, "ssh")

        for future in futures:
            device, check_type = futures[future]
            status, error = future.result()
            logger.info("Auth %s %s: %s%s", device.device_name, check_type.upper(), status,
                        f" ({error})" if error else "")

            # Find or create result entry for this device
            existing = next((r for r in results if r.device_name == device.device_name), None)
            if not existing:
                existing = PreflightResult(device_name=device.device_name)
                results.append(existing)

            if check_type == "bmc":
                existing.bmc_status = status
                existing.bmc_error = error
            else:
                existing.ssh_status = status
                existing.ssh_error = error

            icon = "OK" if status == "OK" else "FAIL"
            print(f"    [{icon}] {device.device_name} {check_type.upper()}: {status}"
                  f"{' - ' + error if error else ''}")

    report = PreflightReport(
        results=results,
        total=total,
        bmc_ok=sum(1 for r in results if r.bmc_status == "OK"),
        bmc_fail=sum(1 for r in results if r.bmc_status != "OK"),
        ssh_ok=sum(1 for r in results if r.ssh_status == "OK"),
        ssh_fail=sum(1 for r in results if r.ssh_status not in ("OK", "IP_EMPTY")),
    )

    logger.info(
        "预检(账户)完成: %d devices, BMC %d/%d OK, SSH %d/%d OK",
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
                    "预检: 设备 '%s' 存在故障 (BMC=%s SSH=%s) 但无匹配计划 (group/tag filter)",
                    name, pr.bmc_status, pr.ssh_status,
                )

    for plan in plans:
        pr = lookup.get(plan.device.device_name)
        if not pr:
            logger.warning(
                "预检: 设备 '%s' 在计划中但不在预检报告中 — plan not skipped",
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
        "预检应用:  BMC plans skipped=%d (fail devices with plans=%d), "
        "SSH plans skipped=%d (fail devices with plans=%d)",
        bmc_skipped, bmc_fail_devices_with_plans,
        ssh_skipped, ssh_fail_devices_with_plans,
    )

    return plans
