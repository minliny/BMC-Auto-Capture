from __future__ import annotations

"""
Resizable worker pool — thread-based pool that can scale up/down gracefully.

Scaling down never kills a running worker — it only stops creating replacements
when workers naturally complete.
"""

import logging
import threading
import time
import weakref
from concurrent.futures import thread as futures_thread
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Callable, Any
import os

logger = logging.getLogger("bmc_auto_capture.workerpool")


@dataclass(frozen=True)
class DispatchResult:
    committed: bool
    future: Future | None
    resource_key: str


@dataclass
class ShutdownResult:
    """Result of a WorkerPool shutdown call."""
    graceful: bool = False
    timed_out: bool = False
    active_futures_cancelled: int = 0
    active_futures_remaining: int = 0
    running_resources_cleared: int = 0



class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are all daemon.

    Python's ThreadPoolExecutor creates non-daemon threads by default,
    which prevents the interpreter from exiting until all workers have
    completed.  This subclass overrides _adjust_thread_count so that
    every worker thread is created with daemon=True.
    """

    def _adjust_thread_count(self):
        """Create daemon workers without registering them for interpreter join."""
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, queue=self._work_queue):
            queue.put(None)

        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return

        thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
        worker = threading.Thread(
            name=thread_name,
            target=futures_thread._worker,
            args=(
                weakref.ref(self, weakref_cb),
                self._work_queue,
                self._initializer,
                self._initargs,
            ),
            daemon=True,
        )
        worker.start()
        self._threads.add(worker)


class DispatchNotCommittedError(RuntimeError):
    """Dispatch failed before executor.submit() committed a worker."""


class WorkerPool:
    """Wraps ThreadPoolExecutor with dynamic resize capability."""

    def __init__(self, name: str, base_workers: int, max_workers: int):
        self.name = name
        self._base = base_workers
        self._max = max_workers
        self._target = base_workers
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._running_resources: set[str] = set()
        self._active_futures: dict[Future, str] = {}  # future -> resource_key (endpoint_key)

    @property
    def target_size(self) -> int:
        return self._target

    @property
    def max_size(self) -> int:
        return self._max

    def start(self):
        if self._executor is None:
            # Create a DaemonThreadPoolExecutor — all worker threads are daemon,
            # so the interpreter can exit even if a worker is stuck.
            self._executor = DaemonThreadPoolExecutor(
                max_workers=self._target,
                thread_name_prefix=self.name,
            )
            logger.info("[%s] 工作池已启动, 线程数= %d workers", self.name, self._target)

    def resize(self, new_target: int):
        new_target = max(1, min(new_target, self._max))
        old = self._target
        self._target = new_target
        if new_target != old and self._executor is not None:
            self._executor._max_workers = new_target
            logger.info("[%s] 工作池已调整: %d → %d", self.name, old, new_target)

    def has_idle(self) -> bool:
        """Check if pool has capacity for more work."""
        if self._executor is None:
            return True
        return len(self._active_futures) < self._target

    def resource_has_running_task(self, endpoint_key: str) -> bool:
        """Check if this endpoint_key is currently running in this pool."""
        with self._lock:
            return endpoint_key in self._running_resources

    def dispatch(
        self, fn: Callable, resource_key: str, on_complete: Callable | None = None,
    ) -> DispatchResult:
        """Submit work with an explicit commit point.

        This method raises only when no worker was submitted. Once submit()
        succeeds, callback-registration failures are recovered internally and
        must not cause the scheduler to release/requeue the endpoint.
        """
        if self._executor is None:
            try:
                self.start()
            except Exception as exc:
                raise DispatchNotCommittedError(
                    f"worker pool start failed for {resource_key}: {exc}"
                ) from exc

        with self._lock:
            self._running_resources.add(resource_key)

        try:
            future = self._executor.submit(fn)
        except Exception as exc:
            with self._lock:
                self._running_resources.discard(resource_key)
            raise DispatchNotCommittedError(
                f"worker submit failed for {resource_key}: {exc}"
            ) from exc

        # submit() succeeded: dispatch is committed from this point onward.
        with self._lock:
            self._active_futures[future] = resource_key

        callback = (
            (lambda f: self._on_done(f, resource_key, on_complete))
            if on_complete
            else (lambda f: self._on_done(f, resource_key))
        )
        try:
            future.add_done_callback(callback)
        except Exception:
            logger.critical(
                "[%s] DISPATCH_COMMITTED_CALLBACK_REGISTRATION_FAILED "
                "resource=%s; starting recovery waiter",
                self.name,
                resource_key,
                exc_info=True,
            )
            recovery = threading.Thread(
                target=self._recover_committed_future,
                args=(future, resource_key, on_complete),
                name=f"{self.name}-dispatch-recovery",
                daemon=True,
            )
            recovery.start()

        return DispatchResult(
            committed=True,
            future=future,
            resource_key=resource_key,
        )

    def shutdown(
        self, wait: bool = True, shutdown_timeout: float = 30.0,
    ) -> ShutdownResult:
        """Shut down the pool with timeout protection.

        Parameters
        ----------
        wait:
            If True, wait for active futures to complete (up to *shutdown_timeout*).
            If False, return immediately (futures are cancelled).
        shutdown_timeout:
            Maximum seconds to wait for active futures. Futures still running
            after the timeout have their resource tracking cleaned up so the
            pool does not block the caller. Running worker threads are NOT
            killed (no thread.kill in Python), but their completion callbacks
            will find no entry in _active_futures and return early.

        Returns
        -------
        ShutdownResult indicating whether the shutdown was graceful, timed out,
        and how many futures/resources were affected.

        Idempotent: calling shutdown() multiple times is safe.
        """
        if self._executor is None:
            return ShutdownResult(graceful=True)

        executor = self._executor
        # Prevent further dispatch while shutting down — this also marks
        # the pool as "shut down" for idempotent re-entry.
        self._executor = None

        # Phase 1: prevent new submissions and cancel pending futures
        executor.shutdown(wait=False, cancel_futures=True)

        if not wait:
            # Immediate return — remove all tracking state
            still_active = len(self._active_futures)
            self._active_futures.clear()
            self._running_resources.clear()
            logger.info(
                "[%s] 工作池已立即关闭 (wait=False, "
                "cancelled_pending=%d still_active=%d)",
                self.name, len(self._active_futures), still_active,
            )
            return ShutdownResult(
                timed_out=False,
                active_futures_remaining=still_active,
            )

        # Phase 2: wait for running futures to complete
        deadline = time.monotonic() + max(shutdown_timeout, 0.1)
        poll_interval = 0.1
        timed_out = False

        while self._active_futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(poll_interval, remaining))

        still_active = len(self._active_futures)
        cancelled_extra = 0

        if timed_out or still_active > 0:
            # Timeout reached — cancel what we can, clear tracking state
            for fut in list(self._active_futures.keys()):
                if not fut.done():
                    try:
                        if fut.cancel():
                            cancelled_extra += 1
                    except Exception:
                        pass
            self._active_futures.clear()
            remaining_resources = len(self._running_resources)
            self._running_resources.clear()

            logger.warning(
                "[%s] WORKERPOOL_SHUTDOWN_TIMEOUT after %.1fs — "
                "cancelled=%d still_active=%d resources_cleared=%d",
                self.name, shutdown_timeout,
                cancelled_extra, still_active, remaining_resources,
            )
            return ShutdownResult(
                graceful=False,
                timed_out=True,
                active_futures_cancelled=cancelled_extra,
                active_futures_remaining=still_active,
                running_resources_cleared=remaining_resources,
            )

        completed = len(self._active_futures)
        logger.info(
            "[%s] 工作池已优雅关闭 (wait=True, timeout=%.1f, "
            "graceful=True)",
            self.name, shutdown_timeout,
        )
        return ShutdownResult(
            graceful=True,
            timed_out=False,
            active_futures_cancelled=0,
            active_futures_remaining=0,
            running_resources_cleared=0,
        )

    def _on_done(self, future: Future, resource_key: str, callback: Callable | None = None):
        with self._lock:
            # Callback registration and recovery may race. Only one path owns
            # completion and invokes the user callback.
            if future not in self._active_futures:
                return
            self._running_resources.discard(resource_key)
            self._active_futures.pop(future, None)

        if not callback:
            return

        try:
            result = future.result()
        except Exception as e:
            logger.error("[%s] 工作线程异常, 资源: %s: %s", self.name, resource_key, e)
            # Synthesize an error result so callback always fires
            from ..models.execution_result import ExecutionResult
            result = ExecutionResult(
                plan_id="",
                device_name=resource_key,
                task_name="(crashed)",
                execution_status="EXEC_ERROR",
                execution_failure_reason=f"Worker exception: {e}",
                started_at=time.time(),
                ended_at=time.time(),
            )

        try:
            callback(result)
        except Exception as e:
            logger.error("[%s] Callback error for resource %s: %s", self.name, resource_key, e)

    def _recover_committed_future(
        self, future: Future, resource_key: str, callback: Callable | None,
    ) -> None:
        """Recover completion when add_done_callback() failed after submit."""
        try:
            future.result()
        except BaseException:
            # _on_done() converts worker exceptions to an ExecutionResult.
            pass
        self._on_done(future, resource_key, callback)
