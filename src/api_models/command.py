"""
Command — unified control protocol between server and executor.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandType(str, Enum):
    ASSIGN_JOB = "ASSIGN_JOB"
    CANCEL_JOB = "CANCEL_JOB"
    PING = "PING"
    # v0.2+
    ASSIGN_RUN = "ASSIGN_RUN"
    CANCEL_RUN = "CANCEL_RUN"
    PAUSE_RUN = "PAUSE_RUN"
    RESUME_RUN = "RESUME_RUN"
    UPDATE_CONFIG = "UPDATE_CONFIG"
    REQUEST_ARTIFACT = "REQUEST_ARTIFACT"


class CommandStatus(str, Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACKED = "ACKED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class Command:
    command_id: str
    command_type: CommandType
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str = ""

    # Convenience: extract job_id from payload when command_type is JOB-related
    @property
    def job_id(self) -> str:
        return self.payload.get("job_id", "")

    @property
    def run_id(self) -> str:
        return self.payload.get("run_id", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type.value,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Command":
        return cls(
            command_id=d["command_id"],
            command_type=CommandType(d.get("command_type", "PING")),
            payload=dict(d.get("payload", d.get("job", {}))),
            created_at=d.get("created_at", ""),
            expires_at=d.get("expires_at", ""),
        )

    def is_expired(self, now: str = "") -> bool:
        """Check if command is past its expires_at. Caller should pass current ISO timestamp."""
        if not self.expires_at:
            return False
        if not now:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return now > self.expires_at
