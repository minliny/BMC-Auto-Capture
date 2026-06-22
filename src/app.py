"""
App — composition root. Wires all components, provides unified execution interface.

All three interfaces (API, TUI, GUI) call App.run().
No interface reimplements execution logic.
"""

from __future__ import annotations
import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .checks import CheckStage, check_result_from_execution_status
from .models.app_config import AppConfig
from .models.task_plan import TaskPlan, make_plan_item_id
from .models.execution_result import ExecutionResult
from .models.verdict import compute_verdict
from .executor.retry import execute_with_retry
from .loader.excel_reader import load_all
from .loader.schema_validator import validate, ValidationReport
from .scheduler.plan_generator import generate_plans
from .connectivity.preflight import check_all as preflight_check_all, apply_preflight
from .connectivity.route_guard import RouteGuard
from .executor.ssh_executor import SSHExecutor
from .executor.bmc_executor import BMCExecutor
from .executor.browser_manager import BrowserManager
from .out.result_writer import ResultWriter
from .run_session import RunSession

logger = logging.getLogger("bmc_auto_capture.app")


class EventBus:
    """Lightweight pub/sub for decoupling scheduler from UI layers."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}

    def subscribe(self, event: str, callback: Callable):
        self._subscribers.setdefault(event, []).append(callback)

    def emit(self, event: str, **kwargs):
        for cb in self._subscribers.get(event, []):
            try:
                cb(**kwargs)
            except Exception:
                pass


class App:
    """Main application — creates all components and runs the pipeline."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.event_bus = EventBus()
        self._results: list[ExecutionResult] = []
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        # P0-3: save reference to current scheduler for stop/pause/resume forwarding
        self._active_scheduler: object | None = None
        self._run_lock = threading.Lock()
        self._stop_reason = "scheduler_stop"
        self._stop_triggered_by = ""
        self._stopped_at = 0.0
        self._affected_pending_count = 0
        self._result_writer = ResultWriter()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, excel_path: str | Path, mode: str = "sequential") -> list[ExecutionResult]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("RUN_ALREADY_ACTIVE")
        try:
            return self._run(excel_path, mode)
        finally:
            self._active_scheduler = None
            self._run_lock.release()

    def _run(self, excel_path: str | Path, mode: str = "sequential") -> list[ExecutionResult]:
        """Full pipeline: load → validate → plan → preflight → routeguard → execute → collect."""
        # P1-4: clear state before each run to support App instance reuse
        self._results = []
        self._stop_event.clear()
        self._pause_event.set()
        self._stop_reason = "scheduler_stop"
        self._clear_stop_metadata()

        excel_path = Path(excel_path)

        # 0. Set up timestamped output root to avoid cross-run overwrites
        session = RunSession.start(self.config.output_root)
        self.config.output_root = session.output_root
        batch_plan_id = session.plan_id
        logger.info("输出目录:  %s", self.config.output_root)
        logger.info("本次执行批次 plan_id: %s", batch_plan_id)

        # 1. Load
        logger.info("正在加载Excel:  %s", excel_path)
        devices, tasks = load_all(str(excel_path))
        logger.info("已加载 %d 台设备, %d 个任务", len(devices), len(tasks))

        # 2. Validate
        report = validate(devices, tasks)
        self._print_validation(report)
        if not report.is_valid:
            logger.error("校验失败, 错误数:  %d errors", len(report.errors))
            return []

        # 3. Generate plans
        plans = generate_plans(devices, tasks, plan_id=batch_plan_id)
        if not plans:
            logger.warning("未生成执行计划 — check device/task matching")
            return []

        self.event_bus.emit("plans_generated", count=len(plans))

        # 4. Connectivity preflight
        if self.config.preflight_enabled:
            logger.info("正在执行网络连通性预检...")
            pr = preflight_check_all(devices, timeout=self.config.tcp_connect_timeout,
                                     max_workers=self.config.max_bmc_workers + self.config.max_ssh_workers)
            plans = apply_preflight(plans, pr)
            # Count & record ALL skipped plans (both PRECHECK_FAILED and PORT_BLOCKED)
            for p in plans:
                if p.status.startswith("EXEC_SKIPPED"):
                    reason = p.skip_reason or "网络预检不通"
                    logger.info("Skip: %s / %s → %s", p.device.device_name, p.task.task_name, reason)
                    result = ExecutionResult(
                        plan_id=p.plan_id,
                        task_id=p.task_id,
                        plan_item_id=p.effective_plan_item_id,
                        client_task_id=p.client_task_id,
                        device_name=p.device.device_name,
                        device_group=p.device.device_group,
                        bmc_ip=p.device.bmc_ip,
                        inband_ip=p.device.inband_ip,
                        task_name=p.task.task_name,
                        task_type=p.task.task_type,
                        execution_mode=p.task.execution_mode,
                        execution_status=p.status,
                        execution_failure_reason=reason,
                        started_at=time.time(),
                        ended_at=time.time(),
                    )
                    result.add_check_result(check_result_from_execution_status(
                        result.execution_status,
                        result.execution_failure_reason,
                        stage=CheckStage.PRECHECK,
                        check_id="connectivity.preflight",
                        source="connectivity.preflight",
                    ))
                    self._compute_verdict(result)
                    self._results.append(result)
            skipped_count = sum(1 for p in plans if p.status.startswith("EXEC_SKIPPED"))
            logger.info("预检: %d 个计划已跳过 (%d 个就绪)", skipped_count, len(plans) - skipped_count)

        # 5. Route guard
        route_guard = None
        if self.config.route_guard_enabled:
            route_guard = RouteGuard(check_interval=self.config.route_guard_check_interval)
            route_guard.on_change = self._on_route_change
            route_guard.start()

        # 6. Execute
        ready_plans = [p for p in plans if not p.status.startswith("EXEC_SKIPPED")]
        logger.info("正在执行 %d 个计划 (%d 个被预检跳过)", len(ready_plans), len(plans) - len(ready_plans))

        if mode == "full":
            self._execute_dynamic(ready_plans)
        else:
            self._execute_sequential(ready_plans)

        # 7. Route guard cleanup
        if route_guard:
            route_guard.stop()

        # 8. Collect — with fallback if output dir is not writable
        output_dir = self._ensure_writable_output_dir()
        self._result_writer.write(
            self._results,
            str(output_dir),
            stop_metadata=self._stop_metadata(),
        )

        return self._results

    def _ensure_writable_output_dir(self) -> Path:
        """Try configured output_root, fall back to home/temp if not writable.

        Uses atomic file creation (os.open with O_CREAT|O_EXCL) to avoid
        TOCTOU race conditions when multiple processes or external cleanup
        touch the same directory concurrently.
        """
        import tempfile
        import uuid

        candidates = [
            Path(self.config.output_root),
            Path.home() / "bmc-auto-capture" / "output",
            Path(tempfile.gettempdir()) / "bmc-auto-capture" / "output",
        ]

        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                # Atomic write test: unique file per process
                probe_name = f".write_test_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
                probe_path = d / probe_name
                try:
                    fd = os.open(
                        str(probe_path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.close(fd)
                except FileExistsError:
                    # Extremely unlikely (UUID collision), retry once
                    probe_name = f".write_test_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
                    probe_path = d / probe_name
                    fd = os.open(
                        str(probe_path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.close(fd)
                finally:
                    # Clean up our own test file only
                    try:
                        if probe_path.exists():
                            probe_path.unlink()
                    except OSError:
                        pass

                if d != candidates[0]:
                    logger.warning("Output dir '%s' not writable, using '%s'", candidates[0], d)
                    print(f"WARNING: Cannot write to {candidates[0]}, output: {d}")
                return d
            except (OSError, PermissionError):
                continue

        # Last resort
        raise OSError("No writable output directory found")

    def stop(self):
        self._record_stop("user_stop", "App.stop")
        self._stop_event.set()
        self._pause_event.set()  # Unblock pause so stop can take effect
        # P0-3: forward to active scheduler if running in full mode
        if self._active_scheduler is not None:
            try:
                self._active_scheduler.stop(reason="user_stop", triggered_by="App.stop")
            except Exception:
                pass

    def pause(self):
        self._pause_event.clear()
        # P0-3: forward to active scheduler if running in full mode
        if self._active_scheduler is not None:
            try:
                self._active_scheduler.pause()
            except Exception:
                pass

    def resume(self):
        self._pause_event.set()
        # P0-3: forward to active scheduler if running in full mode
        if self._active_scheduler is not None:
            try:
                self._active_scheduler.resume()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Route guard callback
    # ------------------------------------------------------------------
    def _on_route_change(self, changes: list[str]):
        threshold = int(getattr(self.config, "route_guard_stop_threshold", 100) or 100)
        if len(changes) < threshold:
            logger.warning(
                "RouteGuard: %d route changes detected — observe only "
                "(scope=system_routes impact=none threshold=%d). "
                "Dispatch continues; BMC SPA navigation/session recovery is handled per device/session.",
                len(changes),
                threshold,
            )
            self.event_bus.emit(
                "route_changed",
                changes=changes,
                scope="system_routes",
                impact="observe",
                threshold=threshold,
            )
            return

        logger.warning(
            "RouteGuard: %d route changes detected — global route storm, stopping dispatch "
            "(scope=system_routes impact=global threshold=%d)",
            len(changes),
            threshold,
        )
        self._record_stop("route_change", "RouteGuard")
        self._stop_event.set()
        self._pause_event.set()  # unblock pause so stop can take effect
        # AUDIT-003: forward to active scheduler with route_change reason
        if self._active_scheduler is not None:
            try:
                self._active_scheduler.stop(reason="route_change", triggered_by="RouteGuard")
            except Exception:
                pass
        self.event_bus.emit("route_changed", changes=changes)

    # ------------------------------------------------------------------
    # Sequential executor
    # ------------------------------------------------------------------
    def _execute_sequential(self, plans: list[TaskPlan]):
        ssh_exec = SSHExecutor(connect_timeout=self.config.tcp_connect_timeout,
                               command_timeout=self.config.ssh_command_timeout,
                               idle_timeout=self.config.ssh_idle_timeout)
        bm = BrowserManager(headless=self.config.browser_headless)
        bmc_exec = BMCExecutor(bm, connect_timeout=self.config.tcp_connect_timeout,
                                 page_timeout=self.config.bmc_page_timeout,
                                 popup_timeout=self.config.popup_dismiss_selector_timeout,
                                 artifact_profile=getattr(self.config, "bmc_artifact_profile", "full"))

        total = len(plans)
        logger.info("Sequential execution of %d plans", total)

        for i, plan in enumerate(plans):
            if self._stop_event.is_set():
                logger.info("Stop requested — %d plans remaining", total - i)
                self._affected_pending_count = total - i
                if self._stopped_at <= 0:
                    self._record_stop(self._stop_reason, "external_stop_event")
                for remaining in plans[i:]:
                    remaining.status = (
                        "EXEC_SKIPPED_ROUTE_CHANGED"
                        if self._stop_reason == "route_change"
                        else "EXEC_SKIPPED_STOPPED"
                    )
                    now = time.time()
                    skipped_result = ExecutionResult(
                        plan_id=remaining.plan_id,
                        task_id=remaining.task_id,
                        client_task_id=remaining.client_task_id,
                        device_name=remaining.device.device_name,
                        device_group=remaining.device.device_group,
                        bmc_ip=remaining.device.bmc_ip,
                        inband_ip=remaining.device.inband_ip,
                        task_name=remaining.task.task_name,
                        task_type=remaining.task.task_type,
                        execution_mode=remaining.task.execution_mode,
                        execution_status=remaining.status,
                        execution_failure_reason=(
                            f"顺序执行停止: {self._stop_reason_label()} "
                            f"triggered_by={self._stop_triggered_by or 'unknown'} "
                            f"affectedPendingCount={self._affected_pending_count}"
                        ),
                        started_at=now,
                        ended_at=now,
                        duration_seconds=0.001,
                        endpoint_key=remaining.endpoint_key,
                        endpoint_type=remaining.endpoint_type,
                    )
                    self._compute_verdict(skipped_result)
                    self._results.append(skipped_result)
                break

            self._pause_event.wait()

            plan.status = "RUNNING"
            plan.started_at = time.time()
            self.event_bus.emit("plan_started", plan=plan, index=i, total=total)

            logger.info("开始 [%s] %s / %s (%d/%d)", plan.protocol, plan.device.device_name, plan.task.task_name, i+1, total)
            print(f"  START [{plan.protocol}] {plan.device.device_name}  {plan.task.task_name}")

            try:
                if plan.protocol == "BMC":
                    result = execute_with_retry(bmc_exec, plan, self.config.output_root)
                elif plan.protocol == "SSH":
                    result = execute_with_retry(ssh_exec, plan, self.config.output_root)
                else:
                    result = ExecutionResult(
                        plan_id=plan.plan_id,
                        task_id=plan.task_id,
                        client_task_id=plan.client_task_id,
                        device_name=plan.device.device_name,
                        task_name=plan.task.task_name,
                        execution_status="EXEC_FAILED",
                        execution_failure_reason=f"Unsupported protocol: {plan.protocol}",
                        started_at=time.time(),
                        ended_at=time.time(),
                    )

                plan.completed_at = time.time()
                plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status
                self._compute_verdict(result)
                self._results.append(result)

                self.event_bus.emit("plan_completed", plan=plan, result=result, index=i, total=total)

                status_icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
                reason = ""
                if status_icon == "FAIL" and result.execution_failure_reason:
                    reason = f"  [{result.execution_failure_reason[:60]}]"
                print(f"[{i+1:>5}/{total}] {status_icon:>4} {plan.device.device_name}  {plan.task.task_name}{reason}")

            except Exception as e:
                logger.error("Plan %s crashed: %s", plan.plan_id, e)
                plan.status = "EXEC_ERROR"
                plan.completed_at = time.time()
                error_result = ExecutionResult(
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
                    execution_status="EXEC_ERROR",
                    execution_failure_reason=f"Sequential executor exception: {e}",
                    started_at=plan.started_at,
                    ended_at=plan.completed_at,
                    duration_seconds=max(0.0, plan.completed_at - plan.started_at),
                    endpoint_key=plan.endpoint_key,
                    endpoint_type=plan.endpoint_type,
                )
                self._compute_verdict(error_result)
                self._results.append(error_result)

        try:
            asyncio.run(bm.teardown())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Dynamic scheduler
    # ------------------------------------------------------------------
    def _execute_dynamic(self, plans: list[TaskPlan]):
        from .scheduler.dynamic_scheduler import DynamicScheduler

        # P0-3: save scheduler reference so App.stop/pause/resume can forward
        scheduler = DynamicScheduler(
            self.config,
            event_bus=self.event_bus,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
        )
        self._active_scheduler = scheduler
        try:
            results = scheduler.run(plans)
            self._results.extend(results)
            self._apply_scheduler_stop_metadata(scheduler)
        finally:
            self._active_scheduler = None

    # ------------------------------------------------------------------
    def _clear_stop_metadata(self) -> None:
        self._stop_triggered_by = ""
        self._stopped_at = 0.0
        self._affected_pending_count = 0

    def _record_stop(self, reason: str, triggered_by: str) -> None:
        if self._stopped_at <= 0:
            self._stop_reason = reason or self._stop_reason or "scheduler_stop"
            self._stop_triggered_by = triggered_by or "unknown"
            self._stopped_at = time.time()

    def _stop_reason_label(self) -> str:
        if self._stop_reason == "route_change":
            return "ROUTE_GUARD_STOPPED"
        if self._stop_reason in ("scheduler_stop", "user_stop"):
            return "USER_STOPPED"
        if self._stop_reason == "user_interrupt":
            return "USER_INTERRUPT"
        return (self._stop_reason or "").upper()

    def _stop_metadata(self) -> dict:
        return {
            "stopReason": self._stop_reason_label() if self._stopped_at > 0 else "",
            "stopTriggeredBy": self._stop_triggered_by if self._stopped_at > 0 else "",
            "stoppedAt": self._stopped_at,
            "affectedPendingCount": self._affected_pending_count,
        }

    def _apply_scheduler_stop_metadata(self, scheduler) -> None:
        meta = getattr(scheduler, "stop_metadata", {}) or {}
        if meta.get("stoppedAt"):
            self._stop_reason = getattr(scheduler, "_stop_reason", self._stop_reason)
            self._stop_triggered_by = str(meta.get("stopTriggeredBy") or "")
            self._stopped_at = float(meta.get("stoppedAt") or 0.0)
            self._affected_pending_count = int(meta.get("affectedPendingCount") or 0)

    # ------------------------------------------------------------------
    def _print_validation(self, report: ValidationReport):
        for msg in report.warnings[:10]:
            logger.warning("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)
        if len(report.warnings) > 10:
            logger.warning("... and %d more warnings", len(report.warnings) - 10)
        for msg in report.errors:
            logger.error("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)

    # ------------------------------------------------------------------
    def _compute_verdict(self, result: ExecutionResult) -> None:
        """Derive final_verdict from execution/artifact/checkpoint/ready status.
        Delegates to standalone compute_verdict() for consistency across all modes.
        """
        result.final_verdict = compute_verdict(result)

    # ------------------------------------------------------------------
    # Run with pre-parsed plans (in-memory execution via API)
    # ------------------------------------------------------------------
    def run_with_plans(self, plans: list[TaskPlan], mode: str = "full") -> list[ExecutionResult]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("RUN_ALREADY_ACTIVE")
        try:
            return self._run_with_plans(plans, mode)
        finally:
            self._active_scheduler = None
            self._run_lock.release()

    def _run_with_plans(self, plans: list[TaskPlan], mode: str = "full") -> list[ExecutionResult]:
        """Execute pre-parsed plans directly without loading Excel.
        Called by internal runners when an in-memory plan list is provided.

        mode: "sequential" = one-by-one; "full" = endpoint-aware dynamic scheduler.
        """
        # AUDIT-003: clear state before each run to support App instance reuse
        self._results = []
        self._stop_event.clear()
        self._pause_event.set()
        self._stop_reason = "scheduler_stop"
        self._clear_stop_metadata()

        session = RunSession.start(self.config.output_root)
        execution_started_at = session.started_at
        self.config.output_root = session.output_root
        self._assign_batch_identity(plans, session.plan_id)
        logger.info("输出目录 (in-memory):  %s", self.config.output_root)
        logger.info("本次执行批次 plan_id: %s", session.plan_id)

        self.event_bus.emit("plans_generated", count=len(plans))

        # Connectivity preflight (same as run())
        if self.config.preflight_enabled:
            logger.info("正在执行网络连通性预检...")
            pr = preflight_check_all([p.device for p in plans],
                                     timeout=self.config.tcp_connect_timeout,
                                     max_workers=self.config.max_bmc_workers + self.config.max_ssh_workers)
            plans = apply_preflight(plans, pr)
            for p in plans:
                if p.status.startswith("EXEC_SKIPPED"):
                    reason = p.skip_reason or "网络预检不通"
                    logger.info("Skip: %s / %s → %s", p.device.device_name, p.task.task_name, reason)
                    result = ExecutionResult(
                        plan_id=p.plan_id,
                        task_id=p.task_id,
                        client_task_id=p.client_task_id,
                        device_name=p.device.device_name,
                        device_group=p.device.device_group,
                        bmc_ip=p.device.bmc_ip,
                        inband_ip=p.device.inband_ip,
                        task_name=p.task.task_name,
                        task_type=p.task.task_type,
                        execution_mode=p.task.execution_mode,
                        execution_status=p.status,
                        execution_failure_reason=reason,
                        started_at=time.time(),
                        ended_at=time.time(),
                    )
                    self._compute_verdict(result)
                    self._results.append(result)
            skipped_count = sum(1 for p in plans if p.status.startswith("EXEC_SKIPPED"))
            logger.info("预检: %d 个计划已跳过 (%d 个就绪)", skipped_count, len(plans) - skipped_count)

        # Route guard
        route_guard = None
        if self.config.route_guard_enabled:
            route_guard = RouteGuard(check_interval=self.config.route_guard_check_interval)
            route_guard.on_change = self._on_route_change
            route_guard.start()

        # Execute
        ready_plans = [p for p in plans if not p.status.startswith("EXEC_SKIPPED")]
        logger.info("正在执行 %d 个计划 (mode=%s)", len(ready_plans), mode)

        if mode == "full":
            self._execute_dynamic(ready_plans)
        else:
            self._execute_sequential(ready_plans)

        # Route guard cleanup
        if route_guard:
            route_guard.stop()

        # Collect
        output_dir = self._ensure_writable_output_dir()
        self._result_writer.write(
            self._results,
            str(output_dir),
            execution_started_at=execution_started_at,
            stop_metadata=self._stop_metadata(),
        )

        return self._results

    @staticmethod
    def _assign_batch_identity(plans: list[TaskPlan], plan_id: str) -> None:
        """Ensure all provided plans belong to one execution batch."""
        for plan in plans:
            plan.plan_id = plan_id
            if not plan.task_id:
                plan.task_id = plan.task.effective_task_id
            plan.plan_item_id = make_plan_item_id(
                plan.plan_id,
                plan.device_id,
                plan.effective_task_id,
            )
