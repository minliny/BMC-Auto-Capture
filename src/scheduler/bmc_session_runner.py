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
from ..models.verdict import AttemptRecord, compute_verdict, is_retryable_failure
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
        result_meta_by_plan_id: dict[str, tuple[str, str]] = {}

        def append_result_once(
            plan: TaskPlan, result: ExecutionResult, source: str,
        ) -> bool:
            plan_id = plan.plan_id or result.plan_id
            if not plan_id:
                logger.error(
                    "[SessionRunner] result rejected without plan_id source=%s",
                    source,
                )
                return False
            result.plan_id = plan_id
            old_meta = result_meta_by_plan_id.get(plan_id)
            if old_meta is not None:
                logger.warning(
                    "[SessionRunner] duplicate final result discarded "
                    "plan_id=%s old_status=%s new_status=%s "
                    "old_source=%s new_source=%s",
                    plan_id,
                    old_meta[0],
                    result.execution_status,
                    old_meta[1],
                    source,
                )
                return False
            result_meta_by_plan_id[plan_id] = (result.execution_status, source)
            results.append(result)
            if self._on_plan_done:
                self._on_plan_done(plan, result)
            return True

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
                _fail_ts = time.time()
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
                        started_at=_fail_ts,
                        ended_at=_fail_ts,
                        duration_seconds=0.001,
                        endpoint_key=self._endpoint_key,
                        endpoint_type="BMC",
                    )
                    r.final_verdict = compute_verdict(r)
                    append_result_once(plan, r, "login_failure")
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

                def make_result(attempt_output_dir: str, started_at: float) -> ExecutionResult:
                    return ExecutionResult(
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
                        started_at=started_at,
                        output_dir=attempt_output_dir,
                        endpoint_key=self._endpoint_key,
                        endpoint_type="BMC",
                    )

                result = make_result(output_dir, plan.started_at)

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
                        append_result_once(plan, result, "session_relogin_failure")
                        continue

                # --- Execute with per-plan timeout and optional retry ---
                max_retries = max(0, task.retry_count)
                plan_timeout = (
                    float(task.timeout_seconds)
                    if task.timeout_seconds > 0
                    else float(self._page_timeout)
                )
                _attempts_data: list[AttemptRecord] = []
                _retry_reasons: list[str] = []

                for attempt_idx in range(max_retries + 1):
                    plan.retry_attempt = attempt_idx
                    _attempt_start = time.time()
                    attempt_output_dir = (
                        os.path.join(output_dir, f"attempt_{attempt_idx + 1}")
                        if max_retries > 0 else output_dir
                    )
                    os.makedirs(attempt_output_dir, exist_ok=True)
                    attempt_result = make_result(attempt_output_dir, _attempt_start)

                    try:
                        await asyncio.wait_for(
                            _exec._run_capture_flow(
                                page, task, device, device.bmc_ip,
                                attempt_output_dir, attempt_result,
                            ),
                            timeout=plan_timeout,
                        )
                    except asyncio.TimeoutError:
                        elapsed = time.time() - plan.executor_started_at
                        attempt_result.execution_status = "EXEC_TIMEOUT"
                        attempt_result.execution_failure_reason = (
                            f"BMC plan timeout: exceeded {plan_timeout}s "
                            f"(task.timeout_seconds={task.timeout_seconds}, "
                            f"elapsed={elapsed:.0f}s)"
                        )
                        logger.error(
                            "[SessionRunner] %s plan %d/%d timeout: %s",
                            self._endpoint_key, idx + 1, len(self._plans),
                            attempt_result.execution_failure_reason,
                        )
                    except Exception as e:
                        logger.error(
                            "[SessionRunner] %s plan %d/%d crashed: %s",
                            self._endpoint_key, idx + 1, len(self._plans), e,
                        )
                        if attempt_result.execution_status not in ("EXEC_FAILED",):
                            attempt_result.execution_status = "EXEC_ERROR"
                            attempt_result.execution_failure_reason = str(e)

                    # --- Plan health check ---
                    if attempt_result.execution_status not in ("EXEC_FAILED", "EXEC_ERROR", "EXEC_TIMEOUT"):
                        hr2 = await check_bmc_page_health(
                            page, "after_plan",
                            target_url=task.command_or_url or "",
                        )
                        if not hr2.healthy:
                            attempt_result.execution_status = "EXEC_FAILED"
                            attempt_result.execution_failure_reason = (
                                f"BMC_PAGE_HEALTH_FAILED [{hr2.status}]: {hr2.details}"
                            )
                            logger.error(
                                "[SessionRunner] %s — plan %d/%d health failed: %s",
                                self._endpoint_key, idx + 1, len(self._plans), hr2.status,
                            )

                    if attempt_result.execution_status not in (
                        "EXEC_FAILED", "EXEC_ERROR", "EXEC_PARTIAL", "EXEC_TIMEOUT",
                    ):
                        attempt_result.execution_status = "EXEC_SUCCESS"

                    _attempt_end = time.time()
                    attempt_result.ended_at = _attempt_end
                    attempt_result.duration_seconds = round(
                        _attempt_end - _attempt_start, 3,
                    )
                    _attempts_data.append(AttemptRecord(
                        attempt_index=attempt_idx,
                        max_retries=max_retries,
                        execution_status=attempt_result.execution_status,
                        execution_failure_reason=attempt_result.execution_failure_reason or "",
                        elapsed_seconds=round(_attempt_end - _attempt_start, 3),
                        started_at=_attempt_start,
                        ended_at=_attempt_end,
                        output_dir=attempt_output_dir,
                        artifact_paths=tuple(
                            path for path in (
                                *attempt_result.screenshots,
                                *attempt_result.raw_screenshots,
                                attempt_result.html_file,
                                attempt_result.txt_file,
                            ) if path
                        ),
                        step_result_count=len(attempt_result.step_results),
                    ))
                    result = attempt_result

                    # Decide whether to retry
                    if attempt_result.execution_status == "EXEC_SUCCESS":
                        break
                    if not is_retryable_failure(attempt_result):
                        break
                    if attempt_idx >= max_retries:
                        break

                    # Retryable failure with retries left → re-login, then retry
                    logger.warning(
                        "[SessionRunner] %s plan %d/%d attempt %d/%d retryable: %s — re-login & retry",
                        self._endpoint_key, idx + 1, len(self._plans),
                        attempt_idx + 1, max_retries + 1,
                        (attempt_result.execution_failure_reason or "")[:60],
                    )
                    _retry_reasons.append(
                        attempt_result.execution_failure_reason
                        or attempt_result.execution_status
                    )
                    login_ok_r, reason_r = await self._do_login(page, device, bmc_url)
                    self.login_count += 1
                    if not login_ok_r:
                        logger.warning(
                            "[SessionRunner] %s re-login failed: %s — aborting retry",
                            self._endpoint_key, reason_r,
                        )
                        break
                result.attempt_records = _attempts_data
                result.retry_count = max(0, len(_attempts_data) - 1)
                result.attempt_count = len(_attempts_data)
                result.max_attempts = max_retries + 1
                result.final_attempt_index = len(_attempts_data)
                result.retry_reasons = _retry_reasons

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
                result.final_verdict = compute_verdict(result)

                append_result_once(plan, result, "plan_complete")

                logger.info(
                    "[SessionRunner] %s plan %d/%d done: %s — %s",
                    self._endpoint_key, idx + 1, len(self._plans),
                    result.device_name, result.execution_status,
                )

            # --- Best-effort logout ---
            await self._do_logout(page, device)

        except asyncio.TimeoutError:
            logger.error("[SessionRunner] %s — session timeout", self._endpoint_key)
            # P0-4: _fail_ts was only defined in login-failure branch — fix UnboundLocalError
            _fail_ts = time.time()
            # Current plan (if any) gets timeout result
            # Mark remaining unexecuted plans as failed
            for plan in self._plans:
                if plan.status not in ("SUCCESS", "EXEC_FAILED", "EXEC_ERROR", "EXEC_PARTIAL", "EXEC_TIMEOUT"):
                    plan.status = "EXEC_TIMEOUT"
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
                        execution_status="EXEC_TIMEOUT",
                        execution_failure_reason="Session runner timeout",
                        started_at=_fail_ts,
                        ended_at=_fail_ts,
                        duration_seconds=0.001,
                        endpoint_key=self._endpoint_key,
                        endpoint_type="BMC",
                    )
                    r.final_verdict = compute_verdict(r)
                    append_result_once(plan, r, "session_timeout")
        except Exception as e:
            logger.error("[SessionRunner] %s crashed: %s", self._endpoint_key, e)
            # P0-4: ensure all remaining plans get a result (not just logged)
            _fail_ts = time.time()
            for plan in self._plans:
                if plan.status not in ("SUCCESS", "EXEC_FAILED", "EXEC_ERROR", "EXEC_PARTIAL", "EXEC_TIMEOUT"):
                    plan.status = "EXEC_ERROR"
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
                        execution_status="EXEC_ERROR",
                        execution_failure_reason=f"Session runner crashed: {e}",
                        started_at=_fail_ts,
                        ended_at=_fail_ts,
                        duration_seconds=0.001,
                        endpoint_key=self._endpoint_key,
                        endpoint_type="BMC",
                    )
                    r.final_verdict = compute_verdict(r)
                    append_result_once(plan, r, "session_exception")
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
