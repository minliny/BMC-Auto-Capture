"""
ErrorInfo — structured error returned in Job.finish or event payloads.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ErrorInfo:
    code: str
    message: str = ""
    retryable: bool = False
    category: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "category": self.category,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ErrorInfo":
        return cls(
            code=d.get("code", ""),
            message=d.get("message", ""),
            retryable=bool(d.get("retryable", False)),
            category=d.get("category", ""),
            details=dict(d.get("details", {})),
        )
