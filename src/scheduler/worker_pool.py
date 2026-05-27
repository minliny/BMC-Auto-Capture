from __future__ import annotations

"""
Resizable worker pool — thread-based pool that can scale up/down gracefully.

Scaling down never kills a running worker — it only stops creating replacements
when workers naturally complete.
"""

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Any

logger = logging.getLogger("bmc_auto_capture.workerpool")


class WorkerPool:
    """Wraps ThreadPoolExecutor with dynamic resize capability."""

    def __init__(self, name: str, base_workers: int, max_workers: int):
        self.name = name
        self._base = base_workers
        self._max = max_workers
        self._target = base_workers
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._running_devices: set[str] = set()
        self._active_futures: dict[Future, str] = {}  # future -> device_id

    @property
    def target_size(self) -> int:
        return self._target

    @property
    def max_size(self) -> int:
        return self._max

    def start(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._target, thread_name_prefix=self.name)
            logger.info("[%s] Pool started with %d workers", self.name, self._target)

    def resize(self, new_target: int):
        new_target = max(1, min(new_target, self._max))
        old = self._target
        self._target = new_target
        if new_target != old and self._executor is not None:
            self._executor._max_workers = new_target
            logger.info("[%s] Pool resized: %d → %d", self.name, old, new_target)

    def has_idle(self) -> bool:
        """Check if pool has capacity for more work."""
        if self._executor is None:
            return True
        return len(self._active_futures) < self._target

    def device_has_running_task(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._running_devices

    def dispatch(self, fn: Callable, device_id: str, on_complete: Callable | None = None):
        """Submit work. device_id enforces per-device serialization."""
        if self._executor is None:
            self.start()

        with self._lock:
            self._running_devices.add(device_id)

        future = self._executor.submit(fn)
        with self._lock:
            self._active_futures[future] = device_id

        if on_complete:
            future.add_done_callback(lambda f: self._on_done(f, device_id, on_complete))
        else:
            future.add_done_callback(lambda f: self._on_done(f, device_id))

    def shutdown(self):
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
            logger.info("[%s] Pool shut down", self.name)

    def _on_done(self, future: Future, device_id: str, callback: Callable | None = None):
        with self._lock:
            self._running_devices.discard(device_id)
            self._active_futures.pop(future, None)

        if not callback:
            return

        try:
            result = future.result()
        except Exception as e:
            logger.error("[%s] Worker crashed for device %s: %s", self.name, device_id, e)
            # Synthesize an error result so callback always fires
            from ..models.execution_result import ExecutionResult
            result = ExecutionResult(
                device_name=device_id,
                task_name="(crashed)",
                execution_status="EXEC_ERROR",
                execution_failure_reason=f"Worker exception: {e}",
                started_at=time.time(),
                ended_at=time.time(),
            )

        try:
            callback(result)
        except Exception as e:
            logger.error("[%s] Callback error for device %s: %s", self.name, device_id, e)
