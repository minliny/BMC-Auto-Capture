"""
DirectDispatchStore — thread-safe in-memory job store for direct dispatch flow.

Tracks: external_task_id, job_id, command_id, status, timing, callback info.
"""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class JobStoreStatus:
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"
    CALLBACK_FAILED = "CALLBACK_FAILED"


@dataclass
class StoredJob:
    external_task_id: str
    job_id: str
    command_id: str
    status: str = JobStoreStatus.ACCEPTED
    received_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    callback_status_url: str = ""
    callback_artifact_url: str = ""
    callback_auth_token: str = ""
    last_callback_error: str = ""
    last_callback_status_code: int = 0
    last_callback_at: float = 0.0
    lock_uri: str = ""
    error: dict[str, Any] | None = None
    result_summary: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


class DirectDispatchStore:
    """Thread-safe in-memory store for direct-dispatch jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, StoredJob] = {}  # keyed by job_id
        self._by_external: dict[str, str] = {}  # external_task_id -> job_id
        self._by_command: dict[str, str] = {}  # command_id -> job_id
        self._mtx = threading.Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create(self, job: StoredJob) -> bool:
        """Insert a new job. Returns False if command_id already exists."""
        with self._mtx:
            if job.command_id and job.command_id in self._by_command:
                return False  # Duplicate command_id
            job.received_at = time.time()
            self._jobs[job.job_id] = job
            if job.external_task_id:
                self._by_external[job.external_task_id] = job.job_id
            if job.command_id:
                self._by_command[job.command_id] = job.job_id
            return True

    def update_status(self, job_id: str, status: str) -> bool:
        with self._mtx:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = status
            return True

    def mark_running(self, job_id: str) -> bool:
        with self._mtx:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = JobStoreStatus.RUNNING
            job.started_at = time.time()
            return True

    def mark_finished(
        self,
        job_id: str,
        status: str,
        duration_ms: int = 0,
        error: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> bool:
        with self._mtx:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = status
            job.finished_at = time.time()
            if duration_ms > 0:
                job.duration_ms = duration_ms
            else:
                job.duration_ms = int((job.finished_at - job.started_at) * 1000)
            if error is not None:
                job.error = error
            if result_summary is not None:
                job.result_summary = result_summary
            if artifacts is not None:
                job.artifacts = artifacts
            return True

    def mark_callback_failed(
        self, job_id: str, error_msg: str, status_code: int = 0
    ) -> bool:
        with self._mtx:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = JobStoreStatus.CALLBACK_FAILED
            job.last_callback_error = error_msg
            job.last_callback_status_code = status_code
            job.last_callback_at = time.time()
            return True

    def cancel(self, job_id: str) -> bool:
        with self._mtx:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = JobStoreStatus.CANCELED
            job.finished_at = time.time()
            return True

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> StoredJob | None:
        with self._mtx:
            return self._jobs.get(job_id)

    def get_by_external_task_id(self, external_task_id: str) -> StoredJob | None:
        with self._mtx:
            jid = self._by_external.get(external_task_id)
            if jid is None:
                return None
            return self._jobs.get(jid)

    def get_by_command_id(self, command_id: str) -> StoredJob | None:
        with self._mtx:
            jid = self._by_command.get(command_id)
            if jid is None:
                return None
            return self._jobs.get(jid)

    def is_command_duplicate(self, command_id: str) -> bool:
        with self._mtx:
            return command_id in self._by_command

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a dict of {job_id: serialized_fields} for all jobs."""
        with self._mtx:
            return {
                jid: {
                    "external_task_id": j.external_task_id,
                    "job_id": j.job_id,
                    "command_id": j.command_id,
                    "status": j.status,
                    "received_at": j.received_at,
                    "started_at": j.started_at,
                    "finished_at": j.finished_at,
                    "duration_ms": j.duration_ms,
                    "callback_status_url": j.callback_status_url,
                    "last_callback_error": j.last_callback_error,
                    "lock_uri": j.lock_uri,
                }
                for jid, j in self._jobs.items()
            }

    def status_counts(self) -> dict[str, int]:
        """Return counts of jobs by status."""
        with self._mtx:
            counts: dict[str, int] = {}
            for j in self._jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            return counts

    def __len__(self) -> int:
        with self._mtx:
            return len(self._jobs)
