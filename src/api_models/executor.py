"""
Executor model — execution node identity + capacity.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutorStatus(str, Enum):
    REGISTERING = "REGISTERING"
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    DRAINING = "DRAINING"
    UNRESPONSIVE = "UNRESPONSIVE"
    OFFLINE = "OFFLINE"


@dataclass
class ExecutorCapabilities:
    max_bmc_workers: int = 4
    max_ssh_workers: int = 8
    bmc_worker_slots_free: int = 0
    ssh_worker_slots_free: int = 0
    supported_protocols: list[str] = field(default_factory=lambda: ["BMC", "SSH"])
    known_lock_uris: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_bmc_workers": self.max_bmc_workers,
            "max_ssh_workers": self.max_ssh_workers,
            "bmc_worker_slots_free": self.bmc_worker_slots_free,
            "ssh_worker_slots_free": self.ssh_worker_slots_free,
            "supported_protocols": list(self.supported_protocols),
            "known_lock_uris": list(self.known_lock_uris),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecutorCapabilities":
        return cls(
            max_bmc_workers=int(d.get("max_bmc_workers", 4)),
            max_ssh_workers=int(d.get("max_ssh_workers", 8)),
            bmc_worker_slots_free=int(d.get("bmc_worker_slots_free", 0)),
            ssh_worker_slots_free=int(d.get("ssh_worker_slots_free", 0)),
            supported_protocols=list(d.get("supported_protocols", ["BMC", "SSH"])),
            known_lock_uris=list(d.get("known_lock_uris", [])),
        )


@dataclass
class Executor:
    executor_id: str
    hostname: str = ""
    ip: str = ""
    os: str = ""
    version: str = "0.2.5"
    status: ExecutorStatus = ExecutorStatus.REGISTERING
    capabilities: ExecutorCapabilities = field(default_factory=ExecutorCapabilities)
    registered_at: str = ""
    last_heartbeat_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "hostname": self.hostname,
            "ip": self.ip,
            "os": self.os,
            "version": self.version,
            "status": self.status.value,
            "capabilities": self.capabilities.to_dict(),
            "registered_at": self.registered_at,
            "last_heartbeat_at": self.last_heartbeat_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Executor":
        return cls(
            executor_id=d["executor_id"],
            hostname=d.get("hostname", ""),
            ip=d.get("ip", ""),
            os=d.get("os", ""),
            version=d.get("version", "0.2.5"),
            status=ExecutorStatus(d.get("status", "REGISTERING")),
            capabilities=ExecutorCapabilities.from_dict(d.get("capabilities", {})),
            registered_at=d.get("registered_at", ""),
            last_heartbeat_at=d.get("last_heartbeat_at", ""),
        )
