"""Runtime status provider for the Executor API."""

from __future__ import annotations

import time

from src._version import APP_VERSION


class ExecutorRuntimeStatusService:
    """Expose process-level executor status without job/run dispatch state."""

    def __init__(self, executor_id: str = "exec-default"):
        self.executor_id = executor_id
        self._started_at = time.time()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    def get_executor_status(self) -> dict:
        return {
            "executor_id": self.executor_id,
            "status": "ONLINE",
            "version": APP_VERSION,
            "uptime_seconds": self.uptime_seconds,
        }
