"""
API v0.1 core models — dataclass-based, to_dict/from_dict serialization.

All models align with docs/API_CONTRACT_V0_1.md.
No plaintext passwords — only password_ref / secret_ref fields.
Job uses task_snapshot, never command.
"""

from .executor import Executor, ExecutorStatus, ExecutorCapabilities
from .device_snapshot import DeviceSnapshot, ResourceLockEntry
from .task_snapshot import TaskSnapshot, TaskRule, TaskRuleCheck
from .job import Job, JobStatus, StepResult
from .command import Command, CommandType, CommandStatus
from .artifact import Artifact, ArtifactType, ArtifactStatus
from .error_info import ErrorInfo
from .resource_lock import ResourceLock, LockType

__all__ = [
    "Executor",
    "ExecutorStatus",
    "ExecutorCapabilities",
    "DeviceSnapshot",
    "ResourceLockEntry",
    "TaskSnapshot",
    "TaskRule",
    "TaskRuleCheck",
    "Job",
    "JobStatus",
    "StepResult",
    "Command",
    "CommandType",
    "CommandStatus",
    "Artifact",
    "ArtifactType",
    "ArtifactStatus",
    "ErrorInfo",
    "ResourceLock",
    "LockType",
]
