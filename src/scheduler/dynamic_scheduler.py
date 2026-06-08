from __future__ import annotations

"""
Dynamic Scheduler — the core scheduling loop.

Features:
- Resource-serialized execution by endpoint_key (max 1 task per endpoint at a time)
- Protocol-split pools (BMC vs INBAND)
- Dynamic pool resizing based on CPU/memory
- Graceful pause/resume/stop
- Global ResourceRegistry for cross-execution serialization
- Timing instrumentation
"""

import logging
import threading
import time
import uuid
from collections import deque, defaultdict
from typing import Callable

from ..models.app_config import AppConfig
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult
from ..executor.ssh_executor import SSHExecutor
from ..executor.bmc_executor import BMCExecutor
from ..executor.browser_manager import BrowserManager
from ..out.console import start as cstart, done as cdone, heartbeat as cheartbeat, info as cinfo
from .resource_monitor import ResourceMonitor
from .worker_pool import WorkerPool
from .resource_registry import ResourceRegistry

logger = logging.getLogger("bmc_auto_capture.scheduler")


class DynamicScheduler:
    """Unified scheduler with dynamic concurrency and endpoint-key serialization.

    Scheduling model:
      - Plans are grouped by endpoint_key (e.g. BMC:10.0.0.1:443).
      - Within the same endpoint_key, plans run serially in FIFO order.
      - Different endpoint_keys can run concurrently (up to per-pool worker limits).
      - BMC pool uses BMC endpoint_keys; INBAND pool uses INBAND endpoint_keys.
      - A process-wide Global ResourceRegistry prevents concurrent access to the
        same endpoint_key across different scheduler instances (e.g. API multi-execution).
      - device_name is used ONLY for display/logging — never as a scheduling lock key.
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus=None,
    ):
        self._config = config
        self._event_bus = event_bus

        # Endpoint-key queues: one FIFO per endpoint_key
        self._endpoint_queues: dict[str, deque[TaskPlan]] = {}
        self._ready_endpoints: deque[str] = deque()
        self._endpoint_plan_order: dict[str, list[TaskPlan]] = defaultdict(list)

        # Results
        self._results: list[ExecutionResult] = []
        self._results_lock = threading.Lock()

        # Control
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # Pools — max_bmc_workers = max concurrent BMC endpoint groups,
        # max_ssh_workers = max concurrent INBAND endpoint groups.
        self._bmc_pool = WorkerPool("bmc", config.base_bmc_workers, config.max_bmc_workers)
        self._ssh_pool = WorkerPool("ssh", config.base_ssh_workers, config.max_ssh_workers)

        # Resource monitor
        self._monitor = ResourceMonitor(interval=config.resource_check_interval)

        # Global ResourceRegistry (process-wide singleton)
        self._registry = ResourceRegistry()

        # Execution context for reentrant registry acquire
        self._execution_id: str = ""

        # Executors (created per worker — lazy init via factory)
        self._bm: BrowserManager | None = None

        # Stats for logging
        self._dispatched_count = 0
        self._plan_order_for_show: list[str] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, plans: list[TaskPlan]) -> list[ExecutionResult]:
        if not self._execution_id:
            self._execution_id = uuid.uuid4().hex[:12]
        self._build_endpoint_queues(plans)
        self._log_startup_stats(plans)
        self._monitor.start()
        self._bmc_pool.start()
        self._ssh_pool.start()

        # Browser manager — shared across BMC workers
        self._bm = BrowserManager(
            headless=self._config.browser_headless,
            max_tasks=self._config.browser_max_tasks_before_recycle,
            max_age_seconds=self._config.browser_max_age_seconds,
        )

        last_progress_at = time.time()
        last_heartbeat_at = time.time()
        completed_count = 0
        self._dispatched_count = 0

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

                if now - last_heartbeat_at >= 30:
                    last_heartbeat_at = now
                    pending = sum(len(q) for q in self._endpoint_queues.values())
                    running_bmc = len(self._bmc_pool._active_futures)
                    running_ssh = len(self._ssh_pool._active_futures)
                    ready = len(self._ready_endpoints)
                    locked_bmc = len(self._bmc_pool._running_resources)
                    locked_ssh = len(self._ssh_pool._running_resources)
                    logger.info(
                        "心跳: 已派发=%d 已完成=%d 待处理=%d 运行中(BMC=%d SSH=%d) 就绪端点=%d",
                        self._dispatched_count, completed_count, pending, running_bmc, running_ssh, ready,
                    )
                    cheartbeat(self._dispatched_count, completed_count, pending,
                               running_bmc, running_ssh, ready)

                # Stall detection: no progress for 60s
                if now - last_progress_at >= 60:
                    logger.warning("调度器停滞: %.0f 秒无进展", now - last_progress_at)
                    pending_dumped = 0
                    for ekey, q in sorted(self._endpoint_queues.items()):
                        if q and pending_dumped < 10:
                            p = q[0]
                            blocked_by = ""
                            if self._bmc_pool.resource_has_running_task(ekey):
                                blocked_by = " (endpoint busy in BMC pool)"
                            elif self._ssh_pool.resource_has_running_task(ekey):
                                blocked_by = " (endpoint busy in SSH pool)"
                            logger.warning(
                                "  PENDING: endpoint=%s device=%s task=%s protocol=%s%s",
                                ekey, p.device.device_name, p.task.task_name, p.protocol, blocked_by,
                            )
                            pending_dumped += 1
                    logger.warning("  BMC active resources: %s", list(self._bmc_pool._running_resources))
                    logger.warning("  SSH active resources: %s", list(self._ssh_pool._running_resources))
                    logger.warning("  Ready endpoints: %d, total endpoint queues: %d",
                                   len(self._ready_endpoints), len(self._endpoint_queues))

                # 4. Brief sleep
                time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("用户中断 — stopping scheduler")
        finally:
            drained = self._drain()

            # Close browsers on worker threads BEFORE pool shutdown
            if self._bm and self._bmc_pool._executor is not None:
                browser_count = len(self._bm._tls)
                if browser_count > 0:
                    logger.info("正在清理 %d 个浏览器实例 on worker threads...", browser_count)
                    cleanup_futures = []
                    for _ in range(browser_count):
                        f = self._bmc_pool._executor.submit(
                            self._bm.close_current_thread_browser
                        )
                        cleanup_futures.append(f)
                    for f in cleanup_futures:
                        try:
                            f.result(timeout=20)
                        except Exception:
                            pass

            self._bmc_pool.shutdown(wait=drained)
            self._ssh_pool.shutdown(wait=drained)
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
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()
        logger.info("调度器已暂停")

    def resume(self):
        self._pause_event.set()
        logger.info("调度器已恢复")

    @property
    def results(self) -> list[ExecutionResult]:
        with self._results_lock:
            return list(self._results)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _build_endpoint_queues(self, plans: list[TaskPlan]):
        """Group plans by endpoint_key instead of device_name."""
        # Log warnings for missing IPs
        for plan in plans:
            if plan.endpoint_type == "BMC" and not plan.device.bmc_ip:
                logger.warning(
                    "设备 %s BMC IP 为空 — endpoint_key=%s",
                    plan.device.device_name, plan.endpoint_key,
                )
            elif plan.endpoint_type == "INBAND" and not plan.device.inband_ip:
                logger.warning(
                    "设备 %s INBAND IP 为空 — endpoint_key=%s",
                    plan.device.device_name, plan.endpoint_key,
                )

        for plan in plans:
            ekey = plan.endpoint_key
            if ekey not in self._endpoint_queues:
                self._endpoint_queues[ekey] = deque()
            self._endpoint_queues[ekey].append(plan)
            self._endpoint_plan_order[ekey].append(plan)

        # Populate ready list (preserve original plan order within each endpoint)
        seen = set()
        # We iterate plans in original order to maintain fairness
        for plan in plans:
            ekey = plan.endpoint_key
            if ekey not in seen and ekey in self._endpoint_queues:
                seen.add(ekey)
                self._ready_endpoints.append(ekey)

    def _has_remaining_work(self) -> bool:
        for q in self._endpoint_queues.values():
            if q:
                return True
        if self._bmc_pool._active_futures or self._ssh_pool._active_futures:
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
        for pool, ep_type in [(self._bmc_pool, "BMC"), (self._ssh_pool, "INBAND")]:
            skipped = 0
            ready_snapshot = len(self._ready_endpoints)
            while pool.has_idle() and self._ready_endpoints:
                if skipped >= ready_snapshot:
                    break

                endpoint_key = self._ready_endpoints.popleft()

                # Check if this endpoint_key is already running in this pool
                if pool.resource_has_running_task(endpoint_key):
                    self._ready_endpoints.append(endpoint_key)
                    skipped += 1
                    continue

                q = self._endpoint_queues.get(endpoint_key)
                if not q:
                    continue  # Queue drained

                plan = q[0]  # Peek — don't pop until successfully dispatched

                if plan.endpoint_type != ep_type:
                    # Wrong pool — push back
                    self._ready_endpoints.append(endpoint_key)
                    skipped += 1
                    continue

                # Atomically try to hold via Global ResourceRegistry
                holder_meta = {
                    "execution_id": self._execution_id,
                    "plan_id": plan.plan_id,
                    "device_name": plan.device.device_name,
                    "task_name": plan.task.task_name,
                    "endpoint_key": endpoint_key,
                }
                if not self._registry.try_hold(endpoint_key, holder_meta):
                    logger.debug(
                        "[Dispatch] %s held by global registry — waiting",
                        endpoint_key,
                    )
                    self._ready_endpoints.append(endpoint_key)
                    skipped += 1
                    continue

                # BMC: dispatch all plans for this endpoint as a group (session reuse)
                if ep_type == "BMC":
                    # Pop ALL plans for this endpoint_key
                    group_plans: list[TaskPlan] = []
                    while q:
                        group_plans.append(q.popleft())
                    self._dispatched_count += len(group_plans)

                    # Mark all plans
                    now_t = time.time()
                    for gp in group_plans:
                        gp._resource_lease_held = True
                        gp._execution_id = self._execution_id
                        gp.status = "RUNNING"
                        gp.ready_at = now_t
                        gp.resource_wait_started_at = now_t
                        gp.resource_acquired_at = now_t
                        gp.executor_started_at = now_t

                    logger.info(
                        "开始 [BMC group] %s — %d plans (endpoint=%s)",
                        group_plans[0].device.device_name, len(group_plans), endpoint_key,
                    )

                    def _bmc_group_worker(plans=group_plans, ek=endpoint_key):
                        from .bmc_session_runner import BMCEndpointSessionRunner
                        runner = BMCEndpointSessionRunner(
                            browser_manager=self._bm,
                            endpoint_key=ek,
                            plans=plans,
                            output_root=self._config.output_root,
                            connect_timeout=self._config.tcp_connect_timeout,
                            page_timeout=self._config.bmc_page_timeout,
                            on_plan_done=lambda plan, result: self._on_bmc_plan_in_group(plan, result),
                        )
                        return runner.run()

                    try:
                        pool.dispatch(
                            fn=_bmc_group_worker,
                            resource_key=endpoint_key,
                            on_complete=lambda results, ek=endpoint_key: self._on_bmc_group_done(results, ek),
                        )
                    except Exception:
                        # Dispatch failed — push all plans back
                        for gp in reversed(group_plans):
                            q.appendleft(gp)
                            gp.status = "PENDING"
                        self._ready_endpoints.append(endpoint_key)
                        logger.error("BMC group dispatch failed for %s, plans re-queued", endpoint_key)

                    skipped = 0
                    continue

                # INBAND: dispatch single plan (no session reuse needed)
                q.popleft()

                # Mark lease held so executor skips double-acquire
                plan._resource_lease_held = True
                plan._execution_id = self._execution_id

                # Match! Dispatch
                skipped = 0
                plan.status = "RUNNING"
                now_t = time.time()
                plan.ready_at = now_t
                plan.resource_wait_started_at = now_t
                plan.resource_acquired_at = now_t
                plan.executor_started_at = now_t
                self._dispatched_count += 1

                logger.info(
                    "开始 [%s] %s / %s (endpoint=%s)",
                    plan.protocol, plan.device.device_name, plan.task.task_name, endpoint_key,
                )
                cstart(plan.protocol, plan.device.device_name, plan.task.task_name)

                if self._event_bus:
                    self._event_bus.emit("plan_started", plan=plan)

                try:
                    pool.dispatch(
                        fn=lambda p=plan: self._execute_plan(p),
                        resource_key=endpoint_key,
                        on_complete=lambda result, p=plan, ek=endpoint_key: self._on_plan_done(p, result, ek),
                    )
                except Exception:
                    # Dispatch failed — push task back to queue front
                    q.appendleft(plan)
                    plan.status = "PENDING"
                    self._ready_endpoints.append(endpoint_key)
                    logger.error("派发失败 for %s/%s, 任务已重新入队",
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

            # Mark executor start timing
            plan.executor_started_at = time.time()
            result = exec_.execute(plan, self._config.output_root)
            plan.executor_finished_at = time.time()

            # Copy timing + endpoint data from plan into result
            result.endpoint_key = plan.endpoint_key
            result.endpoint_type = plan.endpoint_type
            result.resource_wait_seconds = plan.resource_wait_seconds
            result.executor_duration_seconds = plan.executor_duration_seconds
            result.retry_count = plan.retry_attempt

            return result
        except BaseException as e:
            plan.executor_finished_at = time.time()
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

    def _on_plan_done(self, plan: TaskPlan, result: ExecutionResult, endpoint_key: str):
        """Called from worker thread when a task completes."""
        now_t = time.time()
        plan.ended_at = now_t
        plan.completed_at = now_t
        plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status

        # Release global ResourceRegistry hold (if any)
        try:
            self._registry.release(endpoint_key)
        except Exception:
            pass

        # Clear lease flag — executor fallback protection no longer needed
        plan._resource_lease_held = False
        plan._execution_id = ""

        with self._results_lock:
            self._results.append(result)

        # Re-add endpoint to ready queue if it still has pending tasks
        q = self._endpoint_queues.get(endpoint_key)
        if q:
            self._ready_endpoints.append(endpoint_key)

        if self._event_bus:
            self._event_bus.emit("plan_completed", plan=plan, result=result)

        status_icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
        reason = ""
        if status_icon == "FAIL" and result.execution_failure_reason:
            reason = f"  [{result.execution_failure_reason[:60]}]"
        cdone(len(self._results), self._dispatched_count, status_icon, result.device_name, result.task_name, reason)

    def _on_bmc_plan_in_group(self, plan: TaskPlan, result: ExecutionResult):
        """Per-plan callback during BMC session group — no release, no re-queue."""
        plan.ended_at = time.time()
        plan.completed_at = plan.ended_at
        plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status
        plan._resource_lease_held = False
        plan._execution_id = ""

        with self._results_lock:
            self._results.append(result)

        if self._event_bus:
            self._event_bus.emit("plan_completed", plan=plan, result=result)

        _icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
        _reason = ""
        if _icon == "FAIL" and result.execution_failure_reason:
            _reason = f"  [{result.execution_failure_reason[:60]}]"
        cdone(len(self._results), self._dispatched_count, _icon,
              result.device_name, result.task_name, _reason)

    def _on_bmc_group_done(self, results: list[ExecutionResult], endpoint_key: str):
        """Called when a BMC session group completes. Release registry + re-queue if needed."""
        # Release global ResourceRegistry hold
        try:
            self._registry.release(endpoint_key)
        except Exception:
            pass

        # Re-add endpoint to ready queue ONLY if it was re-created during session
        # (Session runner pops ALL plans from queue, so no more plans for this endpoint)
        # No re-queue needed — the endpoint queue is empty by design for BMC groups
        if status_icon == "FAIL" and result.execution_failure_reason:
            reason = f"  [{result.execution_failure_reason[:60]}]"
        cdone(len(self._results), self._dispatched_count, status_icon, result.device_name, result.task_name, reason)

    def _drain(self) -> bool:
        """Wait for running tasks to finish."""
        logger.info("等待运行中的任务完成...")
        drain_deadline = time.time() + 300  # 5 min
        last_log = time.time()
        while self._bmc_pool._active_futures or self._ssh_pool._active_futures:
            now = time.time()
            if now > drain_deadline:
                logger.warning(
                    "等待超时(5分钟) — %d bmc futures + %d ssh futures still active",
                    len(self._bmc_pool._active_futures),
                    len(self._ssh_pool._active_futures),
                )
                return False
            if now - last_log >= 30:
                pending = sum(len(q) for q in self._endpoint_queues.values())
                logger.info(
                    "DRAIN: waiting on %d bmc + %d ssh futures (pending=%d completed=%d)",
                    len(self._bmc_pool._active_futures),
                    len(self._ssh_pool._active_futures),
                    pending, len(self._results),
                )
                cinfo(f"等待收尾: BMC运行={len(self._bmc_pool._active_futures)} "
                      f"SSH运行={len(self._ssh_pool._active_futures)} "
                      f"待处理={pending} 已完成={len(self._results)}")
                last_log = now
            time.sleep(1)
        logger.info("等待完成")
        return True

    def _log_startup_stats(self, plans: list[TaskPlan]):
        """Log scheduling startup statistics."""
        endpoint_keys = set()
        bmc_endpoints = set()
        inband_endpoints = set()
        for p in plans:
            ek = p.endpoint_key
            endpoint_keys.add(ek)
            if p.endpoint_type == "BMC":
                bmc_endpoints.add(ek)
            else:
                inband_endpoints.add(ek)

        logger.info(
            "调度启动: total_plans=%d total_endpoint_groups=%d "
            "bmc_endpoint_groups=%d inband_endpoint_groups=%d "
            "max_bmc_endpoint_workers=%d max_inband_endpoint_workers=%d",
            len(plans),
            len(endpoint_keys),
            len(bmc_endpoints),
            len(inband_endpoints),
            self._config.max_bmc_workers,
            self._config.max_ssh_workers,
        )
