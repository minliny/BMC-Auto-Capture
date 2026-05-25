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

        try:
            while not self._stop_event.is_set() and self._has_remaining_work():
                self._pause_event.wait()

                # 1. Adjust pools
                cpu, mem = self._monitor.latest
                self._adjust_pools(cpu, mem)

                # 2. Dispatch
                self._dispatch()

                # 3. Brief sleep
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

                plan = self._device_queues[device_id][0]  # peek

                if plan.protocol != protocol:
                    self._ready_devices.append(device_id)
                    continue

                plan.status = "RUNNING"
                plan.started_at = time.time()

                if self._event_bus:
                    self._event_bus.emit("plan_started", plan=plan)

                pool.dispatch(
                    fn=lambda p=plan: self._execute_plan(p),
                    device_id=device_id,
                    on_complete=lambda result, p=plan, did=device_id: self._on_plan_done(p, result, did),
                )

    def _execute_plan(self, plan: TaskPlan) -> ExecutionResult:
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

    def _on_plan_done(self, plan: TaskPlan, result: ExecutionResult, device_id: str):
        plan.completed_at = time.time()
        plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status

        with self._results_lock:
            self._results.append(result)

        # Pop completed task from device queue
        q = self._device_queues.get(device_id)
        if q:
            q.popleft()
            if q:
                self._ready_devices.append(device_id)

        if self._event_bus:
            self._event_bus.emit("plan_completed", plan=plan, result=result)

        status_icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
        print(f"[{len(self._results):>5}] {status_icon:>4} {result.device_name[:20]:<20} {result.task_name[:30]}")

    def _drain(self):
        """Wait for running tasks to finish."""
        logger.info("Draining running tasks...")
        while self._bmc_pool._active_futures or self._ssh_pool._active_futures:
            time.sleep(1)
        logger.info("Drain complete")
