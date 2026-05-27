from __future__ import annotations

"""
Dynamic Scheduler — the core scheduling loop.

Features:
- Device-serialized execution (max 1 task per device at a time)
- Protocol-split pools (BMC vs SSH)
- Dynamic pool resizing based on CPU/memory
- Graceful pause/resume/stop
- Event-driven status reporting
"""

import logging
import threading
import time
from collections import deque
from typing import Callable

from ..models.app_config import AppConfig
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult
from ..executor.ssh_executor import SSHExecutor
from ..executor.bmc_executor import BMCExecutor
from ..executor.browser_manager import BrowserManager
from .resource_monitor import ResourceMonitor
from .worker_pool import WorkerPool

logger = logging.getLogger("bmc_auto_capture.scheduler")


class DynamicScheduler:
    """Unified scheduler with dynamic concurrency and device serialization."""

    def __init__(
        self,
        config: AppConfig,
        event_bus=None,
    ):
        self._config = config
        self._event_bus = event_bus

        # Device queues: one FIFO per device
        self._device_queues: dict[str, deque[TaskPlan]] = {}
        self._ready_devices: deque[str] = deque()

        # Results
        self._results: list[ExecutionResult] = []
        self._results_lock = threading.Lock()

        # Control
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # Pools
        self._bmc_pool = WorkerPool("bmc", config.base_bmc_workers, config.max_bmc_workers)
        self._ssh_pool = WorkerPool("ssh", config.base_ssh_workers, config.max_ssh_workers)

        # Resource monitor
        self._monitor = ResourceMonitor(interval=config.resource_check_interval)

        # Executors (created per worker — lazy init via factory)
        self._bm: BrowserManager | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, plans: list[TaskPlan]) -> list[ExecutionResult]:
        self._build_device_queues(plans)
        self._monitor.start()
        self._bmc_pool.start()
        self._ssh_pool.start()

        # Browser manager — shared across BMC workers (one for now; each worker uses its own page)
        self._bm = BrowserManager(
            headless=self._config.browser_headless,
            max_tasks=self._config.browser_max_tasks_before_recycle,
            max_age_seconds=self._config.browser_max_age_seconds,
        )

        total = len(plans)
        logger.info("DynamicScheduler: %d plans, %d devices", total, len(self._device_queues))

        last_progress_at = time.time()
        last_heartbeat_at = time.time()
        completed_count = 0

        try:
            while not self._stop_event.is_set() and self._has_remaining_work():
                self._pause_event.wait()

                # 1. Adjust pools
                cpu, mem = self._monitor.latest
                self._adjust_pools(cpu, mem)

                # 2. Dispatch
                self._dispatch()

                # 3. Heartbeat + stall detection
                now = time.time()
                cur_completed = len(self._results)
                if cur_completed > completed_count:
                    completed_count = cur_completed
                    last_progress_at = now

                # Heartbeat every 30s
                if now - last_heartbeat_at >= 30:
                    last_heartbeat_at = now
                    pending = sum(len(q) for q in self._device_queues.values())
                    running_bmc = len(self._bmc_pool._active_futures)
                    running_ssh = len(self._ssh_pool._active_futures)
                    ready = len(self._ready_devices)
                    locked_bmc = len(self._bmc_pool._running_devices)
                    locked_ssh = len(self._ssh_pool._running_devices)
                    logger.info(
                        "HEARTBEAT: completed=%d pending=%d running(bmc=%d ssh=%d) ready=%d devices_locked(bmc=%d ssh=%d)",
                        completed_count, pending, running_bmc, running_ssh, ready, locked_bmc, locked_ssh,
                    )
                    print(f"  -- heartbeat: done={completed_count} pending={pending} "
                          f"running(bmc={running_bmc} ssh={running_ssh}) ready={ready} "
                          f"locked(bmc={locked_bmc} ssh={locked_ssh}) --")

                # Stall detection: no progress for 60s
                if now - last_progress_at >= 60:
                    logger.warning("SCHEDULER STALL: no progress for %.0fs", now - last_progress_at)
                    # Dump first 10 pending plans with their status
                    pending_dumped = 0
                    for did, q in sorted(self._device_queues.items()):
                        if q and pending_dumped < 10:
                            p = q[0]
                            blocked_by = ""
                            if self._bmc_pool.device_has_running_task(did):
                                blocked_by = " (device busy in BMC pool)"
                            elif self._ssh_pool.device_has_running_task(did):
                                blocked_by = " (device busy in SSH pool)"
                            logger.warning(
                                "  PENDING: device=%s task=%s protocol=%s%s",
                                did, p.task.task_name, p.protocol, blocked_by,
                            )
                            pending_dumped += 1
                    # Dump device locks
                    logger.warning("  BMC active devices: %s", list(self._bmc_pool._running_devices))
                    logger.warning("  SSH active devices: %s", list(self._ssh_pool._running_devices))
                    logger.warning("  Ready devices: %d, total device queues: %d",
                                   len(self._ready_devices), len(self._device_queues))

                # 4. Brief sleep
                time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — stopping scheduler")
        finally:
            self._drain()
            self._bmc_pool.shutdown()
            self._ssh_pool.shutdown()
            self._monitor.stop()

            import asyncio
            if self._bm:
                try:
                    asyncio.run(self._bm.teardown())
                except Exception:
                    pass

        return self._results

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # Unblock pause so stop can take effect

    def pause(self):
        self._pause_event.clear()
        logger.info("Scheduler paused")

    def resume(self):
        self._pause_event.set()
        logger.info("Scheduler resumed")

    @property
    def results(self) -> list[ExecutionResult]:
        with self._results_lock:
            return list(self._results)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _build_device_queues(self, plans: list[TaskPlan]):
        for plan in plans:
            did = plan.device_id
            if did not in self._device_queues:
                self._device_queues[did] = deque()
            self._device_queues[did].append(plan)

        # Populate ready list
        for did in self._device_queues:
            self._ready_devices.append(did)

    def _has_remaining_work(self) -> bool:
        for q in self._device_queues.values():
            if q:
                return True
        return False

    def _adjust_pools(self, cpu: float, mem: float):
        scale = self._compute_scale(cpu, mem)
        target_bmc = max(1, int(self._config.base_bmc_workers * scale))
        target_ssh = max(1, int(self._config.base_ssh_workers * scale))
        self._bmc_pool.resize(min(target_bmc, self._config.max_bmc_workers))
        self._ssh_pool.resize(min(target_ssh, self._config.max_ssh_workers))

    def _compute_scale(self, cpu: float, mem: float) -> float:
        if mem > self._config.mem_emergency_pct or cpu > self._config.cpu_emergency_pct:
            return 0.3
        if mem > self._config.mem_scale_down_pct or cpu > self._config.cpu_scale_down_pct:
            return 0.6
        if mem < self._config.mem_scale_up_pct and cpu < self._config.cpu_scale_up_pct:
            return 1.3
        return 1.0

    def _dispatch(self):
        for pool, protocol in [(self._bmc_pool, "BMC"), (self._ssh_pool, "SSH")]:
            while pool.has_idle() and self._ready_devices:
                device_id = self._ready_devices.popleft()

                if pool.device_has_running_task(device_id):
                    self._ready_devices.append(device_id)  # Back of line
                    continue

                q = self._device_queues.get(device_id)
                if not q:
                    continue  # Queue drained by another thread, skip

                # Pop task NOW (main thread) to prevent race with worker-thread callback
                plan = q.popleft()

                if plan.protocol != protocol:
                    # Wrong pool — push back and re-add device
                    q.appendleft(plan)
                    self._ready_devices.append(device_id)
                    continue

                plan.status = "RUNNING"
                plan.started_at = time.time()

                logger.info("START [%s] %s / %s", plan.protocol, plan.device.device_name, plan.task.task_name)
                print(f"  START [{plan.protocol}] {plan.device.device_name}  {plan.task.task_name}")

                if self._event_bus:
                    self._event_bus.emit("plan_started", plan=plan)

                try:
                    pool.dispatch(
                        fn=lambda p=plan: self._execute_plan(p),
                        device_id=device_id,
                        on_complete=lambda result, p=plan, did=device_id: self._on_plan_done(p, result, did),
                    )
                except Exception:
                    # Dispatch failed → push task back to queue front
                    q.appendleft(plan)
                    plan.status = "PENDING"
                    self._ready_devices.append(device_id)
                    logger.error("Dispatch failed for %s/%s, task re-queued",
                                 plan.device.device_name, plan.task.task_name)

    def _execute_plan(self, plan: TaskPlan) -> ExecutionResult:
        try:
            if plan.protocol == "BMC" and self._bm:
                exec_ = BMCExecutor(self._bm, connect_timeout=self._config.tcp_connect_timeout)
            elif plan.protocol == "SSH":
                exec_ = SSHExecutor(connect_timeout=self._config.tcp_connect_timeout)
            else:
                return ExecutionResult(
                    plan_id=plan.plan_id,
                    device_name=plan.device.device_name,
                    task_name=plan.task.task_name,
                    execution_status="EXEC_FAILED",
                    execution_failure_reason=f"Unsupported protocol: {plan.protocol}",
                    started_at=time.time(),
                    ended_at=time.time(),
                )

            return exec_.execute(plan, self._config.output_root)
        except Exception as e:
            logger.error("_execute_plan crashed for %s/%s: %s",
                         plan.device.device_name, plan.task.task_name, e)
            return ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                task_name=plan.task.task_name,
                execution_status="EXEC_ERROR",
                execution_failure_reason=f"_execute_plan exception: {e}",
                started_at=time.time(),
                ended_at=time.time(),
            )

    def _on_plan_done(self, plan: TaskPlan, result: ExecutionResult, device_id: str):
        """Called from worker thread when a task completes.
        Task was already popped from device queue in _dispatch (main thread).
        Just record result and re-add device if it has more pending tasks.
        """
        plan.completed_at = time.time()
        plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status

        with self._results_lock:
            self._results.append(result)

        # Re-add device to ready queue if it still has pending tasks
        q = self._device_queues.get(device_id)
        if q:
            self._ready_devices.append(device_id)

        if self._event_bus:
            self._event_bus.emit("plan_completed", plan=plan, result=result)

        status_icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
        reason = ""
        if status_icon == "FAIL" and result.execution_failure_reason:
            reason = f"  [{result.execution_failure_reason[:60]}]"
        print(f"[{len(self._results):>5}] {status_icon:>4} {result.device_name}  {result.task_name}{reason}")

    def _drain(self):
        """Wait for running tasks to finish — with hard timeout so process always exits."""
        logger.info("Draining running tasks...")
        drain_deadline = time.time() + 300  # 5 min absolute max
        while self._bmc_pool._active_futures or self._ssh_pool._active_futures:
            if time.time() > drain_deadline:
                logger.warning(
                    "Drain timeout after 5min — %d bmc futures + %d ssh futures still active",
                    len(self._bmc_pool._active_futures),
                    len(self._ssh_pool._active_futures),
                )
                break
            time.sleep(1)
        logger.info("Drain complete")
