"""
DeviceSnapshot — the device info embedded in a Job's device_snapshot field.
Contains resource_locks + credential refs. No plaintext passwords.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceLockEntry:
    """A single resource lock descriptor within a DeviceSnapshot."""
    lock_uri: str
    lock_type: str  # BMC, SSH, SSH_VRP, SSH_LINUX
    lock_scope: str = "oob"  # "oob" | "inband"
    lock_exclusive: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "lock_uri": self.lock_uri,
            "lock_type": self.lock_type,
            "lock_scope": self.lock_scope,
            "lock_exclusive": self.lock_exclusive,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResourceLockEntry":
        return cls(
            lock_uri=d["lock_uri"],
            lock_type=d.get("lock_type", ""),
            lock_scope=d.get("lock_scope", "oob"),
            lock_exclusive=bool(d.get("lock_exclusive", True)),
        )


@dataclass
class DeviceSnapshot:
    """Device info carried inside a Job. No plaintext credentials."""
    device_id: str
    device_name: str = ""
    device_group: str = ""
    ssh_type: str = ""  # SSH | SSH_VRP | SSH_LINUX
    oob_ip: str = ""
    oob_port: int = 443
    inband_ip: str = ""
    inband_port: int = 22
    bmc_username: str = ""
    bmc_password_ref: str = ""
    ssh_username: str = ""
    ssh_password_ref: str = ""
    resource_locks: list[ResourceLockEntry] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "device_group": self.device_group,
            "ssh_type": self.ssh_type,
            "oob_ip": self.oob_ip,
            "oob_port": self.oob_port,
            "inband_ip": self.inband_ip,
            "inband_port": self.inband_port,
            "bmc_username": self.bmc_username,
            "bmc_password_ref": self.bmc_password_ref,
            "ssh_username": self.ssh_username,
            "ssh_password_ref": self.ssh_password_ref,
            "resource_locks": [rl.to_dict() for rl in self.resource_locks],
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceSnapshot":
        locks = [ResourceLockEntry.from_dict(rl) for rl in d.get("resource_locks", [])]
        return cls(
            device_id=d["device_id"],
            device_name=d.get("device_name", ""),
            device_group=d.get("device_group", ""),
            ssh_type=d.get("ssh_type", ""),
            oob_ip=d.get("oob_ip", ""),
            oob_port=int(d.get("oob_port", 443)),
            inband_ip=d.get("inband_ip", ""),
            inband_port=int(d.get("inband_port", 22)),
            bmc_username=d.get("bmc_username", ""),
            bmc_password_ref=d.get("bmc_password_ref", ""),
            ssh_username=d.get("ssh_username", ""),
            ssh_password_ref=d.get("ssh_password_ref", ""),
            resource_locks=locks,
            enabled=bool(d.get("enabled", True)),
        )

    @property
    def has_oob(self) -> bool:
        return bool(self.oob_ip)

    @property
    def has_inband(self) -> bool:
        return bool(self.inband_ip)
