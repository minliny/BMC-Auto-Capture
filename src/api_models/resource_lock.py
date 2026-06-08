"""
ResourceLock model — server-side lock record for a lock_uri.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LockType(str, Enum):
    BMC = "BMC"
    SSH = "SSH"
    SSH_VRP = "SSH_VRP"
    SSH_LINUX = "SSH_LINUX"


@dataclass
class ResourceLock:
    lock_uri: str
    lock_type: LockType = LockType.BMC
    lock_exclusive: bool = True
    holder_job_id: str = ""
    holder_executor_id: str = ""
    acquired_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lock_uri": self.lock_uri,
            "lock_type": self.lock_type.value,
            "lock_exclusive": self.lock_exclusive,
            "holder_job_id": self.holder_job_id,
            "holder_executor_id": self.holder_executor_id,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResourceLock":
        return cls(
            lock_uri=d["lock_uri"],
            lock_type=LockType(d.get("lock_type", "BMC")),
            lock_exclusive=bool(d.get("lock_exclusive", True)),
            holder_job_id=d.get("holder_job_id", ""),
            holder_executor_id=d.get("holder_executor_id", ""),
            acquired_at=d.get("acquired_at", ""),
            expires_at=d.get("expires_at", ""),
        )
