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
    device_group: str = ""
    bmc_endpoint: str = ""
    ssh_endpoint: str = ""
    bmc_username: str = ""
    ssh_username: str = ""
    bmc_duration: float = 0.0
    ssh_duration: float = 0.0

    def check_results(self):
        results = []
        for target in ("bmc", "ssh"):
            check = _preflight_check_result_for_target(self, target)
            if check is not None:
                results.append(check)
        return results

    def check_results_as_dicts(self) -> list[dict]:
        return [c.to_dict() for c in self.check_results()]


@dataclass
class PreflightReport:
    results: list[PreflightResult] = field(default_factory=list)
    total: int = 0
    bmc_ok: int = 0
    bmc_fail: int = 0
    ssh_ok: int = 0
    ssh_fail: int = 0
    probe_count: int = 0
    impacted_task_count: int = 0
    skipped_task_count: int = 0

    def check_results(self):
        checks = []
        for result in self.results:
            checks.extend(result.check_results())
        return checks

    def check_results_as_dicts(self) -> list[dict]:
        return [c.to_dict() for c in self.check_results()]


def _preflight_check_result_for_target(result: PreflightResult, target: str):
    from ..checks import CheckResult, CheckStage, CheckStatus

    status = getattr(result, f"{target}_status", "")
    error = getattr(result, f"{target}_error", "")
    endpoint = getattr(result, f"{target}_endpoint", "")
    latency_ms = getattr(result, f"{target}_latency_ms", 0.0)
    duration = getattr(result, f"{target}_duration", 0.0)
    username = getattr(result, f"{target}_username", "")

    if status in (PreflightStatus.OK, PreflightStatus.IP_EMPTY) and not any(
        (error, endpoint, latency_ms, duration, username)
    ):
        return None

    if status in (PreflightStatus.OK, "AUTH_OK"):
        check_status = CheckStatus.PASS
        severity = "INFO"
    elif status in (PreflightStatus.IP_EMPTY, "CREDENTIAL_EMPTY"):
        check_status = CheckStatus.SKIP
        severity = "WARNING"
    else:
        check_status = CheckStatus.FAIL
        severity = "ERROR"

    source = "connectivity.preflight"
    return CheckResult(
        stage=CheckStage.PRECHECK,
        check_id=f"{source}.{target}",
        status=check_status,
        severity=severity,
        message=error or status,
        details={
            "device_name": result.device_name,
            "device_group": result.device_group,
            "target": target.upper(),
            "status": status,
            "error": error,
            "endpoint": endpoint,
            "latency_ms": latency_ms,
            "duration_seconds": duration,
        },
        source=source,
        target=endpoint or result.device_name,
        actual=status,
    )


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


def build_endpoint_key(protocol: str, host: str, port: int) -> str:
    """Build an endpoint key string from protocol, host, and port.

    Empty host is represented as 'IP_EMPTY'.
    """
    return f"{protocol}|{host or 'IP_EMPTY'}|{port}"


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
    """Probe connectivity for all unique endpoints.

    Deduplicates by endpoint (protocol+host+port), not by device_name.
    Same BMC IP:443 is probed only once even if multiple devices share it.
    """
    # Build endpoint → devices mapping
    endpoint_map: dict[str, list[Device]] = {}  # endpoint_key -> [Device]
    endpoint_probe_spec: dict[str, tuple[str, str, int]] = {}  # endpoint_key -> (protocol, host, port)

    for d in devices:
        if not d.enabled:
            continue
        if target in ("all", "bmc"):
            bmc_host = _resolve_host(d.bmc_ip)
            bmc_key = f"BMC|{bmc_host or 'IP_EMPTY'}|443"
            endpoint_map.setdefault(bmc_key, []).append(d)
            endpoint_probe_spec.setdefault(bmc_key, ("BMC", bmc_host, 443))
        if target in ("all", "ssh"):
            ssh_host = _resolve_host(d.inband_ip)
            ssh_key = f"SSH|{ssh_host or 'IP_EMPTY'}|22"
            endpoint_map.setdefault(ssh_key, []).append(d)
            endpoint_probe_spec.setdefault(ssh_key, ("SSH", ssh_host, 22))

    probe_count = len(endpoint_probe_spec)
    impacted_task_count = sum(len(dl) for dl in endpoint_map.values())

    logger.info("预检: 正在探测 %d unique endpoints (max %d parallel, target=%s)",
                probe_count, max_workers, target)
    print(f"  Probing {probe_count} endpoints in parallel (workers={max_workers}, target={target})...")

    # Probe each unique endpoint
    endpoint_results: dict[str, tuple[str, str, float]] = {}  # endpoint_key -> (status, error, latency)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preflight") as pool:
        future_to_key = {}
        for key, (protocol, host, port) in endpoint_probe_spec.items():
            if "IP_EMPTY" in key:
                endpoint_results[key] = (PreflightStatus.IP_EMPTY, "IP为空", 0.0)
            else:
                future_to_key[pool.submit(_tcp_probe, host, port, timeout)] = key

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                status, error, latency = future.result()
            except Exception as e:
                status, error, latency = PreflightStatus.UNKNOWN, str(e), 0.0
            endpoint_results[key] = (status, error, latency)
            logger.info("Endpoint %s: status=%s latency=%.1fms", key, status, latency)

    # Map endpoint results back to per-device PreflightResult
    report = PreflightReport(
        total=len(set(d.device_name for d in devices if d.enabled)),
        probe_count=probe_count,
        impacted_task_count=impacted_task_count,
    )

    # Build per-device results
    device_results: dict[str, PreflightResult] = {}
    for key, dev_list in endpoint_map.items():
        status, error, latency = endpoint_results.get(key, (PreflightStatus.UNKNOWN, "unknown", 0.0))
        protocol = key.split("|")[0]
        for d in dev_list:
            if d.device_name not in device_results:
                device_results[d.device_name] = PreflightResult(
                    device_name=d.device_name,
                    device_group=d.device_group,
                )
            pr = device_results[d.device_name]
            if protocol == "BMC":
                pr.bmc_status = status
                pr.bmc_error = error
                pr.bmc_latency_ms = latency
                pr.bmc_endpoint = key
            else:
                pr.ssh_status = status
                pr.ssh_error = error
                pr.ssh_latency_ms = latency
                pr.ssh_endpoint = key

    report.results = sorted(device_results.values(), key=lambda r: r.device_name)

    # Count OK/fail
    for r in report.results:
        if r.bmc_status != PreflightStatus.OK and r.bmc_status != PreflightStatus.IP_EMPTY:
            report.bmc_fail += 1
        elif r.bmc_status == PreflightStatus.OK:
            report.bmc_ok += 1
        if r.ssh_status != PreflightStatus.OK and r.ssh_status != PreflightStatus.IP_EMPTY:
            report.ssh_fail += 1
        elif r.ssh_status == PreflightStatus.OK:
            report.ssh_ok += 1

    # Count skipped tasks
    skipped = 0
    for r in report.results:
        if r.bmc_status not in (PreflightStatus.OK, PreflightStatus.IP_EMPTY, ""):
            skipped += 1
        if r.ssh_status not in (PreflightStatus.OK, PreflightStatus.IP_EMPTY, ""):
            skipped += 1
    report.skipped_task_count = skipped

    logger.info(
        "网络预检完成：探测端点 %d 个，影响任务 %d 个，跳过任务 %d 个。",
        probe_count, impacted_task_count, skipped,
    )
    return report


def _check_ssh_auth(device: Device, timeout: float) -> tuple[str, str, float]:
    """Try SSH authentication via paramiko. Returns (status, error_msg, duration)."""
    import socket as _socket
    t0 = time.time()
    if not device.inband_ip:
        return "IP_EMPTY", "带内IP为空", 0.0
    if not device.inband_username or not device.inband_password:
        return "CREDENTIAL_EMPTY", "带内用户名或密码为空", 0.0
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=device.inband_ip, port=22,
            username=device.inband_username, password=device.inband_password,
            timeout=timeout, look_for_keys=False, allow_agent=False,
        )
        client.close()
        return "AUTH_OK", "", round(time.time() - t0, 3)
    except paramiko.AuthenticationException:
        return "AUTH_FAILED", "SSH认证失败: 用户名或密码错误", round(time.time() - t0, 3)
    except _socket.timeout:
        return "TIMEOUT", f"SSH连接超时 ({timeout}s)", round(time.time() - t0, 3)
    except _socket.error as e:
        s = str(e).lower()
        return ("CONNECT_FAILED", f"SSH连接失败: {e}", round(time.time() - t0, 3))
    except Exception as e:
        return "ERROR", f"SSH检测异常: {e}", round(time.time() - t0, 3)


def _check_bmc_auth(device: Device, timeout: float) -> tuple[str, str, float]:
    """Try BMC web interface via HTTPS. Returns (status, error_msg, duration)."""
    import socket as _socket
    t0 = time.time()
    if not device.bmc_ip:
        return "IP_EMPTY", "BMC IP为空", 0.0
    if not device.bmc_username or not device.bmc_password:
        return "CREDENTIAL_EMPTY", "BMC用户名或密码为空", 0.0
    try:
        import urllib.request as _req
        url = f"https://{device.bmc_ip}"
        r = _req.urlopen(url, timeout=timeout)
        if r.status == 200 or r.status == 302:
            return "AUTH_OK", "", round(time.time() - t0, 3)
        return f"HTTP_{r.status}", f"BMC返回HTTP {r.status}", round(time.time() - t0, 3)
    except _socket.timeout:
        return "TIMEOUT", f"BMC连接超时 ({timeout}s)", round(time.time() - t0, 3)
    except _socket.error as e:
        s = str(e).lower()
        return ("CONNECT_FAILED", f"BMC连接失败: {e}", round(time.time() - t0, 3))
    except Exception as e:
        return "ERROR", f"BMC检测异常: {e}", round(time.time() - t0, 3)


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
            status, error, duration = future.result()
            logger.info("Auth %s %s: %s%s", device.device_name, check_type.upper(), status,
                        f" ({error})" if error else "")

            # Find or create result entry for this device
            existing = next((r for r in results if r.device_name == device.device_name), None)
            if not existing:
                existing = PreflightResult(
                    device_name=device.device_name,
                    device_group=device.device_group,
                )
                results.append(existing)

            if check_type == "bmc":
                existing.bmc_status = status
                existing.bmc_error = error
                existing.bmc_endpoint = f"{device.bmc_ip}:443"
                existing.bmc_username = device.bmc_username
                existing.bmc_duration = duration
            else:
                existing.ssh_status = status
                existing.ssh_error = error
                existing.ssh_endpoint = f"{device.inband_ip}:22"
                existing.ssh_username = device.inband_username
                existing.ssh_duration = duration

            icon = "OK" if status == "AUTH_OK" else "FAIL"
            print(f"    [{icon}] {device.device_name} {check_type.upper()}: {status}"
                  f"{' - ' + error if error else ''}")

    report = PreflightReport(
        results=results,
        total=total,
        bmc_ok=sum(1 for r in results if r.bmc_status == "AUTH_OK"),
        bmc_fail=sum(1 for r in results if r.bmc_status not in ("AUTH_OK", "IP_EMPTY", "CREDENTIAL_EMPTY")),
        ssh_ok=sum(1 for r in results if r.ssh_status == "AUTH_OK"),
        ssh_fail=sum(1 for r in results if r.ssh_status not in ("AUTH_OK", "IP_EMPTY", "CREDENTIAL_EMPTY")),
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
