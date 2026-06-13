"""
Global ResourceRegistry — process-wide singleton for endpoint_key serialization.

Guarantees that within a single process, the same endpoint_key is never held
by more than one holder at a time.  Covers multiple DynamicScheduler instances
within the same process (e.g. multiple API executions).

Optionally backed by FileLock for cross-process safety.  When file_lock is
enabled, the registry acquires both the in-process lease AND the file lock,
providing full multi-process serialization.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("bmc_auto_capture.resource_registry")


class _ResourceLease:
    """A held lease on an endpoint_key."""

    __slots__ = ("holder_metadata", "acquired_at")

    def __init__(self, holder_metadata: dict[str, str]):
        self.holder_metadata = dict(holder_metadata)
        self.acquired_at = time.time()


class _AcquireContext:
    """Context manager returned by ResourceRegistry.acquire()."""

    def __init__(
        self,
        registry: ResourceRegistry,
        endpoint_key: str,
        holder_metadata: dict[str, str],
        timeout: float | None,
    ):
        self._registry = registry
        self._endpoint_key = endpoint_key
        self._holder_metadata = holder_metadata
        self._timeout = timeout

        # Set by __enter__
        self.acquired_at: float = 0.0
        self.reentrant: bool = False
        self.wait_seconds: float = 0.0
        self.endpoint_key: str = endpoint_key
        self._did_acquire = False

    def __enter__(self) -> dict[str, Any]:
        meta = dict(self._holder_metadata)
        meta.setdefault("endpoint_key", self._endpoint_key)
        holder_key = (meta.get("execution_id", ""), meta.get("plan_id", ""))

        wait_start = time.time()

        with self._registry._condition:
            # Reentrant check: same holder already owns this key
            existing = self._registry._leases.get(self._endpoint_key)
            if existing and _holder_key(existing.holder_metadata) == holder_key:
                self.acquired_at = existing.acquired_at
                self.reentrant = True
                self.wait_seconds = 0.0
                self._did_acquire = False
                logger.debug(
                    "[ResourceRegistry] reentrant acquire %s (holder=%s)",
                    self._endpoint_key, holder_key,
                )
                return self._info()

            # Wait until available
            deadline = None if self._timeout is None else time.time() + self._timeout
            while self._endpoint_key in self._registry._leases:
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    held_by = self._registry._leases[self._endpoint_key].holder_metadata
                    wait_sec = time.time() - wait_start
                    raise RuntimeError(
                        f"ResourceRegistry timeout: could not acquire {self._endpoint_key} "
                        f"after {self._timeout}s (waited {wait_sec:.1f}s, "
                        f"held by {_holder_summary(held_by)})"
                    )
                self._registry._condition.wait(timeout=remaining)

            # Acquire
            lease = _ResourceLease(meta)
            self._registry._leases[self._endpoint_key] = lease
            self._did_acquire = True

        # Outside lock: record timing
        self.acquired_at = lease.acquired_at
        self.wait_seconds = round(time.time() - wait_start, 3)
        self.reentrant = False

        logger.info(
            "[ResourceRegistry] acquired %s (holder=%s, wait=%.2fs)",
            self._endpoint_key, holder_key, self.wait_seconds,
        )

        return self._info()

    def __exit__(self, *exc) -> None:
        if not self._did_acquire:
            return
        self._registry._release(self._endpoint_key)

    def _info(self) -> dict[str, Any]:
        return {
            "endpoint_key": self.endpoint_key,
            "acquired_at": self.acquired_at,
            "reentrant": self.reentrant,
            "wait_seconds": self.wait_seconds,
        }


class ResourceRegistry:
    """Process-wide singleton registry for endpoint_key serialization.

    Thread-safe (threading.Condition).  Supports reentrant acquire from the same
    holder (same execution_id + plan_id) to avoid deadlock when scheduler and
    executor both try to acquire the same endpoint_key.

    Optionally backed by FileLock for cross-process safety.  Call
    enable_file_lock() once before use to activate cross-process serialization.
    """

    _instance: ResourceRegistry | None = None
    _instance_lock = threading.Lock()
    _file_lock: object | None = None  # FileLock instance (lazy)

    def __new__(cls) -> ResourceRegistry:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._lock = threading.Lock()
                    inst._leases: dict[str, _ResourceLease] = {}
                    inst._condition = threading.Condition(lock=inst._lock)
                    inst._file_lock_contexts: dict[str, object] = {}
                    cls._instance = inst
        return cls._instance

    @classmethod
    def enable_file_lock(cls, lock_dir: str | None = None) -> None:
        """Enable cross-process file locking for multi-process safety.

        Must be called once before any scheduling.  On first call, a FileLock
        singleton is created.  All subsequent try_hold/acquire calls will also
        acquire the file lock.

        Args:
            lock_dir: Directory for lock files.  None = system temp dir.
        """
        from .file_lock import FileLock
        if cls._file_lock is None:
            cls._file_lock = FileLock(lock_dir)
            import logging
            _log = logging.getLogger("bmc_auto_capture.resource_registry")
            _log.info(
                "FileLock enabled for cross-process safety (dir=%s)",
                cls._file_lock._lock_dir if hasattr(cls._file_lock, '_lock_dir') else lock_dir,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        endpoint_key: str,
        holder_metadata: dict[str, str] | None = None,
        timeout: float | None = 300.0,
    ) -> _AcquireContext:
        """Return a context manager that acquires endpoint_key on enter, releases on exit.

        Reentrant: if the same (execution_id, plan_id) already holds this key,
        acquisition succeeds immediately without waiting.

        Raises RuntimeError if timeout expires before acquisition.
        """
        return _AcquireContext(
            self,
            endpoint_key,
            holder_metadata or {},
            timeout,
        )

    def is_held(self, endpoint_key: str) -> bool:
        with self._lock:
            return endpoint_key in self._leases

    def try_hold(self, endpoint_key: str, holder_metadata: dict[str, str]) -> bool:
        """Non-blocking: acquire endpoint_key if available. Returns True on success."""
        with self._lock:
            if endpoint_key in self._leases:
                return False

            # Cross-process file lock (if enabled)
            fl_ctx = None
            if ResourceRegistry._file_lock is not None:
                fl_ctx = ResourceRegistry._file_lock.try_acquire(endpoint_key)
                if fl_ctx is None:
                    logger.debug(
                        "[ResourceRegistry] try_hold %s BLOCKED by file lock (another process)",
                        endpoint_key,
                    )
                    return False
                self._file_lock_contexts[endpoint_key] = fl_ctx

            self._leases[endpoint_key] = _ResourceLease(dict(holder_metadata))
            logger.debug("[ResourceRegistry] try_hold %s (holder=%s)", endpoint_key, holder_metadata.get("plan_id", "?"))
            return True

    def release(self, endpoint_key: str) -> None:
        """Release a held endpoint_key. Thread-safe. Also closes cross-process file lock."""
        # P0-5: release must also close file lock contexts (same as _release)
        self._release(endpoint_key)

    def wait_and_hold(self, endpoint_key: str, holder_metadata: dict[str, str], timeout: float | None = None) -> bool:
        """Blocking: wait until endpoint_key is available, then acquire.

        Returns True on success, False on timeout.
        Does NOT support reentrancy (use acquire() for that).

        If FileLock is enabled, also acquires the cross-process file lock.
        """
        deadline = None if timeout is None else time.time() + timeout
        with self._condition:
            while endpoint_key in self._leases:
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

            # Cross-process file lock (if enabled)
            fl_ctx = None
            if ResourceRegistry._file_lock is not None:
                # Use blocking file lock with remaining timeout
                fl_timeout = None
                if deadline is not None:
                    fl_timeout = max(0, deadline - time.time())
                fl_ctx = ResourceRegistry._file_lock.try_acquire(endpoint_key)
                # If try_acquire fails, retry with short sleep until deadline
                while fl_ctx is None:
                    if deadline is not None and time.time() >= deadline:
                        return False
                    time.sleep(0.1)
                    fl_ctx = ResourceRegistry._file_lock.try_acquire(endpoint_key)
                self._file_lock_contexts[endpoint_key] = fl_ctx

            self._leases[endpoint_key] = _ResourceLease(dict(holder_metadata))
            logger.debug("[ResourceRegistry] wait_and_hold %s (holder=%s)", endpoint_key, holder_metadata.get("plan_id", "?"))
            return True

    def current_holder(self, endpoint_key: str) -> dict[str, str] | None:
        with self._lock:
            lease = self._leases.get(endpoint_key)
            if lease:
                return dict(lease.holder_metadata)
            return None

    @property
    def held_keys(self) -> list[str]:
        with self._lock:
            return list(self._leases.keys())

    @property
    def active_lease_count(self) -> int:
        with self._lock:
            return len(self._leases)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _release(self, endpoint_key: str) -> None:
        """Release a held endpoint_key."""
        with self._condition:
            if endpoint_key in self._leases:
                del self._leases[endpoint_key]
                logger.debug("[ResourceRegistry] released %s", endpoint_key)
                self._condition.notify_all()
            else:
                logger.debug(
                    "[ResourceRegistry] release skipped for %s (not held)",
                    endpoint_key,
                )

        # Release cross-process file lock (if any)
        fl_ctx = self._file_lock_contexts.pop(endpoint_key, None)
        if fl_ctx is not None:
            try:
                fl_ctx._close()
            except Exception:
                pass

    def _reset_for_test(self) -> None:
        """Clear all leases — test use only."""
        with self._lock:
            self._leases.clear()


def _holder_key(meta: dict[str, str]) -> tuple[str, str]:
    return (meta.get("execution_id", ""), meta.get("plan_id", ""))


def _holder_summary(meta: dict[str, str]) -> str:
    return (
        f"execution={meta.get('execution_id', '?')} "
        f"plan={meta.get('plan_id', '?')} "
        f"device={meta.get('device_name', '?')} "
        f"task={meta.get('task_name', '?')}"
    )
