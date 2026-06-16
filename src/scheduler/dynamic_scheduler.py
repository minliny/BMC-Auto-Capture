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
from ..executor.browser_manager import BrowserManager, check_playwright_runtime_dependency
from ..executor.retry import execute_with_retry
from ..models.verdict import compute_verdict
from ..out.console import start as cstart, done as cdone, heartbeat as cheartbeat, info as cinfo
from .resource_monitor import ResourceMonitor
from .worker_pool import DispatchNotCommittedError, WorkerPool
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
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
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
        self._result_index_by_plan_id: dict[str, int] = {}
        self._result_sources: dict[str, str] = {}

        # Control — P0-3: accept external events from App for unified stop/pause
        if stop_event is not None:
            self._stop_event = stop_event
        else:
            self._stop_event = threading.Event()
        if pause_event is not None:
            self._pause_event = pause_event
        else:
            self._pause_event = threading.Event()
            self._pause_event.set()

        # AUDIT-003: track WHY stop was triggered
        self._stop_reason: str = "scheduler_stop"  # or "route_change"
        self._stop_triggered_by: str = ""
        self._stopped_at: float = 0.0
        self._affected_pending_count: int = 0

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
        self._total_plan_count = 0
        self._plan_order_for_show: list[str] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, plans: list[TaskPlan]) -> list[ExecutionResult]:
        if not self._execution_id:
            self._execution_id = uuid.uuid4().hex[:12]
        self._build_endpoint_queues(plans)
        self._total_plan_count = len(plans)
        self._gate_bmc_runtime_dependency()
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
                    active_bmc = len(self._bmc_pool._active_futures)
                    active_ssh = len(self._ssh_pool._active_futures)
                    pending = sum(len(q) for q in self._endpoint_queues.values())

                    if active_bmc > 0 or active_ssh > 0:
                        # Resources are running but no progress — long-running tasks
                        logger.warning(
                            "Long running endpoint(s): %.0fs no progress, "
                            "active BMC=%d SSH=%d, ready=%d pending=%d",
                            now - last_progress_at, active_bmc, active_ssh,
                            len(self._ready_endpoints), pending,
                        )
                    elif pending > 0 and len(self._ready_endpoints) == 0:
                        # True stall: pending plans but no ready endpoints
                        logger.warning("调度器停滞: %.0f 秒无进展, ready=0, pending=%d",
                                       now - last_progress_at, pending)
                        pending_dumped = 0
                        for ekey, q in sorted(self._endpoint_queues.items()):
                            if q and pending_dumped < 10:
                                p = q[0]
                                logger.warning("  PENDING: endpoint=%s device=%s task=%s",
                                               ekey, p.device.device_name, p.task.task_name)
                                pending_dumped += 1
                    else:
                        logger.warning("调度器空闲: %.0f 秒无进展 (no active, no pending)",
                                       now - last_progress_at)

                # 4. Brief sleep
                time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("用户中断 — stopping scheduler")
            self._record_stop("user_interrupt", "KeyboardInterrupt")
        finally:
            if self._stop_event.is_set() and self._stopped_at <= 0:
                self._record_stop(self._stop_reason, "external_stop_event")
            # AUDIT-003: generate results for all remaining pending plans
            # (stop / route change / exception — plans that never got dispatched)
            pending_plans: list[TaskPlan] = []
            for q in self._endpoint_queues.values():
                while q:
                    pending_plans.append(q.popleft())

            if pending_plans:
                _now = time.time()
                self._affected_pending_count = len(pending_plans)
                stop_label = self._stop_reason_label()
                logger.warning(
                    "AUDIT-003: %d pending plans will be marked SKIPPED "
                    "(stopReason=%s triggeredBy=%s)",
                    len(pending_plans), stop_label, self._stop_triggered_by,
                )
                for plan in pending_plans:
                    plan.status = "EXEC_SKIPPED_ROUTE_CHANGED" if self._stop_reason == "route_change" else "EXEC_SKIPPED_STOPPED"
                    plan.ended_at = _now
                    plan.completed_at = _now
                    plan._resource_lease_held = False
                    r = ExecutionResult(
                        plan_id=plan.plan_id,
                        task_id=plan.task_id,
                        client_task_id=plan.client_task_id,
                        device_name=plan.device.device_name,
                        device_group=plan.device.device_group,
                        bmc_ip=plan.device.bmc_ip,
                        inband_ip=plan.device.inband_ip,
                        task_name=plan.task.task_name,
                        task_type=plan.task.task_type,
                        execution_mode=plan.task.execution_mode,
                        execution_status=plan.status,
                        execution_failure_reason=(
                            f"调度停止: {stop_label} "
                            f"triggered_by={self._stop_triggered_by or 'unknown'} "
                            f"affectedPendingCount={self._affected_pending_count}"
                        ),
                        started_at=_now,
                        ended_at=_now,
                        duration_seconds=0.001,
                        endpoint_key=plan.endpoint_key,
                        endpoint_type=plan.endpoint_type,
                    )
                    r.final_verdict = compute_verdict(r)
                    self._append_result_once(
                        plan.plan_id, r, source=f"stop:{self._stop_reason}",
                    )
                logger.info(
                    "AUDIT-003: %d pending plans → SKIPPED (total results now %d)",
                    len(pending_plans), len(self._results),
                )
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

    def stop(self, reason: str = "scheduler_stop", triggered_by: str = "DynamicScheduler.stop"):
        self._record_stop(reason, triggered_by)
        self._stop_event.set()
        self._pause_event.set()

    def _record_stop(self, reason: str, triggered_by: str) -> None:
        if not self._stop_event.is_set() or self._stopped_at <= 0:
            self._stop_reason = reason or self._stop_reason or "scheduler_stop"
            self._stop_triggered_by = triggered_by or "unknown"
            self._stopped_at = time.time()

    def _stop_reason_label(self) -> str:
        if self._stop_reason == "route_change":
            return "ROUTE_GUARD_STOPPED"
        if self._stop_reason == "user_interrupt":
            return "USER_INTERRUPT"
        if self._stop_reason in ("scheduler_stop", "user_stop"):
            return "USER_STOPPED"
        if self._stop_reason:
            return self._stop_reason.upper()
        return ""

    @property
    def stop_metadata(self) -> dict:
        return {
            "stopReason": self._stop_reason_label() if self._stopped_at > 0 else "",
            "stopTriggeredBy": self._stop_triggered_by if self._stopped_at > 0 else "",
            "stoppedAt": self._stopped_at,
            "affectedPendingCount": self._affected_pending_count,
        }

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

    def _gate_bmc_runtime_dependency(self) -> None:
        """Block BMC work early when Playwright is unavailable in this runtime."""
        has_bmc = any(
            plan.endpoint_type == "BMC"
            for queue in self._endpoint_queues.values()
            for plan in queue
        )
        if not has_bmc:
            return

        ok, reason = check_playwright_runtime_dependency()
        if ok:
            return

        logger.error("BMC runtime dependency preflight failed: %s", reason)
        now = time.time()
        blocked = 0
        for endpoint_key, queue in list(self._endpoint_queues.items()):
            if not queue or queue[0].endpoint_type != "BMC":
                continue
            while queue:
                plan = queue.popleft()
                plan.status = "EXEC_BLOCKED"
                plan.started_at = now
                plan.ended_at = now
                plan.completed_at = now
                result = ExecutionResult(
                    plan_id=plan.plan_id,
                    task_id=plan.task_id,
                    client_task_id=plan.client_task_id,
                    device_name=plan.device.device_name,
                    device_group=plan.device.device_group,
                    bmc_ip=plan.device.bmc_ip,
                    inband_ip=plan.device.inband_ip,
                    task_name=plan.task.task_name,
                    task_type=plan.task.task_type,
                    execution_mode=plan.task.execution_mode,
                    execution_status="EXEC_BLOCKED",
                    execution_failure_reason=reason,
                    started_at=now,
                    ended_at=now,
                    duration_seconds=0.001,
                    endpoint_key=plan.endpoint_key,
                    endpoint_type=plan.endpoint_type,
                )
                result.final_verdict = compute_verdict(result)
                self._append_result_once(
                    plan.plan_id,
                    result,
                    source="bmc_dependency_preflight",
                )
                if self._event_bus:
                    self._event_bus.emit("plan_completed", plan=plan, result=result)
                cdone(
                    len(self._results),
                    self._total_plan_count or blocked + 1,
                    "FAIL",
                    device_group=result.device_group,
                    device=result.device_name,
                    task=result.task_name,
                    reason="BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME",
                )
                blocked += 1

        self._ready_endpoints = deque(
            endpoint_key for endpoint_key in self._ready_endpoints
            if self._endpoint_queues.get(endpoint_key)
        )
        logger.error(
            "BMC runtime dependency preflight blocked %d BMC plans; "
            "non-BMC dispatch remains available",
            blocked,
        )

    def _adjust_pools(self, cpu: float, mem: float):
        scale = self._compute_scale(cpu, mem)
        bmc_demand = self._endpoint_demand("BMC", self._bmc_pool)
        ssh_demand = self._endpoint_demand("INBAND", self._ssh_pool)
        target_bmc = self._compute_target_size(
            self._config.base_bmc_workers, self._config.max_bmc_workers,
            bmc_demand, scale,
        )
        target_ssh = self._compute_target_size(
            self._config.base_ssh_workers, self._config.max_ssh_workers,
            ssh_demand, scale,
        )
        self._bmc_pool.resize(target_bmc)
        self._ssh_pool.resize(target_ssh)

    def _endpoint_demand(self, ep_type: str, pool) -> int:
        queued = 0
        for q in self._endpoint_queues.values():
            if not q:
                continue
            if q[0].endpoint_type == ep_type:
                queued += 1
        return len(pool._active_futures) + queued

    @staticmethod
    def _compute_target_size(base_workers: int, max_workers: int, demand: int, scale: float) -> int:
        base_workers = max(1, int(base_workers))
        max_workers = max(1, int(max_workers))
        demand = max(0, int(demand))
        if scale >= 1.0:
            return min(max_workers, max(base_workers, demand))

        resource_cap = max(1, int(max_workers * max(scale, 0.0)))
        return min(max_workers, resource_cap, max(1, demand))

    def _compute_scale(self, cpu: float, mem: float) -> float:
        cfg = self._config
        if mem > cfg.mem_emergency_pct or cpu > cfg.cpu_emergency_pct:
            return cfg.resource_scale_emergency
        if mem > cfg.mem_scale_down_pct or cpu > cfg.cpu_scale_down_pct:
            return cfg.resource_scale_down
        if mem < cfg.mem_scale_up_pct and cpu < cfg.cpu_scale_up_pct:
            return cfg.resource_scale_up
        return cfg.resource_scale_normal

    def _dispatch(self):
        for pool, ep_type in [(self._bmc_pool, "BMC"), (self._ssh_pool, "INBAND")]:
            skipped = 0
            ready_snapshot = len(self._ready_endpoints)
            while pool.has_idle() and self._ready_endpoints:
                # AUDIT-003: check stop before dispatching any new work
                if self._stop_event.is_set():
                    logger.debug("Dispatch: stop set — aborting dispatch loop")
                    return
                # AUDIT-003: respect pause — don't dispatch while paused
                if not self._pause_event.is_set():
                    logger.debug("Dispatch: paused — aborting dispatch loop")
                    return
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
                            artifact_profile=getattr(self._config, "bmc_artifact_profile", "full"),
                            on_plan_done=lambda plan, result: self._on_bmc_plan_in_group(plan, result),
                        )
                        return runner.run()

                    try:
                        pool.dispatch(
                            fn=_bmc_group_worker,
                            resource_key=endpoint_key,
                            on_complete=(
                                lambda results, ek=endpoint_key, plans=group_plans:
                                self._on_bmc_group_done(results, ek, plans)
                            ),
                        )
                    except DispatchNotCommittedError:
                        # No worker was submitted: release and requeue are safe.
                        try:
                            self._registry.release(endpoint_key)
                        except Exception:
                            pass
                        for gp in reversed(group_plans):
                            q.appendleft(gp)
                            gp.status = "PENDING"
                            gp._resource_lease_held = False
                        self._ready_endpoints.append(endpoint_key)
                        logger.error("BMC group dispatch failed for %s, plans re-queued", endpoint_key)
                        break
                    except Exception:
                        # Unknown post-submit state: never release/requeue and risk
                        # duplicate endpoint execution.
                        logger.critical(
                            "BMC dispatch state unknown after exception for %s; "
                            "lease retained, no requeue",
                            endpoint_key,
                            exc_info=True,
                        )

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
                cstart(plan.protocol, device_group=plan.device.device_group, device=plan.device.device_name, task=plan.task.task_name)

                if self._event_bus:
                    self._event_bus.emit("plan_started", plan=plan)

                try:
                    pool.dispatch(
                        fn=lambda p=plan: self._execute_plan(p),
                        resource_key=endpoint_key,
                        on_complete=lambda result, p=plan, ek=endpoint_key: self._on_plan_done(p, result, ek),
                    )
                except DispatchNotCommittedError:
                    # No worker was submitted: release and requeue are safe.
                    try:
                        self._registry.release(endpoint_key)
                    except Exception:
                        pass
                    q.appendleft(plan)
                    plan.status = "PENDING"
                    plan._resource_lease_held = False
                    self._ready_endpoints.append(endpoint_key)
                    logger.error("派发失败 for %s/%s, 任务已重新入队",
                                 plan.device.device_name, plan.task.task_name)
                    break
                except Exception:
                    logger.critical(
                        "Dispatch state unknown after exception for %s/%s; "
                        "lease retained, no requeue",
                        plan.device.device_name,
                        plan.task.task_name,
                        exc_info=True,
                    )

    def _execute_plan(self, plan: TaskPlan) -> ExecutionResult:
        try:
            if plan.protocol == "BMC" and self._bm:
                exec_ = BMCExecutor(self._bm, connect_timeout=self._config.tcp_connect_timeout,
                             page_timeout=self._config.bmc_page_timeout,
                             popup_timeout=self._config.popup_dismiss_selector_timeout,
                             artifact_profile=getattr(self._config, "bmc_artifact_profile", "full"))
            elif plan.protocol == "SSH":
                exec_ = SSHExecutor(
                    connect_timeout=self._config.tcp_connect_timeout,
                    command_timeout=self._config.ssh_command_timeout,
                    idle_timeout=self._config.ssh_idle_timeout,
                )
            else:
                r = ExecutionResult(
                    plan_id=plan.plan_id,
                    device_name=plan.device.device_name,
                    task_name=plan.task.task_name,
                    execution_status="EXEC_FAILED",
                    execution_failure_reason=f"Unsupported protocol: {plan.protocol}",
                    started_at=time.time(),
                    ended_at=time.time(),
                )
                r.final_verdict = compute_verdict(r)
                return r

            # Mark executor start timing
            plan.executor_started_at = time.time()
            result = execute_with_retry(exec_, plan, self._config.output_root)
            plan.executor_finished_at = time.time()

            # Copy timing + endpoint data from plan into result
            result.endpoint_key = plan.endpoint_key
            result.endpoint_type = plan.endpoint_type
            result.resource_wait_seconds = plan.resource_wait_seconds
            result.executor_duration_seconds = plan.executor_duration_seconds
            result.retry_count = plan.retry_attempt

            return result
        except Exception as e:
            plan.executor_finished_at = time.time()
            logger.error("_execute_plan crashed for %s/%s: %s",
                         plan.device.device_name, plan.task.task_name, e)
            r = ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                task_name=plan.task.task_name,
                execution_status="EXEC_ERROR",
                execution_failure_reason=f"_execute_plan exception: {e}",
                started_at=time.time(),
                ended_at=time.time(),
            )
            r.final_verdict = compute_verdict(r)
            return r

    def _on_plan_done(self, plan: TaskPlan, result: ExecutionResult, endpoint_key: str):
        """Called from worker thread when a task completes."""
        now_t = time.time()
        self._fill_result_identity_from_plan(plan, result)
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

        # AUDIT-008: ensure final_verdict is set
        result.final_verdict = compute_verdict(result)

        accepted = self._append_result_once(
            plan.plan_id, result, source="worker_callback",
        )
        if not accepted:
            return

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
        cdone(
            len(self._results),
            self._total_plan_count or self._dispatched_count,
            status_icon,
            device_group=result.device_group,
            device=result.device_name,
            task=result.task_name,
            reason=reason,
        )

    def _on_bmc_plan_in_group(self, plan: TaskPlan, result: ExecutionResult):
        """Per-plan callback during BMC session group — no release, no re-queue."""
        self._fill_result_identity_from_plan(plan, result)
        plan.ended_at = time.time()
        plan.completed_at = plan.ended_at
        plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status
        plan._resource_lease_held = False
        plan._execution_id = ""

        # AUDIT-008: ensure final_verdict is set
        result.final_verdict = compute_verdict(result)

        accepted = self._append_result_once(
            plan.plan_id, result, source="bmc_plan_callback",
        )
        if not accepted:
            return

        if self._event_bus:
            self._event_bus.emit("plan_completed", plan=plan, result=result)

        _icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
        _reason = ""
        if _icon == "FAIL" and result.execution_failure_reason:
            _reason = f"  [{result.execution_failure_reason[:60]}]"
        cdone(
            len(self._results),
            self._total_plan_count or self._dispatched_count,
            _icon,
            device_group=result.device_group,
            device=result.device_name,
            task=result.task_name,
            reason=_reason,
        )

    def _on_bmc_group_done(
        self,
        results: list[ExecutionResult] | ExecutionResult | None,
        endpoint_key: str,
        plans: list[TaskPlan] | None = None,
    ):
        """Called when a BMC session group completes. Release registry + log group summary.

        Per-plan output is already handled by _on_bmc_plan_in_group.
        This callback only releases the global registry hold and logs a group-level summary.
        """
        # Release global ResourceRegistry hold
        try:
            self._registry.release(endpoint_key)
        except Exception:
            pass

        if isinstance(results, ExecutionResult) or results is None:
            self._recover_bmc_group_worker_failure(results, endpoint_key, plans or [])
            return

        # Log group-level summary (per-plan output already handled by _on_bmc_plan_in_group)
        if results:
            passed = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
            failed = len(results) - passed
            logger.info(
                "BMC group done: endpoint=%s plans=%d passed=%d failed=%d",
                endpoint_key, len(results), passed, failed,
            )

    def _recover_bmc_group_worker_failure(
        self,
        worker_result: ExecutionResult | None,
        endpoint_key: str,
        plans: list[TaskPlan],
    ) -> None:
        """Convert a crashed BMC group worker into one result per unfinished plan."""
        reason = "BMC group worker failed"
        if worker_result and worker_result.execution_failure_reason:
            reason = worker_result.execution_failure_reason
        now_t = time.time()
        recovered = 0
        for plan in plans:
            with self._results_lock:
                already_recorded = plan.plan_id in self._result_index_by_plan_id
            if already_recorded:
                continue
            plan.status = "EXEC_ERROR"
            plan.ended_at = now_t
            plan.completed_at = now_t
            plan._resource_lease_held = False
            plan._execution_id = ""
            result = self._make_plan_error_result(
                plan,
                execution_status="EXEC_ERROR",
                reason=reason,
                now_t=now_t,
                endpoint_key=endpoint_key,
                endpoint_type="BMC",
            )
            accepted = self._append_result_once(
                plan.plan_id,
                result,
                source="bmc_group_worker_exception",
            )
            if accepted:
                recovered += 1
                if self._event_bus:
                    self._event_bus.emit("plan_completed", plan=plan, result=result)
        logger.error(
            "BMC group worker failed; recovered_results=%d plans=%d",
            recovered,
            len(plans),
        )

    @staticmethod
    def _fill_result_identity_from_plan(plan: TaskPlan, result: ExecutionResult) -> None:
        synthetic_worker_result = (
            result.task_name == "(crashed)"
            or result.device_name == plan.endpoint_key
        )
        if not result.plan_id:
            result.plan_id = plan.plan_id
        if not result.task_id:
            result.task_id = plan.task_id
        if not result.client_task_id:
            result.client_task_id = plan.client_task_id
        if synthetic_worker_result or not result.device_name:
            result.device_name = plan.device.device_name
        if not result.device_group:
            result.device_group = plan.device.device_group
        if not result.bmc_ip:
            result.bmc_ip = plan.device.bmc_ip
        if not result.inband_ip:
            result.inband_ip = plan.device.inband_ip
        if synthetic_worker_result or not result.task_name:
            result.task_name = plan.task.task_name
        if not result.task_type:
            result.task_type = plan.task.task_type
        if not result.execution_mode:
            result.execution_mode = plan.task.execution_mode
        if not result.task_sequence:
            result.task_sequence = plan.task.sequence_str or str(plan.task.sequence)
        if not result.endpoint_key:
            result.endpoint_key = plan.endpoint_key
        if not result.endpoint_type:
            result.endpoint_type = plan.endpoint_type

    def _make_plan_error_result(
        self,
        plan: TaskPlan,
        *,
        execution_status: str,
        reason: str,
        now_t: float,
        endpoint_key: str,
        endpoint_type: str,
    ) -> ExecutionResult:
        started_at = plan.executor_started_at or plan.started_at or now_t
        result = ExecutionResult(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            client_task_id=plan.client_task_id,
            device_name=plan.device.device_name,
            device_group=plan.device.device_group,
            bmc_ip=plan.device.bmc_ip,
            inband_ip=plan.device.inband_ip,
            task_name=plan.task.task_name,
            task_type=plan.task.task_type,
            execution_mode=plan.task.execution_mode,
            task_sequence=plan.task.sequence_str or str(plan.task.sequence),
            execution_status=execution_status,
            execution_failure_reason=reason,
            started_at=started_at,
            ended_at=now_t,
            duration_seconds=max(0.001, round(now_t - started_at, 3)),
            endpoint_key=endpoint_key,
            endpoint_type=endpoint_type,
        )
        result.final_verdict = compute_verdict(result)
        return result

    @staticmethod
    def _result_priority(status: str) -> int:
        if status.startswith("EXEC_SKIPPED"):
            return 10
        if status in {
            "EXEC_SUCCESS", "EXEC_FAILED", "EXEC_ERROR",
            "EXEC_TIMEOUT", "EXEC_PARTIAL",
        }:
            return 100
        return 50

    def _append_result_once(
        self, plan_id: str, result: ExecutionResult, source: str,
    ) -> bool:
        """Append or upgrade one final result per plan_id.

        Executed terminal results outrank scheduler-generated skipped results.
        Results at the same priority are first-wins.
        """
        stable_plan_id = plan_id or result.plan_id
        if not stable_plan_id:
            logger.error(
                "Result rejected without plan_id: status=%s source=%s",
                result.execution_status,
                source,
            )
            return False
        result.plan_id = stable_plan_id

        with self._results_lock:
            old_index = self._result_index_by_plan_id.get(stable_plan_id)
            if old_index is None:
                self._result_index_by_plan_id[stable_plan_id] = len(self._results)
                self._result_sources[stable_plan_id] = source
                self._results.append(result)
                return True

            old_result = self._results[old_index]
            old_source = self._result_sources.get(stable_plan_id, "unknown")
            old_priority = self._result_priority(old_result.execution_status)
            new_priority = self._result_priority(result.execution_status)
            replace = new_priority > old_priority

            logger.warning(
                "Duplicate final result plan_id=%s old_status=%s new_status=%s "
                "old_source=%s new_source=%s action=%s",
                stable_plan_id,
                old_result.execution_status,
                result.execution_status,
                old_source,
                source,
                "replace" if replace else "discard",
            )
            if replace:
                self._results[old_index] = result
                self._result_sources[stable_plan_id] = source
                return True
            return False

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
