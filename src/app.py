"""
App — composition root. Wires all components, provides unified execution interface.

All three interfaces (API, TUI, GUI) call App.run().
No interface reimplements execution logic.
"""

from __future__ import annotations
import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .models.app_config import AppConfig
from .models.task_plan import TaskPlan
from .models.execution_result import ExecutionResult
from .loader.excel_reader import load_all
from .loader.schema_validator import validate, ValidationReport
from .scheduler.plan_generator import generate_plans
from .connectivity.preflight import check_all as preflight_check_all, apply_preflight
from .connectivity.route_guard import RouteGuard
from .executor.ssh_executor import SSHExecutor
from .executor.bmc_executor import BMCExecutor
from .executor.browser_manager import BrowserManager
from .out.collector import write_result_csv, write_final_result_csv, compute_summary
from .out.summary import build_pivot_csv, print_terminal_summary

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, excel_path: str | Path, mode: str = "sequential") -> list[ExecutionResult]:
        """Full pipeline: load → validate → plan → preflight → routeguard → execute → collect."""
        excel_path = Path(excel_path)

        # 0. Set up timestamped output root to avoid cross-run overwrites
        run_ts = time.strftime("%Y%m%d_%H%M%S")
        self.config.output_root = str(Path(self.config.output_root) / run_ts)
        logger.info("Output directory: %s", self.config.output_root)

        # 1. Load
        logger.info("Loading Excel: %s", excel_path)
        devices, tasks = load_all(str(excel_path))
        logger.info("Loaded %d devices, %d tasks", len(devices), len(tasks))

        # 2. Validate
        report = validate(devices, tasks)
        self._print_validation(report)
        if not report.is_valid:
            logger.error("Validation failed with %d errors", len(report.errors))
            return []

        # 3. Generate plans
        plans = generate_plans(devices, tasks)
        if not plans:
            logger.warning("No plans generated — check device/task matching")
            return []

        self.event_bus.emit("plans_generated", count=len(plans))

        # 4. Connectivity preflight
        if self.config.preflight_enabled:
            logger.info("Running connectivity preflight...")
            pr = preflight_check_all(devices, timeout=self.config.tcp_connect_timeout)
            plans = apply_preflight(plans, pr)
            # Count & record ALL skipped plans (both PRECHECK_FAILED and PORT_BLOCKED)
            for p in plans:
                if p.status.startswith("EXEC_SKIPPED"):
                    reason = p.skip_reason or "网络预检不通"
                    logger.info("Skip: %s / %s → %s", p.device.device_name, p.task.task_name, reason)
                    self._results.append(ExecutionResult(
                        plan_id=p.plan_id,
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
                    ))
            skipped_count = sum(1 for p in plans if p.status.startswith("EXEC_SKIPPED"))
            logger.info("Preflight: %d plans skipped (%d ready)", skipped_count, len(plans) - skipped_count)

        # 5. Route guard
        route_guard = None
        if self.config.route_guard_enabled:
            route_guard = RouteGuard(check_interval=self.config.route_guard_check_interval)
            route_guard.on_change = self._on_route_change
            route_guard.start()

        # 6. Execute
        ready_plans = [p for p in plans if not p.status.startswith("EXEC_SKIPPED")]
        logger.info("Executing %d plans (%d skipped by preflight)", len(ready_plans), len(plans) - len(ready_plans))

        if mode == "full":
            self._execute_dynamic(ready_plans)
        else:
            self._execute_sequential(ready_plans)

        # 7. Route guard cleanup
        if route_guard:
            route_guard.stop()

        # 8. Collect — with fallback if output dir is not writable
        output_dir = self._ensure_writable_output_dir()

        write_result_csv(self._results, str(output_dir))
        write_final_result_csv(self._results, str(output_dir))

        try:
            build_pivot_csv(self._results, str(output_dir))
        except Exception as e:
            logger.warning("Failed to build pivot table: %s", e)

        summary = compute_summary(self._results)
        logger.info("Summary: %s", summary)
        print_terminal_summary(self._results)

        return self._results

    def _ensure_writable_output_dir(self) -> Path:
        """Try configured output_root, fall back to home/temp if not writable."""
        import tempfile

        candidates = [
            Path(self.config.output_root),
            Path.home() / "bmc-auto-capture" / "output",
            Path(tempfile.gettempdir()) / "bmc-auto-capture" / "output",
        ]

        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                # Test write
                test_file = d / ".write_test"
                test_file.touch()
                test_file.unlink()
                if d != candidates[0]:
                    logger.warning("Output dir '%s' not writable, using '%s'", candidates[0], d)
                    print(f"WARNING: Cannot write to {candidates[0]}, output: {d}")
                return d
            except (OSError, PermissionError):
                continue

        # Last resort
        raise OSError("No writable output directory found")

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # Unblock pause so stop can take effect

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    # ------------------------------------------------------------------
    # Route guard callback
    # ------------------------------------------------------------------
    def _on_route_change(self, changes: list[str]):
        logger.warning("RouteGuard: %d route changes detected — pausing dispatch", len(changes))
        self._stop_event.set()
        self.event_bus.emit("route_changed", changes=changes)

    # ------------------------------------------------------------------
    # Sequential executor
    # ------------------------------------------------------------------
    def _execute_sequential(self, plans: list[TaskPlan]):
        ssh_exec = SSHExecutor(connect_timeout=self.config.tcp_connect_timeout)
        bm = BrowserManager(headless=self.config.browser_headless)
        bmc_exec = BMCExecutor(bm, connect_timeout=self.config.tcp_connect_timeout)

        total = len(plans)
        logger.info("Sequential execution of %d plans", total)

        for i, plan in enumerate(plans):
            if self._stop_event.is_set():
                logger.info("Stop requested — %d plans remaining", total - i)
                for remaining in plans[i:]:
                    remaining.status = "EXEC_SKIPPED_ROUTE_CHANGED"
                break

            self._pause_event.wait()

            plan.status = "RUNNING"
            plan.started_at = time.time()
            self.event_bus.emit("plan_started", plan=plan, index=i, total=total)

            logger.info("START [%s] %s / %s (%d/%d)", plan.protocol, plan.device.device_name, plan.task.task_name, i+1, total)
            print(f"  START [{plan.protocol}] {plan.device.device_name}  {plan.task.task_name}")

            try:
                if plan.protocol == "BMC":
                    result = bmc_exec.execute(plan, self.config.output_root)
                elif plan.protocol == "SSH":
                    result = ssh_exec.execute(plan, self.config.output_root)
                else:
                    result = ExecutionResult(
                        plan_id=plan.plan_id,
                        device_name=plan.device.device_name,
                        task_name=plan.task.task_name,
                        execution_status="EXEC_FAILED",
                        execution_failure_reason=f"Unsupported protocol: {plan.protocol}",
                        started_at=time.time(),
                        ended_at=time.time(),
                    )

                plan.completed_at = time.time()
                plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status
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

        try:
            asyncio.run(bm.teardown())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Dynamic scheduler
    # ------------------------------------------------------------------
    def _execute_dynamic(self, plans: list[TaskPlan]):
        from .scheduler.dynamic_scheduler import DynamicScheduler

        scheduler = DynamicScheduler(self.config, event_bus=self.event_bus)
        results = scheduler.run(plans)
        self._results.extend(results)

    # ------------------------------------------------------------------
    def _print_validation(self, report: ValidationReport):
        for msg in report.warnings[:10]:
            logger.warning("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)
        if len(report.warnings) > 10:
            logger.warning("... and %d more warnings", len(report.warnings) - 10)
        for msg in report.errors:
            logger.error("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)
