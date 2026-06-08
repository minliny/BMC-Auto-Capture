"""
BMC Endpoint Session Runner — login once, execute many, logout once.

Replaces the old per-plan login/logout cycle for BMC tasks on the same
endpoint.  When a DynamicScheduler dispatches a BMC endpoint group, all
plans for that endpoint share one browser page and one login session.

Lifecycle:
  acquire lock
  ├─ create browser context + page
  ├─ login once
  ├─ for each plan:
  │   ├─ health check (session alive)
  │   ├─ navigate to target
  │   ├─ run capture flow
  │   ├─ health check (page loaded)
  │   ├─ screenshot / html / mhtml
  │   └─ record result
  ├─ best-effort logout
  ├─ close page
  └─ release lock
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult
from ..executor.bmc_executor import BMCExecutor
from ..executor.browser_manager import BrowserManager
from ..executor.bmc_health_check import check_bmc_page_health, HealthResult

logger = logging.getLogger("bmc_auto_capture.session_runner")


class BMCEndpointSessionRunner:
    """Execute all BMC plans for one endpoint_key with a single login session.

    Designed to be called from DynamicScheduler as a worker.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        endpoint_key: str,
        plans: list[TaskPlan],
        output_root: str,
        connect_timeout: float = 30.0,
        page_timeout: float = 60.0,
        on_plan_done: Callable | None = None,
        on_group_done: Callable | None = None,
    ):
        if not plans:
            raise ValueError("plans must not be empty")
        self._bm = browser_manager
        self._endpoint_key = endpoint_key
        self._plans = list(plans)
        self._output_root = output_root
        self._connect_timeout = connect_timeout
        self._page_timeout = page_timeout
        self._on_plan_done = on_plan_done
        self._on_group_done = on_group_done

        # Timing
        self.session_started_at: float = 0.0
        self.login_duration: float = 0.0
        self.session_finished_at: float = 0.0
        self.login_count: int = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self) -> list[ExecutionResult]:
        """Execute all plans with a single login session.  Synchronous entry."""
        from ..executor.browser_manager import _get_thread_loop
        loop = _get_thread_loop()
        return loop.run_until_complete(self._run_async())

    async def _run_async(self) -> list[ExecutionResult]:
        self.session_started_at = time.time()
        results: list[ExecutionResult] = []

        page = None
        page_acquired = False

        try:
            # --- Acquire browser context + page ---
            context = await asyncio.wait_for(self._bm.get_context(), timeout=30)
            page = await asyncio.wait_for(context.new_page(), timeout=15)
            page_acquired = True
            page.set_default_timeout(self._page_timeout * 1000)
            logger.info(
                "[SessionRunner] %s — 浏览器就绪 (%d plans)",
                self._endpoint_key, len(self._plans),
            )

            # --- Login once ---
            first_plan = self._plans[0]
            device = first_plan.device
            bmc_url = f"https://{device.bmc_ip}"

            login_start = time.time()
            login_ok, login_reason = await self._do_login(page, device, bmc_url)
            self.login_duration = round(time.time() - login_start, 3)
            self.login_count = 1

            if not login_ok:
                # All plans in this group fail with login failure
                for plan in self._plans:
                    plan.status = "EXEC_FAILED"
                    r = ExecutionResult(
                        plan_id=plan.plan_id,
                        device_name=plan.device.device_name,
                        device_group=plan.device.device_group,
                        bmc_ip=plan.device.bmc_ip,
                        inband_ip=plan.device.inband_ip,
                        task_name=plan.task.task_name,
                        task_type=plan.task.task_type,
                        execution_status="EXEC_FAILED",
                        execution_failure_reason=login_reason or "BMC登录失败 (session group)",
                        started_at=time.time(),
                        ended_at=time.time(),
                        endpoint_key=self._endpoint_key,
                        endpoint_type="BMC",
                    )
                    results.append(r)
                    if self._on_plan_done:
                        self._on_plan_done(plan, r)
                return results

            logger.info(
                "[SessionRunner] %s — 登录成功 (%.1fs), 开始 %d 个任务",
                self._endpoint_key, self.login_duration, len(self._plans),
            )

            # --- Execute each plan ---
            for idx, plan in enumerate(self._plans):
                task = plan.task
                device = plan.device

                plan.status = "RUNNING"
                plan.started_at = time.time()
                plan.executor_started_at = time.time()

                # Build output dir
                from ..executor.bmc_executor import BMCExecutor as _BMC
                # We need a minimal executor for output dir / file naming helpers
                _exec = _BMC(self._bm, connect_timeout=self._connect_timeout,
                             page_timeout=self._page_timeout)

                output_dir = _exec._build_output_dir(self._output_root, device, task)
                import os
                os.makedirs(output_dir, exist_ok=True)

                result = ExecutionResult(
                    plan_id=plan.plan_id,
                    task_id=plan.task_id,
                    client_task_id=plan.client_task_id,
                    device_name=device.device_name,
                    device_group=device.device_group,
                    bmc_ip=device.bmc_ip,
                    inband_ip=device.inband_ip,
                    task_name=task.task_name,
                    task_type=task.task_type,
                    execution_mode=task.execution_mode,
                    started_at=plan.started_at,
                    output_dir=output_dir,
                    endpoint_key=self._endpoint_key,
                    endpoint_type="BMC",
                )

                # --- Health check: session alive ---
                hr = await check_bmc_page_health(page, "before_plan", target_url="")
                if not hr.healthy and hr.status in (
                    "BMC_ACCOUNT_LOGGED_IN_ELSEWHERE",
                    "BMC_SESSION_EXPIRED",
                    "BMC_LOGIN_PAGE_RETURNED",
                ):
                    # Session lost — try one re-login
                    logger.warning(
                        "[SessionRunner] %s — session lost (plan %d/%d): %s, 尝试重新登录",
                        self._endpoint_key, idx + 1, len(self._plans), hr.status,
                    )
                    login_ok2, reason2 = await self._do_login(page, device, bmc_url)
                    self.login_count += 1
                    if not login_ok2:
                        result.execution_status = "EXEC_FAILED"
                        result.execution_failure_reason = (
                            f"BMC_SESSION_INVALID [{hr.status}] + re-login failed: {reason2}"
                        )
                        result.ended_at = time.time()
                        result.duration_seconds = result.ended_at - result.started_at
                        plan.completed_at = result.ended_at
                        plan.status = "EXEC_FAILED"
                        results.append(result)
                        if self._on_plan_done:
                            self._on_plan_done(plan, result)
                        continue

                # --- Execute this plan's capture flow ---
                try:
                    await _exec._run_capture_flow(
                        page, task, device, device.bmc_ip, output_dir, result,
                    )
                except Exception as e:
                    logger.error(
                        "[SessionRunner] %s plan %d/%d crashed: %s",
                        self._endpoint_key, idx + 1, len(self._plans), e,
                    )
                    if result.execution_status not in ("EXEC_FAILED",):
                        result.execution_status = "EXEC_ERROR"
                        result.execution_failure_reason = str(e)

                # --- Plan health check ---
                if result.execution_status not in ("EXEC_FAILED", "EXEC_ERROR"):
                    hr2 = await check_bmc_page_health(
                        page, "after_plan",
                        target_url=task.command_or_url or "",
                    )
                    if not hr2.healthy:
                        result.execution_status = "EXEC_FAILED"
                        result.execution_failure_reason = (
                            f"BMC_PAGE_HEALTH_FAILED [{hr2.status}]: {hr2.details}"
                        )
                        logger.error(
                            "[SessionRunner] %s — plan %d/%d health failed: %s",
                            self._endpoint_key, idx + 1, len(self._plans), hr2.status,
                        )

                if result.execution_status not in ("EXEC_FAILED", "EXEC_ERROR", "EXEC_PARTIAL"):
                    result.execution_status = "EXEC_SUCCESS"

                plan.executor_finished_at = time.time()
                plan.ended_at = time.time()
                plan.completed_at = plan.ended_at
                plan.status = (
                    "SUCCESS" if result.execution_status == "EXEC_SUCCESS"
                    else result.execution_status
                )

                # Copy timing into result
                result.resource_wait_seconds = plan.resource_wait_seconds
                result.executor_duration_seconds = round(
                    plan.ended_at - plan.executor_started_at, 3,
                ) if plan.executor_started_at > 0 else 0.0
                result.duration_seconds = round(plan.ended_at - plan.started_at, 3)
                result.retry_count = plan.retry_attempt

                results.append(result)
                if self._on_plan_done:
                    self._on_plan_done(plan, result)

                logger.info(
                    "[SessionRunner] %s plan %d/%d done: %s — %s",
                    self._endpoint_key, idx + 1, len(self._plans),
                    result.device_name, result.execution_status,
                )

            # --- Best-effort logout ---
            await self._do_logout(page, device)

        except asyncio.TimeoutError:
            logger.error("[SessionRunner] %s — session timeout", self._endpoint_key)
            # Mark remaining unexecuted plans as failed
            for plan in self._plans:
                if plan.status not in ("SUCCESS", "EXEC_FAILED", "EXEC_ERROR", "EXEC_PARTIAL"):
                    plan.status = "EXEC_TIMEOUT"
                    r = ExecutionResult(
                        plan_id=plan.plan_id,
                        device_name=plan.device.device_name,
                        task_name=plan.task.task_name,
                        execution_status="EXEC_TIMEOUT",
                        execution_failure_reason="Session runner timeout",
                        started_at=time.time(),
                        ended_at=time.time(),
                    )
                    results.append(r)
                    if self._on_plan_done:
                        self._on_plan_done(plan, r)
        except Exception as e:
            logger.error("[SessionRunner] %s crashed: %s", self._endpoint_key, e)
        finally:
            # --- Cleanup: close page ---
            if page_acquired:
                try:
                    await asyncio.wait_for(page.close(), timeout=5)
                except Exception:
                    pass
            elif page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=5)
                except Exception:
                    pass

        self.session_finished_at = time.time()
        session_duration = round(self.session_finished_at - self.session_started_at, 3)
        logger.info(
            "[SessionRunner] %s — done: %d plans in %.1fs (login=%.1fs, logins=%d)",
            self._endpoint_key, len(results), session_duration,
            self.login_duration, self.login_count,
        )

        if self._on_group_done:
            self._on_group_done(self._endpoint_key, results)

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _do_login(self, page, device, bmc_url: str) -> tuple[bool, str]:
        """Perform BMC login. Returns (success, failure_reason)."""
        # Create a temporary executor just to call _bmc_login
        _exec = BMCExecutor(
            self._bm,
            connect_timeout=self._connect_timeout,
            page_timeout=self._page_timeout,
        )
        try:
            return await asyncio.wait_for(
                _exec._bmc_login(page, bmc_url, device),
                timeout=self._connect_timeout + 30,
            )
        except asyncio.TimeoutError:
            return False, "BMC登录超时 (session runner)"
        except Exception as e:
            return False, f"BMC登录异常: {e}"

    async def _do_logout(self, page, device) -> None:
        """Best-effort BMC logout.  Failure does not affect task results."""
        try:
            # Try common logout patterns
            logout_selectors = [
                'a:has-text("注销")',
                'a:has-text("退出")',
                'a:has-text("登出")',
                'a:has-text("Logout")',
                'a:has-text("Sign out")',
                'button:has-text("注销")',
                'button:has-text("退出")',
                'button:has-text("Logout")',
                '[title="注销"]',
                '[title="退出"]',
                '[title="Logout"]',
            ]
            for sel in logout_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        logger.info(
                            "[SessionRunner] %s — 正在注销 BMC session",
                            self._endpoint_key,
                        )
                        await el.click()
                        await asyncio.sleep(1)
                        return
                except Exception:
                    continue
            logger.debug(
                "[SessionRunner] %s — 无可用注销按钮, 跳过 logout",
                self._endpoint_key,
            )
        except Exception as e:
            logger.warning(
                "[SessionRunner] %s — logout 失败 (非致命): %s",
                self._endpoint_key, e,
            )
