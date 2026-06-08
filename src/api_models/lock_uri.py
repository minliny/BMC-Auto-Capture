"""
lock_uri derivation — unified function to compute resource lock URIs.

Rules (mandatory, no fallback to device_name):
  BMC_URL / BMC_ACTIONS          → bmc://{oob_ip}
  SSH_CMD + ssh_type=SSH         → ssh://{inband_ip}
  SSH_CMD + ssh_type=SSH_LINUX   → ssh-linux://{inband_ip}
  SSH_CMD + ssh_type=SSH_VRP     → ssh-vrp://{inband_ip}
"""

from __future__ import annotations
from typing import Protocol


class LockUriDerivationError(ValueError):
    """Raised when lock_uri cannot be derived due to missing required IP."""


class DeviceLike(Protocol):
    """Minimal protocol for objects that can provide lock_uri inputs."""
    bmc_ip: str
    inband_ip: str
    device_name: str
    device_group: str


def _normalize_ip(ip: str) -> str:
    return (ip or "").strip()


def derive_lock_uri(
    oob_ip: str = "",
    inband_ip: str = "",
    execution_mode: str = "",
    ssh_type: str = "",
    lock_type: str = "",
) -> str:
    """Derive a lock_uri from connection parameters.

    Args:
        oob_ip: Out-of-band management IP (for BMC).
        inband_ip: In-band management IP (for SSH).
        execution_mode: BMC_URL | BMC_ACTIONS | SSH_CMD.
        ssh_type: SSH | SSH_VRP | SSH_LINUX (only used for SSH_CMD).
        lock_type: BMC | SSH | SSH_VRP | SSH_LINUX (explicit override).

    Returns:
        Normalized lock_uri string.

    Raises:
        LockUriDerivationError: If the required IP is missing.
    """
    # Explicit lock_type override takes precedence
    if lock_type:
        lt = lock_type.upper()
        if lt == "BMC":
            ip = _normalize_ip(oob_ip)
            if not ip:
                raise LockUriDerivationError(
                    "lock_type=BMC but oob_ip is empty — cannot derive bmc:// lock_uri"
                )
            return f"bmc://{ip}"
        elif lt in ("SSH", "SSH_VRP", "SSH_LINUX"):
            ip = _normalize_ip(inband_ip)
            if not ip:
                raise LockUriDerivationError(
                    f"lock_type={lt} but inband_ip is empty — cannot derive ssh:// lock_uri"
                )
            prefix = lt.lower().replace("_", "-")
            return f"{prefix}://{ip}"
        else:
            raise LockUriDerivationError(f"Unknown lock_type: {lock_type!r}")

    # Derive from execution_mode + ssh_type
    mode = (execution_mode or "").upper()
    st = (ssh_type or "").upper()

    if mode in ("BMC_URL", "BMC_ACTIONS"):
        ip = _normalize_ip(oob_ip)
        if not ip:
            raise LockUriDerivationError(
                f"execution_mode={mode} but oob_ip is empty — cannot derive bmc:// lock_uri"
            )
        return f"bmc://{ip}"

    if mode == "SSH_CMD":
        ip = _normalize_ip(inband_ip)
        if not ip:
            raise LockUriDerivationError(
                "execution_mode=SSH_CMD but inband_ip is empty — cannot derive ssh:// lock_uri"
            )
        if st == "SSH_LINUX":
            return f"ssh-linux://{ip}"
        elif st == "SSH_VRP":
            return f"ssh-vrp://{ip}"
        else:
            return f"ssh://{ip}"

    # Fallback: try task_type-based derivation
    if mode in ("BMC",):
        ip = _normalize_ip(oob_ip)
        if not ip:
            raise LockUriDerivationError(
                f"execution_mode={mode} but oob_ip is empty"
            )
        return f"bmc://{ip}"

    if mode in ("SSH", "TELNET"):
        ip = _normalize_ip(inband_ip)
        if not ip:
            raise LockUriDerivationError(
                f"execution_mode={mode} but inband_ip is empty"
            )
        return f"ssh://{ip}"

    raise LockUriDerivationError(
        f"Cannot derive lock_uri: execution_mode={mode!r}, "
        f"ssh_type={st!r}, lock_type={lock_type!r}. "
        f"oob_ip={oob_ip!r}, inband_ip={inband_ip!r}"
    )


def derive_lock_uri_from_device(
    device: DeviceLike,
    execution_mode: str = "",
    ssh_type: str = "",
    lock_type: str = "",
) -> str:
    """Convenience wrapper: derive lock_uri from a Device-like object.

    Never falls back to device_name.
    Raises LockUriDerivationError if IP is missing.
    """
    return derive_lock_uri(
        oob_ip=device.bmc_ip,
        inband_ip=device.inband_ip,
        execution_mode=execution_mode,
        ssh_type=ssh_type or getattr(device, "ssh_type", ""),
        lock_type=lock_type,
    )


def is_valid_lock_uri(uri: str) -> bool:
    """Check if a string looks like a valid lock_uri."""
    if not uri or " " in uri:
        return False
    valid_prefixes = ("bmc://", "ssh://", "ssh-vrp://", "ssh-linux://")
    return any(uri.startswith(p) for p in valid_prefixes) and len(uri) > len("bmc://x")
