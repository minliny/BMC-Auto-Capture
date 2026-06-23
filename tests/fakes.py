"""
Unified fake/mock layer for offline testing.

Provides fake executors, session runners, browser managers, and guards
that prevent accidental real-browser or network access in test suites.

Usage in a test:
    from tests.fakes import (
        FakeBMCExecutor, FakeSSHExecutor, FakeBMCSessionRunner,
        NoBrowserGuard, NoNetworkGuard,
        fake_executor_factory, fake_bmc_session_runner_factory,
        make_fake_app,
    )
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig


# ======================================================================
# Fake executors
# ======================================================================

class FakeBMCExecutor:
    """BMC executor that never creates a browser or touches the network."""

    def __init__(self, config=None, sleep_seconds: float = 0.05,
                 result_status: str = "EXEC_SUCCESS",
                 fill_timing: bool = True):
        self.sleep_seconds = sleep_seconds
        self.result_status = result_status
        self.fill_timing = fill_timing
        self.execute_calls: list[TaskPlan] = []

    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        self.execute_calls.append(plan)
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        started = time.time()
        ended = started + self.sleep_seconds
        return ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            device_group=plan.device.device_group,
            bmc_ip=plan.device.bmc_ip,
            inband_ip=plan.device.inband_ip,
            task_name=plan.task.task_name,
            task_type=plan.task.task_type,
            execution_mode=plan.task.execution_mode,
            execution_status=self.result_status,
            started_at=started,
            ended_at=ended,
            duration_seconds=round(ended - started, 3),
            endpoint_key=plan.endpoint_key,
            endpoint_type=plan.endpoint_type,
        )


class FakeSSHExecutor:
    """SSH executor that never touches paramiko or the network."""

    def __init__(self, sleep_seconds: float = 0.05,
                 result_status: str = "EXEC_SUCCESS"):
        self.sleep_seconds = sleep_seconds
        self.result_status = result_status
        self.execute_calls: list[TaskPlan] = []

    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        self.execute_calls.append(plan)
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        started = time.time()
        ended = started + self.sleep_seconds
        return ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            device_group=plan.device.device_group,
            bmc_ip=plan.device.bmc_ip,
            inband_ip=plan.device.inband_ip,
            task_name=plan.task.task_name,
            task_type=plan.task.task_type,
            execution_mode=plan.task.execution_mode,
            execution_status=self.result_status,
            started_at=started,
            ended_at=ended,
            duration_seconds=round(ended - started, 3),
            endpoint_key=plan.endpoint_key,
            endpoint_type=plan.endpoint_type,
        )


# ======================================================================
# Fake session runner
# ======================================================================

class FakeBMCSessionRunner:
    """Session runner that never creates a browser.

    Executes plans sequentially within the group, sleeping between each.
    Tracks login/logout counts for verification.
    """

    def __init__(self, browser_manager=None, endpoint_key="", plans=None,
                 output_root="", connect_timeout=30, page_timeout=60,
                 artifact_profile="full", on_plan_done=None, on_group_done=None):
        self.plans = list(plans) if plans else []
        self.endpoint_key = endpoint_key
        self.output_root = output_root
        self.artifact_profile = artifact_profile
        self.on_plan_done = on_plan_done
        self.on_group_done = on_group_done
        self.sleep_seconds = 0.05
        self.login_count = 0
        self.logout_count = 0
        self.session_started_at = 0.0
        self.session_finished_at = 0.0
        self.login_duration = 0.0

    def run(self) -> list[ExecutionResult]:
        results = []
        self.session_started_at = time.time()

        # Simulate login once
        time.sleep(self.sleep_seconds * 0.5)
        self.login_count = 1
        self.login_duration = self.sleep_seconds * 0.5

        for plan in self.plans:
            plan.status = "RUNNING"
            plan.executor_started_at = time.time()
            time.sleep(self.sleep_seconds)
            plan.executor_finished_at = time.time()
            plan.ended_at = plan.executor_finished_at

            plan.status = "SUCCESS"
            r = ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                device_group=plan.device.device_group,
                bmc_ip=plan.device.bmc_ip,
                task_name=plan.task.task_name,
                task_type=plan.task.task_type,
                execution_status="EXEC_SUCCESS",
                started_at=plan.executor_started_at,
                ended_at=plan.executor_finished_at,
                duration_seconds=round(self.sleep_seconds, 3),
                endpoint_key=plan.endpoint_key,
                endpoint_type=plan.endpoint_type,
                executor_duration_seconds=round(self.sleep_seconds, 3),
            )
            results.append(r)
            if self.on_plan_done:
                self.on_plan_done(plan, r)

        # Simulate logout once
        time.sleep(self.sleep_seconds * 0.25)
        self.logout_count = 1
        self.session_finished_at = time.time()

        if self.on_group_done:
            self.on_group_done(self.endpoint_key, results)

        return results


def fake_bmc_session_runner_factory(
    sleep_seconds: float = 0.05,
) -> Callable:
    """Return a factory that creates FakeBMCSessionRunners."""
    def factory(browser_manager=None, endpoint_key="", plans=None,
                output_root="", connect_timeout=30, page_timeout=60,
                artifact_profile="full", on_plan_done=None, on_group_done=None):
        runner = FakeBMCSessionRunner(
            browser_manager=browser_manager,
            endpoint_key=endpoint_key,
            plans=plans,
            output_root=output_root,
            artifact_profile=artifact_profile,
            on_plan_done=on_plan_done,
            on_group_done=on_group_done,
        )
        runner.sleep_seconds = sleep_seconds
        return runner
    return factory


# ======================================================================
# Guards
# ======================================================================

class NoBrowserGuard:
    """Context manager: raises if any real browser is created.

    Monkey-patches BrowserManager to prevent Playwright instantiation.
    """

    _orig_get_context = None
    _active = False

    @classmethod
    def install(cls):
        if cls._active:
            return
        from src.executor.browser_manager import BrowserManager, _get_thread_loop
        cls._orig_get_context = BrowserManager.get_context

        async def _block_get_context(self):
            raise AssertionError(
                "NoBrowserGuard: offline test attempted to create real browser. "
                "Use FakeBMCSessionRunner or mock BrowserManager."
            )
        BrowserManager.get_context = _block_get_context
        cls._active = True

    @classmethod
    def uninstall(cls):
        if not cls._active:
            return
        from src.executor.browser_manager import BrowserManager
        if cls._orig_get_context:
            BrowserManager.get_context = cls._orig_get_context
        cls._active = False


class NoNetworkGuard:
    """Context manager: raises if any network connection is attempted."""

    _active = False

    @classmethod
    def install(cls):
        if cls._active:
            return
        import socket
        cls._orig_socket_connect = socket.socket.connect

        def _block_connect(self, *args, **kwargs):
            raise AssertionError(
                "NoNetworkGuard: offline test attempted network connection. "
                "Use FakeSSHExecutor or mock."
            )
        socket.socket.connect = _block_connect
        cls._active = True

    @classmethod
    def uninstall(cls):
        if not cls._active:
            return
        import socket
        if cls._orig_socket_connect:
            socket.socket.connect = cls._orig_socket_connect
        cls._active = False


def offline_test_guard():
    """Install both guards. Returns a cleanup function."""
    NoBrowserGuard.install()
    NoNetworkGuard.install()
    def cleanup():
        NoBrowserGuard.uninstall()
        NoNetworkGuard.uninstall()
    return cleanup


# ======================================================================
# Fake App / run_with_plans helpers
# ======================================================================

def make_fake_dynamic_scheduler(config: AppConfig, sleep_seconds: float = 0.05):
    """Create a DynamicScheduler subclass with fake executors and session runner.

    The returned scheduler class has:
    - Mocked BMCEndpointSessionRunner (no browser)
    - Mocked _execute_plan for SSH/INBAND (no paramiko)
    """
    from src.scheduler.dynamic_scheduler import DynamicScheduler

    class FakeScheduler(DynamicScheduler):
        def __init__(self, config, event_bus=None):
            super().__init__(config, event_bus=event_bus)
            self._fake_sleep = sleep_seconds

        def _execute_plan(self, plan):
            plan.executor_started_at = time.time()
            time.sleep(self._fake_sleep)
            plan.executor_finished_at = time.time()
            r = ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                task_name=plan.task.task_name,
                execution_status="EXEC_SUCCESS",
                started_at=plan.executor_started_at,
                ended_at=plan.executor_finished_at,
                duration_seconds=self._fake_sleep,
                endpoint_key=plan.endpoint_key,
                endpoint_type=plan.endpoint_type,
            )
            plan.ended_at = plan.executor_finished_at
            return r

    # Also mock the session runner import path in _dispatch
    orig_module = None
    try:
        import src.scheduler.bmc_session_runner as bsr
        orig_runner_cls = bsr.BMCEndpointSessionRunner

        class _FakeRunnerForScheduler(FakeBMCSessionRunner):
            pass
        _FakeRunnerForScheduler.sleep_seconds = sleep_seconds

        def _fake_runner_factory(*args, **kwargs):
            r = _FakeRunnerForScheduler(
                browser_manager=kwargs.get('browser_manager'),
                endpoint_key=kwargs.get('endpoint_key', ''),
                plans=kwargs.get('plans', []),
                output_root=kwargs.get('output_root', ''),
                artifact_profile=kwargs.get('artifact_profile', 'full'),
                on_plan_done=kwargs.get('on_plan_done'),
                on_group_done=kwargs.get('on_group_done'),
            )
            r.sleep_seconds = sleep_seconds
            return r

        bsr.BMCEndpointSessionRunner = _fake_runner_factory
        orig_module = (bsr, orig_runner_cls)
    except ImportError:
        pass

    return FakeScheduler, orig_module


def restore_session_runner(orig_module):
    """Restore the original BMCEndpointSessionRunner after test."""
    if orig_module:
        bsr, orig_cls = orig_module
        bsr.BMCEndpointSessionRunner = orig_cls
