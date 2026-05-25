"""
App — composition root. Wires all components, provides unified execution interface.

All three interfaces (API, TUI, GUI) call App.run().
No interface reimplements execution logic.
"""


from __future__ import annotations
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .models.app_config import AppConfig
from .models.task_plan import TaskPlan
from .models.execution_result import ExecutionResult
from .loader.excel_reader import load_all
from .loader.schema_validator import validate, ValidationReport
from .scheduler.plan_generator import generate_plans
from .executor.base import AbstractExecutor
from .executor.ssh_executor import SSHExecutor
from .executor.bmc_executor import BMCExecutor
from .executor.browser_manager import BrowserManager
from .output.collector import write_result_csv, write_final_result_csv, compute_summary
from .output.summary import build_pivot_csv, print_terminal_summary

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
        self._pause_event.set()  # Not paused by default

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, excel_path: str | Path) -> list[ExecutionResult]:
        """Full pipeline: load → validate → plan → execute → collect. Synchronous."""
        excel_path = Path(excel_path)

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

        # 4. Execute (simple sequential scheduler for P4; P5 upgrades to dynamic)
        self._execute_sequential(plans)

        # 5. Collect
        output_dir = Path(self.config.output_root)
        output_dir.mkdir(parents=True, exist_ok=True)

        write_result_csv(self._results, str(output_dir))
        write_final_result_csv(self._results, str(output_dir))

        try:
            build_pivot_csv(self._results, str(output_dir))
        except Exception as e:
            logger.warning("Failed to build pivot table: %s", e)

        print_terminal_summary(self._results)

        return self._results

    def stop(self):
        self._stop_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    # ------------------------------------------------------------------
    # Sequential executor (P4 — replaced by DynamicScheduler in P5)
    # ------------------------------------------------------------------
    def _execute_sequential(self, plans: list[TaskPlan]):
        """Simple sequential executor: each plan runs one at a time.

        P5 (DynamicScheduler) will replace this with device-serialized,
        protocol-split, dynamically-sized worker pools.
        """
        ssh_exec = SSHExecutor(connect_timeout=self.config.tcp_connect_timeout)
        bm = BrowserManager(headless=self.config.browser_headless)
        bmc_exec = BMCExecutor(bm, connect_timeout=self.config.tcp_connect_timeout)

        total = len(plans)
        logger.info("Starting execution of %d plans", total)

        for i, plan in enumerate(plans):
            if self._stop_event.is_set():
                logger.info("Stop requested — %d plans remaining", total - i)
                for remaining in plans[i:]:
                    remaining.status = "SKIPPED"
                break

            self._pause_event.wait()  # Block if paused

            plan.status = "RUNNING"
            plan.started_at = time.time()
            self.event_bus.emit("plan_started", plan=plan, index=i, total=total)

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
                    )

                plan.completed_at = time.time()
                plan.status = "SUCCESS" if result.execution_status == "EXEC_SUCCESS" else result.execution_status
                self._results.append(result)

                self.event_bus.emit("plan_completed", plan=plan, result=result, index=i, total=total)

                status_icon = "OK" if result.execution_status == "EXEC_SUCCESS" else "FAIL"
                print(f"[{i+1:>5}/{total}] {status_icon:>4} {plan.device.device_name[:20]:<20} {plan.task.task_name[:30]}")

            except Exception as e:
                logger.error("Plan %s crashed: %s", plan.plan_id, e)
                plan.status = "EXEC_ERROR"
                plan.completed_at = time.time()

        # Cleanup browser
        import asyncio
        try:
            asyncio.run(bm.teardown())
        except Exception:
            pass

    def _print_validation(self, report: ValidationReport):
        for msg in report.warnings[:10]:
            logger.warning("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)
        if len(report.warnings) > 10:
            logger.warning("... and %d more warnings", len(report.warnings) - 10)
        for msg in report.errors:
            logger.error("[%s row %d] %s: %s", msg.source, msg.row, msg.field, msg.message)
