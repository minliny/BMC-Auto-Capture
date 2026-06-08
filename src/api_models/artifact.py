"""
Artifact model — output artifact produced by a Job.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArtifactType(str, Enum):
    PNG_SCREENSHOT = "PNG_SCREENSHOT"
    HTML_PAGE = "HTML_PAGE"
    TXT_SSH_OUTPUT = "TXT_SSH_OUTPUT"
    JSON_STRUCTURED = "JSON_STRUCTURED"
    CSV_SUMMARY = "CSV_SUMMARY"
    LOG = "LOG"
    ZIP_BUNDLE = "ZIP_BUNDLE"


class ArtifactStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    STORED = "STORED"
    FAILED = "FAILED"


@dataclass
class Artifact:
    artifact_id: str
    job_id: str = ""
    artifact_type: ArtifactType = ArtifactType.PNG_SCREENSHOT
    relative_path: str = ""
    checksum_sha256: str = ""
    size_bytes: int = 0
    content_type: str = ""
    status: ArtifactStatus = ArtifactStatus.PENDING
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "job_id": self.job_id,
            "artifact_type": self.artifact_type.value,
            "relative_path": self.relative_path,
            "checksum_sha256": self.checksum_sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "status": self.status.value,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        return cls(
            artifact_id=d["artifact_id"],
            job_id=d.get("job_id", ""),
            artifact_type=ArtifactType(d.get("artifact_type", "PNG_SCREENSHOT")),
            relative_path=d.get("relative_path", ""),
            checksum_sha256=d.get("checksum_sha256", ""),
            size_bytes=int(d.get("size_bytes", 0)),
            content_type=d.get("content_type", ""),
            status=ArtifactStatus(d.get("status", "PENDING")),
            created_at=d.get("created_at", ""),
        )
