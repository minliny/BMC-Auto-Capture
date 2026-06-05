"""
BMC executor — Playwright-based browser automation for BMC pages.

Handles:
- HTTPS cert bypass
- Login flow (credential fill, submit, CAPTCHA detection)
- Post-login popup dismissal
- URL navigation (direct or action DSL)
- Full-page screenshot with info overlay
- HTML save
"""


from __future__ import annotations
import asyncio
import io
import logging
import os
import shutil
import time

from PIL import Image

from .base import AbstractExecutor
from .browser_manager import BrowserManager
from .captcha_handler import detect_captcha, handle_captcha, CaptchaDetected
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult, StepResult
from ..models.checkpoint import CheckpointSpec
from ..rules.condition_evaluator import (
    evaluate_ready_conditions, evaluate_evidence_checkpoints,
    parse_ready_specs, parse_checkpoint_specs,
    ArtifactContext, ConditionResult, ConditionEvaluationResult,
)
from ..out.file_writer import write_html_file, write_log_file
from ..utils.template import resolve_template, check_unreplaced_vars

logger = logging.getLogger("bmc_auto_capture.bmc")

# --- Common post-login popup patterns ---
POPUP_DISMISS_SELECTORS = [
    # "Password risk" / "Password expiring" / "Password insecure" warnings
    '#bt-modify-later',
    '#bt-modify-noever',
    'button:has-text("暂不修改")',
    'button:has-text("立即修改")',
    'button:has-text("不再提示")',
    'button:has-text("忽略")',
    'button:has-text("稍后")',
    'button:has-text("取消")',
    'button:has-text("Cancel")',
    'button:has-text("Later")',
    'button:has-text("Skip")',
    'button:has-text("Ignore")',
    'a:has-text("暂不修改")',
    'a:has-text("忽略")',
    # License / EULA accept
    'button:has-text("同意")',
    'button:has-text("Accept")',
    'button:has-text("I Agree")',
    # Generic close
    '.modal-footer button.btn-default',
    '.modal .close',
    '[data-dismiss="modal"]',
]

LOGIN_USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[name="Username"]',
    'input[name="user"]',
    'input[id="username"]',
    'input[id="account"]',
    'input[type="text"][placeholder*="用户名"]',
    'input[placeholder*="用户名"]',
]

LOGIN_PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[name="Password"]',
    'input[name="userpassword"]',
    'input[id="password"]',
    'input[type="password"]',
]

LOGIN_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("登录")',
    'button:has-text("Login")',
    'button:has-text("登 录")',
    'input[type="submit"]',
    'button.btn-primary',
]


def _checkpoint_rollup_from_condition(eval_result: ConditionEvaluationResult) -> str:
    """Map condition evaluator rollup to checkpoint_status values."""
    mapping = {
        "FAIL": "CHECK_FAIL",
        "WARN": "CHECK_WARN",
        "PASS": "CHECK_PASS",
        "SKIP": "CHECK_SKIP",
    }
    return mapping.get(eval_result.rollup(), "CHECK_SKIP")


class BMCLoginError(Exception):
    pass


class BMCExecutor(AbstractExecutor):
    """Execute BMC_URL and BMC_ACTIONS tasks.

    Uses thread-local persistent event loops so all BMC tasks on the
    same worker thread share one loop.  This eliminates loop-change
    browser recreation and Chromium process leaks.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        connect_timeout: float = 30.0,
        page_timeout: float = 60.0,
        screenshot_policy: str = "final_only",
    ):
        self._bm = browser_manager
        self._screenshot_policy = screenshot_policy
        self._connect_timeout = connect_timeout
        self._page_timeout = page_timeout

    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        from .browser_manager import _get_thread_loop
        loop = _get_thread_loop()
        return loop.run_until_complete(self._execute_async(plan, output_root))

    async def _execute_async(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        device = plan.device
        task = plan.task
        dname = device.device_name

        result = ExecutionResult(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            client_task_id=plan.client_task_id,
            device_name=dname,
            device_group=device.device_group,
            bmc_ip=device.bmc_ip,
            inband_ip=device.inband_ip,
            task_name=task.task_name,
            task_type=task.task_type,
            execution_mode=task.execution_mode,
            started_at=time.time(),
        )

        if not device.bmc_ip:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = "BMC IP为空"
            result.ended_at = time.time()
            result.duration_seconds = result.ended_at - result.started_at
            return result

        output_dir = self._build_output_dir(output_root, device, task)
        os.makedirs(output_dir, exist_ok=True)
        result.output_dir = output_dir

        page = None
        page_acquired = False
        current_stage = "init"

        # Hard task-level timeout: 2× task.timeout_seconds is the absolute ceiling.
        task_timeout = max(task.timeout_seconds, 30) * 2

        async def _run_with_stages():
            nonlocal page, page_acquired, current_stage

            # --- Stage 1: acquire browser context ---
            current_stage = "1/6 acquire_context"
            logger.info("[%s] Stage %s", dname, current_stage)
            context = await asyncio.wait_for(self._bm.get_context(), timeout=30)
            logger.info("[%s] 阶段 1/6: 浏览器就绪", dname)

            # --- Stage 2: acquire page ---
            current_stage = "2/6 acquire_page"
            logger.info("[%s] Stage %s", dname, current_stage)
            page = await asyncio.wait_for(context.new_page(), timeout=15)
            page_acquired = True
            page.set_default_timeout(self._page_timeout * 1000)
            logger.info("[%s] 阶段 2/6: 页面就绪", dname)

            # --- Stage 3: resolve URL ---
            current_stage = "3/6 resolve_url"
            logger.info("[%s] Stage %s", dname, current_stage)
            bmc_url = self._resolve_url(task.command_or_url, device.bmc_ip)
            logger.info("[%s] Stage 3/6: url=%s", dname, bmc_url)

            # --- Stage 4: login ---
            current_stage = "4/6 login"
            logger.info("[%s] Stage %s", dname, current_stage)
            login_ok, login_reason = await asyncio.wait_for(
                self._bmc_login(page, bmc_url, device), timeout=self._connect_timeout + 30,
            )
            if not login_ok:
                result.execution_status = "EXEC_FAILED"
                result.execution_failure_reason = login_reason or "BMC登录失败"
                result.ended_at = time.time()
                result.duration_seconds = result.ended_at - result.started_at
                return
            logger.info("[%s] 阶段 4/6: 登录 ok", dname)

            # --- Stage 5: dismiss popups + navigate + capture ---
            current_stage = "5/6 navigate_capture"
            logger.info("[%s] Stage %s", dname, current_stage)
            await self._dismiss_popups(page)

            if task.execution_mode in ("BMC_URL", "BMC_ACTIONS"):
                await self._run_capture_flow(page, task, device, device.bmc_ip, output_dir, result)
            logger.info("[%s] 阶段 5/6: 采集完成", dname)

            # --- Stage 6: success ---
            current_stage = "6/6 done"
            logger.info("[%s] Stage %s", dname, current_stage)
            # Only set success if no error/partial status was already recorded by capture flow
            if result.execution_status not in ("EXEC_FAILED", "EXEC_PARTIAL"):
                result.execution_status = "EXEC_SUCCESS"

        _t0 = time.time()
        try:
            await asyncio.wait_for(_run_with_stages(), timeout=task_timeout)

        except asyncio.TimeoutError:
            elapsed = time.time() - _t0
            result.execution_status = "EXEC_TIMEOUT"
            result.execution_failure_reason = (
                f"BMC任务超时: 卡在 {current_stage}, "
                f"已等待 {elapsed:.0f}s (硬上限 {task_timeout}s). "
                f"可能原因: browser启动失败 / page获取阻塞 / BMC页面无响应."
            )
            logger.error("[%s] BMC timeout at stage %s (%.0fs elapsed)", dname, current_stage, elapsed)
            # Force browser reset so next task on this thread doesn't reuse bad state
            self._bm.reset_thread()

        except Exception as e:
            result.execution_status = "EXEC_ERROR"
            result.execution_failure_reason = str(e)
            logger.error("[%s] BMC任务出错, 阶段 %s: %s", dname, current_stage, e)
            # If page acquisition itself failed, force browser reset
            if "acquire_page" in current_stage or "acquire_context" in current_stage:
                self._bm.reset_thread()

        finally:
            # Close page with timeout protection (page might be in broken state)
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

        result.ended_at = time.time()
        result.duration_seconds = result.ended_at - result.started_at

        file_base = resolve_template(task.image_name_template, device, task)
        log_path = write_log_file(output_dir, f"{file_base}.log", self._build_log(result))
        result.log_file = log_path

        return result

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def _bmc_login(self, page, bmc_url: str, device) -> tuple[bool, str]:
        """Navigate to BMC, detect login page, fill credentials, submit.
        Returns (success, failure_reason).
        """
        # Validate URL host matches device BMC IP before navigation
        self._validate_goto_url(bmc_url, device.bmc_ip)
        logger.info(f"[{device.device_name}] 正在访问BMC:  {bmc_url}")

        try:
            await page.goto(bmc_url, wait_until="domcontentloaded", timeout=self._connect_timeout * 1000)
        except Exception as e:
            reason = f"BMC页面无法访问: {e}"
            logger.error("[%s] %s", device.device_name, reason)
            return False, reason

        # Handle self-signed cert warning: "您的连接不是专用连接"
        if await self._bypass_cert_warning(page, device):
            await asyncio.sleep(2)

        await asyncio.sleep(2)  # Allow redirect to login page

        # Check for "account already logged in elsewhere" before login
        if await self._detect_account_conflict(page, device):
            return False, "BMC登录失败: 账户已在其他地方登录"

        # Check for CAPTCHA before login
        captcha_seen = await detect_captcha(page)
        if captcha_seen and not self._bm.headless:
            solved = await handle_captcha(page, os.path.dirname(page.url), timeout=120)
            if not solved:
                return False, "BMC登录失败: 验证码处理失败"

        # Find login form elements
        username_el = await self._find_visible(page, LOGIN_USERNAME_SELECTORS)
        password_el = await self._find_visible(page, LOGIN_PASSWORD_SELECTORS)

        if username_el and password_el:
            logger.info(f"[{device.device_name}] 检测到登录表单, 正在填写凭证")
            await username_el.fill(device.bmc_username)
            await password_el.fill(device.bmc_password)

            # Check for CAPTCHA after filling (some sites show it after username entry)
            captcha_seen = await detect_captcha(page)
            if captcha_seen:
                if self._bm.headless:
                    logger.error("CAPTCHA detected in headless mode — cannot proceed")
                    return False, "BMC登录失败: 验证码拦截(headless模式)"
                solved = await handle_captcha(page, os.path.dirname(page.url), timeout=120)
                if not solved:
                    return False, "BMC登录失败: 验证码处理失败"

            submit_el = await self._find_visible(page, LOGIN_SUBMIT_SELECTORS)
            if submit_el:
                await submit_el.click()
            else:
                await password_el.press("Enter")

            # Wait for navigation after login
            await asyncio.sleep(3)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Verify login succeeded: login form should be gone
            still_login = await self._find_visible(page, LOGIN_USERNAME_SELECTORS)
            if still_login:
                # Check for error messages
                error_selectors = [
                    '.login-error', '.alert-danger', '.error',
                    'text=密码错误', 'text=用户名不存在',
                    'text=Login failed', 'text=Invalid',
                ]
                error_text = ""
                for es in error_selectors:
                    try:
                        el = await page.query_selector(es)
                        if el:
                            error_text = await el.inner_text()
                            break
                    except Exception:
                        pass
                logger.error("[%s] Login failed: still on login page. Error: %s",
                             device.device_name, error_text or "unknown")
                return False, f"BMC登录失败: 账号或密码错误 ({error_text})" if error_text else "BMC登录失败: 账号或密码错误"

            # Check for account conflict message that may appear after redirect
            if await self._detect_account_conflict(page, device):
                return False, "BMC登录失败: 账户已在其他地方登录"

        return True, ""

    async def _bypass_cert_warning(self, page, device) -> bool:
        """Handle self-signed cert warning: 您的连接不是专用连接 → 高级 → 继续访问."""
        cert_indicators = [
            'text=您的连接不是专用连接',
            'text=Your connection is not private',
            'text=not private',
            '#details-button',
            '#proceed-link',
        ]
        found = False
        for sel in cert_indicators:
            try:
                el = await page.query_selector(sel)
                if el:
                    found = True
                    break
            except Exception:
                continue
        if not found:
            return False

        logger.info("[%s] 检测到证书警告页面，尝试跳过...", device.device_name)
        # Click "Advanced" / "高级"
        advanced_selectors = [
            '#details-button',
            'button:has-text("高级")',
            'button:has-text("Advanced")',
        ]
        for sel in advanced_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue

        # Click "Proceed to ... (unsafe)" / "继续访问...（不安全）"
        proceed_selectors = [
            '#proceed-link',
            'a:has-text("继续")',
            'a:has-text("Proceed")',
            'text=继续访问',
            'text=Proceed to',
        ]
        for sel in proceed_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("[%s] 已跳过证书警告", device.device_name)
                    return True
            except Exception:
                continue

        return False

    async def _detect_account_conflict(self, page, device) -> bool:
        """Check if BMC shows 'account already logged in elsewhere' message."""
        conflict_patterns = [
            'text=账户已在其他地方登录',
            'text=已在其他地方登录',
            'text=account already logged in',
            'text=already logged in elsewhere',
            'text=session conflict',
            'text=会话冲突',
            'text=该用户已登录',
            'text=用户已在线',
        ]
        for pattern in conflict_patterns:
            try:
                el = await page.query_selector(pattern)
                if el and await el.is_visible():
                    text = await el.inner_text()
                    logger.error("[%s] Account conflict detected: %s", device.device_name, text)
                    return True
            except Exception:
                continue
        return False

    async def _detect_target_blocker(self, page, target_url: str) -> str:
        """Check if page is blocked from reaching the target.
        Returns error message string if blocked, empty string if OK.
        """
        current_url = page.url

        # Check 1: still on login page
        login_indicators = [
            '#login-container', '#login-input', '#btLogin',
            'input[name="username"]', 'input[name="password"]',
        ]
        on_login = False
        for sel in login_indicators:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    on_login = True
                    break
            except Exception:
                continue

        if on_login or '/login' in current_url:
            # Check for password risk prompt
            risk_selectors = [
                '#bt-modify-later', '#bt-modify-noever', '#bt-modify-now',
                'button:has-text("暂不修改")', 'button:has-text("立即修改")',
                'button:has-text("不再提示")',
                'text=您的账号存在安全风险', 'text=建议修改密码',
            ]
            for sel in risk_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return (
                            f"目标页面未到达: 当前仍在登录页，存在密码风险提示({sel})。"
                            f"target_url={target_url} current_url={current_url}"
                        )
                except Exception:
                    continue

            # Generic login block
            return (
                f"目标页面未到达: 当前仍在登录页。"
                f"target_url={target_url} current_url={current_url}"
            )

        # Check 2: password risk prompt on target page
        try:
            el = await page.query_selector('#bt-modify-later')
            if el and await el.is_visible():
                return (
                    f"目标页面存在密码风险提示，未自动关闭。"
                    f"target_url={target_url} current_url={current_url}"
                )
        except Exception:
            pass

        # Check 3: target URL not reached (check path + hash fragment)
        if target_url:
            from urllib.parse import urlparse
            tp = urlparse(target_url)
            cp = urlparse(current_url)
            # Extract the meaningful part: path + fragment (BMC uses hash routing)
            target_route = (tp.path or "") + (tp.fragment or "")
            current_route = (cp.path or "") + (cp.fragment or "")
            # Also check: if target is NOT home but we landed on home, fail
            target_is_home = "/navigate/home" in (tp.fragment or "")
            current_is_home = "/navigate/home" in (cp.fragment or "") or current_route.endswith("/navigate/home")

            if target_route and target_route not in current_route:
                if current_is_home and not target_is_home:
                    return (
                        f"目标页面未到达: 当前在首页，期望在 {target_route}。"
                        f"可能登录后未正确跳转。target_url={target_url}"
                    )
                return (
                    f"目标页面路径不匹配: 期望={target_route} 实际={current_route}。"
                    f"target_url={target_url} current_url={current_url}"
                )

        return ""

    async def _dismiss_popups(self, page) -> None:
        """Try to dismiss common post-login popups (called after initial login)."""
        for _ in range(5):  # max 5 popups
            dismissed = False
            for sel in POPUP_DISMISS_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        logger.info("正在关闭弹窗:  %s", sel)
                        await el.click()
                        await asyncio.sleep(1)
                        dismissed = True
                        break
                except Exception:
                    continue
            if not dismissed:
                break

    async def _dismiss_all_blockers(self, page) -> None:
        """Aggressively dismiss ALL known blockers (popups, password risks, etc.).
        Retries multiple times because some popups cascade (dismiss one → another appears).
        """
        for round_num in range(5):
            clicked = False
            for sel in POPUP_DISMISS_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        logger.info("正在关闭阻塞弹窗:  %s (round %d)", sel, round_num + 1)
                        await el.click()
                        await asyncio.sleep(2)
                        # Wait for page to stabilize after click
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break  # No more blockers found

    # ------------------------------------------------------------------
    # BMC_URL mode
    # ------------------------------------------------------------------
    async def _run_bmc_url(self, page, bmc_url: str, task, device, output_dir: str, result: ExecutionResult) -> None:
        """Navigate to target URL (may differ from login URL), screenshot, save HTML, evaluate rules."""
        target_url = self._resolve_url(task.command_or_url, device.bmc_ip)
        if target_url and target_url != bmc_url:
            self._validate_goto_url(target_url, device.bmc_ip)

            # Retry loop: dismiss blockers and re-navigate up to 3 times
            for attempt in range(3):
                logger.info("正在导航到目标:  %s (attempt %d)", target_url, attempt + 1)
                try:
                    await page.goto(target_url, wait_until="networkidle", timeout=self._page_timeout * 1000)
                except Exception:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=self._page_timeout * 1000)
                await asyncio.sleep(2)

                # Aggressively dismiss any blockers (password risk, popups, etc.)
                await self._dismiss_all_blockers(page)

                # Check if we reached the target
                blocker = await self._detect_target_blocker(page, target_url)
                if not blocker:
                    break  # Success
                logger.warning("[%s] 目标页面被阻塞 (attempt %d): %s", device.device_name, attempt + 1, blocker)
            else:
                # All retries exhausted
                raise Exception(
                    f"目标页面未到达(已重试3次): {blocker}。"
                    f"target_url={target_url} current_url={page.url}"
                )

        # File naming from template
        file_base = resolve_template(task.image_name_template, device, task)

        # Full-page screenshot
        ss_path = os.path.join(output_dir, f"{file_base}.png")
        await page.screenshot(path=ss_path, full_page=True)
        await self._save_raw_and_compose(ss_path, task, device.bmc_ip, page_url=page.url, result=result, page=page)

        result.screenshots = (ss_path,)
        result.step_results.append(StepResult(
            step_index=0,
            step_name="bmc_url_screenshot",
            status="SUCCESS",
            screenshot=ss_path,
            details=f"URL: {page.url}",
        ))

        # Save HTML
        html_content = await page.content()
        html_path = write_html_file(output_dir, f"{file_base}.html", html_content)
        result.html_file = html_path
        result.artifact_status = "ARTIFACT_SAVED"

        # Run rules (basic = blocking, advanced = validation only)
        await self._evaluate_rules(page, task, device, output_dir, result)

        # Evaluate evidence checkpoints (non-blocking, after artifacts saved)
        await self._evaluate_checkpoints(page, task, output_dir, result, ss_path)

    async def _evaluate_rules(self, page, task, device, output_dir: str, result: ExecutionResult) -> None:
        """Run task rules (basic first, then advanced) against the captured page."""
        rules = task.parsed_rules()
        if not rules:
            return

        from ..rules.engine import RuleEngine, RuleContext
        engine = RuleEngine()
        ctx = RuleContext(
            page=page, device=device, task=task, output_dir=output_dir,
        )

        eval_result = await engine.evaluate(list(rules), ctx)

        # Record basic rule results
        for r in eval_result.basic_results:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name=f"rule_basic_{r.action_type}",
                status="SUCCESS" if r.status == "PASS" else "FAILED",
                details=r.message,
            ))

        # Record advanced rule results
        for r in eval_result.advanced_results:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name=f"rule_advanced_{r.action_type}",
                status="SUCCESS" if r.status == "PASS" else "FAILED",
                details=r.message,
            ))

        if not eval_result.basic_passed:
            raise Exception(
                f"基础规则校验失败: {sum(1 for r in eval_result.basic_results if r.status != 'PASS')} 项未通过"
            )

        if not eval_result.advanced_passed:
            result.rule_status = "RULE_FAILED"
            result.rule_failure_reason = (
                f"高级规则校验失败: "
                f"{sum(1 for r in eval_result.advanced_results if r.status != 'PASS')} 项未通过"
            )
        else:
            result.rule_status = "RULE_PASSED" if eval_result.advanced_results else "RULE_DISABLED"

    async def _evaluate_checkpoints(
        self,
        page,
        task,
        output_dir: str,
        result: ExecutionResult,
        **_kwargs,  # tolerate legacy primary_screenshot kwarg
    ) -> None:
        """Evaluate evidence checkpoints (non-blocking, runs after artifacts are saved).

        Supports two formats from tasks.json:
          1. New: "evidence_checkpoints" → condition_evaluator.evaluate_evidence_checkpoints
          2. Legacy: "checkpoints" → CheckpointEngine (backward compat)
        """
        tdef = getattr(task, '_task_def', None) or {}

        # --- New format: evidence_checkpoints ---
        raw = tdef.get("evidence_checkpoints")
        if raw:
            artifacts = ArtifactContext.from_execution_result(result)
            # Extract page text for BMC tasks
            page_text = ""
            if page is not None:
                try:
                    if not page.is_closed():
                        page_text = await page.inner_text("body")
                except Exception:
                    pass
            artifacts.html_text = page_text  # Use page text as html_text for eval

            specs = parse_checkpoint_specs(raw)
            eval_result = evaluate_evidence_checkpoints(specs, artifacts, page_text)

            result.checkpoint_status = _checkpoint_rollup_from_condition(eval_result)

            for cr in eval_result.results:
                status = "SUCCESS" if cr.status == "PASS" else \
                         "FAILED" if cr.status == "FAIL" else \
                         "WARN" if cr.status == "WARN" else "SKIP"
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name=f"checkpoint_{cr.condition_type}",
                    status=status,
                    details=f"{cr.details} target={cr.target}" if cr.details else f"target={cr.target}",
                    step_type="checkpoint",
                ))
            return

        # --- Legacy format: checkpoints (CheckpointEngine) ---
        checkpoints_json = tdef.get("checkpoints")
        if not checkpoints_json:
            return

        from ..rules.checkpoint_engine import CheckpointEngine
        from ..rules.engine import RuleContext
        import json as _json

        try:
            specs = [CheckpointSpec.from_dict(c) for c in checkpoints_json]
        except Exception:
            logger.warning("Failed to parse checkpoints for task %s", task.task_name)
            return

        if not specs:
            return

        primary_ss = result.screenshots[-1] if result.screenshots else ""
        ctx = RuleContext(
            page=page,
            device=getattr(task, '_device', None),
            task=task,
            output_dir=output_dir,
        )
        ctx.artifacts["screenshot"] = primary_ss
        ctx.artifacts["html"] = result.html_file

        engine = CheckpointEngine()
        eval_result = await engine.evaluate(specs, ctx, evidence_ref=primary_ss)

        result.checkpoint_results = eval_result.results
        result.checkpoint_status = eval_result.rollup_status()

        for cp in eval_result.results:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name=f"checkpoint_{cp.checkpoint_name}",
                status="SUCCESS" if cp.status == "CHECK_PASS" else
                       "FAILED" if cp.status == "CHECK_FAIL" else
                       "WARN" if cp.status == "CHECK_WARN" else "SKIP",
                details=cp.details,
                step_type="checkpoint",
            ))

        # Serialize runtime variables
        if ctx.variables:
            import json as _json
            result.runtime_context = _json.dumps(ctx.variables, ensure_ascii=False)

    # ------------------------------------------------------------------
    # BMC_ACTIONS mode (DSL)
    # ------------------------------------------------------------------
    async def _run_bmc_actions(self, page, task, bmc_ip: str, output_dir: str, result: ExecutionResult) -> None:
        """Execute a sequence of DSL actions."""
        import json

        try:
            actions = json.loads(task.actions_json) if task.actions_json else []
        except json.JSONDecodeError:
            logger.error("Failed to parse BMC_ACTIONS JSON for task %s", task.task_name)
            result.execution_failure_reason = "BMC_ACTIONS JSON 解析失败"
            return

        if not isinstance(actions, list):
            actions = [actions]

        for i, action in enumerate(actions):
            action_type = action.get("action", action.get("type", ""))
            selector = action.get("selector", "")
            value = action.get("value", "")
            timeout_ms = int(action.get("timeout", self._page_timeout)) * 1000

            try:
                if action_type == "goto":
                    resolved = self._resolve_url(value, bmc_ip)
                    self._validate_goto_url(resolved, bmc_ip)
                    await page.goto(resolved, wait_until="networkidle", timeout=timeout_ms)
                elif action_type == "click":
                    await page.click(selector, timeout=timeout_ms)
                elif action_type == "fill":
                    await page.fill(selector, value, timeout=timeout_ms)
                elif action_type == "press":
                    await page.press(selector, value)
                elif action_type == "wait_for_selector":
                    await page.wait_for_selector(selector, timeout=timeout_ms)
                elif action_type == "wait":
                    await asyncio.sleep(float(value) if value else 1.0)
                elif action_type == "screenshot":
                    file_base = (task.image_name_template
                                 .replace("{device_ip}", device.bmc_ip)
                                 .replace("{device_name}", device.device_name)
                                 .replace("{task_name}", task.task_name)
                                 .replace("{task_sequence}", task.sequence_str or str(task.sequence)))
                    ss_path = os.path.join(output_dir, f"{file_base}.png")
                    await page.screenshot(path=ss_path, full_page=True)
                    result.screenshots = result.screenshots + (ss_path,)
                    result.step_results.append(StepResult(
                        step_index=i, step_name=action_type,
                        status="SUCCESS", screenshot=ss_path,
                    ))
                elif action_type == "save_html":
                    html = await page.content()
                    file_base = (task.image_name_template
                                 .replace("{device_ip}", device.bmc_ip)
                                 .replace("{device_name}", device.device_name)
                                 .replace("{task_name}", task.task_name)
                                 .replace("{task_sequence}", task.sequence_str or str(task.sequence)))
                    html_path = write_html_file(output_dir, f"{file_base}.html", html)
                    result.html_file = html_path
                elif action_type == "assert_visible":
                    el = await page.query_selector(selector)
                    if not el or not await el.is_visible():
                        raise AssertionError(f"Element not visible: {selector}")
                    result.step_results.append(StepResult(
                        step_index=i, step_name="assert_visible",
                        status="SUCCESS", details=f"Element visible: {selector}",
                    ))
                else:
                    logger.warning("Unknown BMC action: %s", action_type)

            except Exception as e:
                result.step_results.append(StepResult(
                    step_index=i, step_name=action_type,
                    status="FAILED", details=str(e),
                ))
                if action_type in ("screenshot", "save_html"):
                    raise  # Critical actions fail the task
                logger.warning("BMC action '%s' failed (non-critical): %s", action_type, e)

        # Run legacy checkpoints for old-format actions (if any)
        if result.execution_status != "EXEC_FAILED":
            await self._evaluate_checkpoints(page, task, output_dir, result,
                                              primary_screenshot=(result.screenshots[-1] if result.screenshots else ""))

    # ------------------------------------------------------------------
    # Unified BMC_CAPTURE_FLOW (replaces _run_bmc_url + _run_bmc_actions)
    # ------------------------------------------------------------------
    async def _run_capture_flow(self, page, task, device, bmc_ip: str, output_dir: str, result: ExecutionResult) -> None:
        """Unified capture pipeline for BMC_URL and BMC_ACTIONS.

        Pipeline: goto target_url → pre_capture_actions → ready_conditions
                  → final_capture (guaranteed) → rules → checkpoints.
        """
        flow = task.to_capture_flow()
        file_base = resolve_template(task.image_name_template, device, task)

        # --- Step 1: goto target_url ---
        raw_target = flow.get("target_url", "")
        if raw_target:
            target_url = self._resolve_url(raw_target, bmc_ip)
            self._validate_goto_url(target_url, bmc_ip)
            try:
                await page.goto(target_url, wait_until="domcontentloaded",
                                timeout=self._page_timeout * 1000)
            except Exception as e:
                logger.warning("[%s] goto target_url failed: %s", device.device_name, e)
                result.ready_status = "READY_NOT_READY"
                result.ready_failure_reason = f"goto target_url failed: {e}"
                # Do NOT return — continue to final_capture for debugging
        else:
            # No target_url from either command_or_url (BMC_URL) or actions_json goto (BMC_ACTIONS).
            execution_mode = getattr(task, "execution_mode", "unknown")
            logger.warning(
                "[%s] capture flow has no target_url — mode=%s, cmd_or_url=%r, actions_json=%r",
                device.device_name, execution_mode,
                getattr(task, "command_or_url", ""),
                getattr(task, "actions_json", "")[:120],
            )
            result.ready_status = "READY_NOT_READY"
            if not result.ready_failure_reason:
                result.ready_failure_reason = (
                    f"no target URL configured: execution_mode={execution_mode}, "
                    f"command_or_url is empty and actions_json has no goto"
                )

        await self._dismiss_all_blockers(page)

        # --- Step 2: pre_capture_actions ---
        pre_actions = flow.get("pre_capture_actions", [])
        if pre_actions:
            await self._execute_pre_capture_actions(
                page, pre_actions, device, output_dir, result,
            )

        # --- Step 3: capture_ready_conditions ---
        ready_eval = await self._evaluate_capture_ready_conditions(page, task, device)
        for cr in ready_eval.results:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name=f"ready_{cr.condition_type}",
                status="SUCCESS" if cr.is_pass else "FAILED",
                details=cr.details or f"{cr.target} → {cr.actual[:60]}",
            ))
        if ready_eval.rollup() == "FAIL":
            if result.ready_status != "READY_NOT_READY":
                result.ready_status = "READY_NOT_READY"
            if not result.ready_failure_reason:
                result.ready_failure_reason = f"ready conditions failed: {ready_eval.summary()}"

        # --- Step 4: final_capture (always runs if page is alive) ---
        await self._execute_final_capture(page, task, bmc_ip, file_base, output_dir, result)

        # --- Step 5: rules ---
        await self._evaluate_rules(page, task, device, output_dir, result)

        # --- Step 6: evidence_checkpoints ---
        await self._evaluate_checkpoints(page, task, output_dir, result)

    async def _execute_pre_capture_actions(self, page, actions: list, device, output_dir: str, result: ExecutionResult) -> None:
        """Execute pre_capture_actions: click/fill/press/wait/wait_for_selector.

        - required=True action failure → stop subsequent actions, set EXEC_PARTIAL
        - required=False action failure → log and continue
        - screenshot/save_html in actions → downgraded to intermediate only
        """
        for i, action in enumerate(actions):
            action_type = action.get("action") or action.get("type", "")
            selector = action.get("selector", "")
            value = action.get("value", "")
            timeout_ms = int(action.get("timeout_ms") or action.get("timeout") or 5000)
            required = action.get("required", True)
            description = action.get("description", "")

            try:
                if action_type == "click":
                    await page.locator(selector).first.click(timeout=timeout_ms)
                elif action_type == "fill":
                    await page.locator(selector).first.fill(value, timeout=timeout_ms)
                elif action_type == "press":
                    await page.locator(selector).first.press(value)
                elif action_type == "wait_for_selector":
                    await page.locator(selector).first.wait_for(timeout=timeout_ms)
                elif action_type in ("wait", "sleep"):
                    await asyncio.sleep(float(value) if value else 1.0)
                elif action_type == "intermediate_screenshot":
                    # Action-level screenshot: policy-controlled
                    if self._screenshot_policy in ("all", "checkpoints"):
                        ss_path = os.path.join(output_dir, f"intermediate_{i:02d}.png")
                        await page.screenshot(path=ss_path, full_page=True)
                        logger.debug("[%s] intermediate screenshot saved (policy=%s): %s",
                                   device.device_name, self._screenshot_policy, ss_path)
                    else:
                        logger.debug("[%s] intermediate screenshot skipped (policy=%s)", device.device_name,
                                   self._screenshot_policy)
                elif action_type == "goto":
                    pass  # Already handled as target_url in _run_capture_flow
                else:
                    logger.warning("[%s] Unknown pre_capture action: %s", device.device_name, action_type)

                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name=f"pre_{action_type}",
                    status="SUCCESS",
                    details=description or f"{action_type} {selector}".strip(),
                ))

            except Exception as e:
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name=f"pre_{action_type}",
                    status="FAILED",
                    details=f"{description or action_type}: {e}",
                ))
                logger.warning("[%s] pre_capture action[%d] '%s' failed: %s",
                               device.device_name, i, action_type, e)

                if required:
                    result.execution_status = "EXEC_PARTIAL"
                    result.ready_status = "READY_NOT_READY"
                    result.ready_failure_reason = (
                        f"required action failed at step {i}: "
                        f"{action_type} {selector}: {e}"
                    )
                    # Stop subsequent actions, but continue to final_capture
                    break
                # optional action failure → continue next action

    async def _evaluate_capture_ready_conditions(self, page, task, device) -> ConditionEvaluationResult:
        """Evaluate capture_ready_conditions from tasks.json (or defaults) against live page.

        BMC defaults: page_alive + not_login_page.
        SSH/TELNET: skip (no Playwright page).
        """
        raw = None
        if hasattr(task, '_task_def') and task._task_def:
            raw = task._task_def.get("capture_ready_conditions")

        specs = parse_ready_specs(raw)
        protocol = getattr(task, "task_type", "BMC") or "BMC"
        eval_result = await evaluate_ready_conditions(page, specs, protocol=protocol)

        # Record each condition as a step result for traceability
        for cr in eval_result.results:
            result = getattr(self, '_current_result', None)
            # Step recording is done in _run_capture_flow caller
            pass

        return eval_result

    async def _execute_final_capture(
        self,
        page,
        task,
        bmc_ip: str,
        file_base: str,
        output_dir: str,
        result: ExecutionResult,
    ) -> None:
        """Guaranteed final evidence: screenshot + HTML save.

        Runs regardless of prior failures, as long as page is alive.
        Sets artifact_status accordingly.
        """
        if page is None:
            result.artifact_status = "ARTIFACT_FAILED"
            if not result.artifact_failure_reason:
                result.artifact_failure_reason = "page is None at final_capture"
            return

        try:
            if page.is_closed():
                result.artifact_status = "ARTIFACT_FAILED"
                if not result.artifact_failure_reason:
                    result.artifact_failure_reason = "page closed before final_capture"
                return
        except Exception:
            pass

        errors = []

        # Final screenshot (content-aware)
        ss_path = ""
        try:
            ss_path = os.path.join(output_dir, f"{file_base}.png")
            await self._content_aware_screenshot(page, ss_path, task, result)
            await self._save_raw_and_compose(ss_path, task, bmc_ip, page_url=page.url, result=result, page=page)
            # Overwrite screenshots with final evidence only
            result.screenshots = (ss_path,)
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="final_screenshot",
                status="SUCCESS",
                screenshot=ss_path,
            ))
        except Exception as e:
            errors.append(f"screenshot: {e}")
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="final_screenshot",
                status="FAILED",
                details=str(e),
            ))

        # Final HTML
        try:
            html_content = await page.content()
            html_path = write_html_file(output_dir, f"{file_base}.html", html_content)
            result.html_file = html_path
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="final_save_html",
                status="SUCCESS",
            ))
        except Exception as e:
            errors.append(f"html: {e}")
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="final_save_html",
                status="FAILED",
                details=str(e),
            ))

        # evidence.html — rendered DOM with computed styles inlined (offline-viewable)
        try:
            evidence_html = await page.evaluate("""() => {
                const root = document.documentElement.cloneNode(true);
                // Remove all script tags (JS won't execute offline)
                for (const s of root.querySelectorAll('script')) { s.remove(); }
                // Remove event handlers
                for (const el of root.querySelectorAll('*')) {
                    for (const attr of [...el.attributes]) {
                        if (attr.name.startsWith('on')) { el.removeAttribute(attr.name); }
                    }
                }
                return '<!DOCTYPE html>\\n' + root.outerHTML;
            }""")
            evidence_path = os.path.join(output_dir, f"{file_base}.evidence.html")
            with open(evidence_path, "w", encoding="utf-8") as f:
                f.write(evidence_html)
            logger.info("evidence.html (DOM) saved: %s (%.1f KB)", evidence_path, len(evidence_html) / 1024)
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="final_save_evidence_html",
                status="SUCCESS",
                details=evidence_path,
            ))
        except Exception as e:
            logger.warning("evidence.html failed: %s", e)

        # MHTML — best-effort, not required for offline viewing
        try:
            cdp = await page.context.new_cdp_session(page)
            result_cdp = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_data = result_cdp.get("data", "")
            if mhtml_data and len(mhtml_data) > 100:
                mhtml_path = os.path.join(output_dir, f"{file_base}.mhtml")
                with open(mhtml_path, "wb") as f:
                    f.write(mhtml_data.encode("utf-8", errors="replace"))
                logger.info("MHTML saved (best-effort): %s (%.1f MB)", mhtml_path, len(mhtml_data) / 1_048_576)
        except Exception as e:
            logger.debug("MHTML skipped (best-effort): %s", e)

        # Set artifact_status
        if not errors:
            result.artifact_status = "ARTIFACT_SAVED"
        elif ss_path or result.html_file:
            result.artifact_status = "ARTIFACT_PARTIAL"
            result.artifact_failure_reason = "; ".join(errors)
        else:
            result.artifact_status = "ARTIFACT_FAILED"
            result.artifact_failure_reason = "; ".join(errors)

    async def _content_aware_screenshot(self, page, ss_path, task, result) -> None:
        """Take a content-aware BMC screenshot: detect scroll container, avoid blank areas.

        Strategy depends on full_screenshot and screenshot_mode task fields.
        """
        full_ss = getattr(task, "full_screenshot", False)
        ss_mode = getattr(task, "screenshot_mode", "auto") or "auto"

        # Detect content boundaries via JS
        content_info = await page.evaluate("""() => {
            const info = { docScrollH: 0, bodyScrollH: 0, scrollContainer: null,
                          scrollH: 0, clientH: 0, bottomVisible: 0, viewportH: window.innerHeight };

            info.docScrollH = document.documentElement.scrollHeight;
            info.bodyScrollH = document.body.scrollHeight;

            // Find internal scroll container (Element UI / Vue SPA)
            const candidates = document.querySelectorAll(
                '.el-scrollbar__wrap, .el-main, .main-content, .content-wrapper, .page-content, [style*="overflow"]'
            );
            let best = null, bestArea = 0;
            for (const el of candidates) {
                const style = getComputedStyle(el);
                if (style.overflowY === 'auto' || style.overflowY === 'scroll' || style.overflowY === 'overlay') {
                    const area = el.clientWidth * el.clientHeight;
                    if (el.scrollHeight > el.clientHeight + 20 && area > bestArea) {
                        best = el; bestArea = area;
                    }
                }
            }
            if (best) {
                const tag = best.tagName.toLowerCase();
                const cls = (best.className || '').toString().substring(0, 80);
                info.scrollContainer = (best.id ? '#' + best.id : tag + (cls ? '.' + cls.split(' ')[0] : ''));
                info.scrollH = best.scrollHeight;
                info.clientH = best.clientHeight;
            }

            // Find bottommost visible element
            const all = document.querySelectorAll('*');
            let maxBottom = 0;
            for (const el of all) {
                const rect = el.getBoundingClientRect();
                if (rect.bottom > maxBottom && rect.width > 0 && rect.height > 0 &&
                    rect.bottom < 50000 && rect.top < 50000) {
                    maxBottom = rect.bottom;
                }
            }
            info.bottomVisible = Math.ceil(maxBottom + 24); // small padding
            return info;
        }""")

        # Determine target height
        viewport_h = content_info.get("viewportH", 900)
        bottom_visible = content_info.get("bottomVisible", viewport_h)
        scroll_h = content_info.get("scrollH", 0)
        doc_scroll_h = content_info.get("docScrollH", 0)

        if ss_mode == "viewport":
            target_h = viewport_h
        elif ss_mode == "full_page" or full_ss:
            target_h = max(doc_scroll_h, scroll_h, bottom_visible)
        elif ss_mode == "content":
            target_h = scroll_h if scroll_h > viewport_h else max(bottom_visible, viewport_h)
        else:  # auto
            # Prefer internal scroll container if found; otherwise crop to content
            if scroll_h > viewport_h + 50:
                target_h = scroll_h
            else:
                target_h = max(bottom_visible, viewport_h)

        # Cap
        max_h = 20000
        if target_h > max_h:
            target_h = max_h
            logger.warning("Screenshot height capped at %d px (actual may be taller)", max_h)

        # Take screenshot with calculated clip
        await page.set_viewport_size({"width": page.viewport_size["width"], "height": target_h})
        await page.wait_for_timeout(300)  # let layout settle
        await page.screenshot(path=ss_path, full_page=True)
        await page.set_viewport_size({"width": page.viewport_size["width"], "height": viewport_h})

        # Apply blank crop from bottom using PIL
        try:
            import io
            from PIL import Image
            img = Image.open(ss_path).convert("RGB")
            w, h = img.size
            # Scan from bottom: find first non-blank row
            blank_limit = int(h * 0.90)  # don't crop more than 10%
            crop_y = h
            for y in range(h - 1, blank_limit, -1):
                row_colors = set()
                for x in range(0, w, max(1, w // 20)):
                    px = img.getpixel((x, y))
                    row_colors.add(px)
                # Row is blank if all sampled pixels are very similar light/white
                if len(row_colors) > 2:
                    crop_y = y + 40  # keep small padding
                    break
                elif len(row_colors) == 1:
                    r, g, b = list(row_colors)[0][:3]
                    if r < 230 or g < 230 or b < 230:
                        crop_y = y + 40
                        break
            if crop_y < h:
                img = img.crop((0, 0, w, crop_y))
                img.save(ss_path, "PNG")
                logger.info("Blank crop: %d px removed (%d → %d)", h - crop_y, h, crop_y)
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name="bmc_blank_crop",
                    status="SUCCESS",
                    details=f"removed {h - crop_y} blank px",
                ))
        except Exception as e:
            logger.debug("Blank crop skipped: %s", e)

    async def _save_raw_and_compose(
        self,
        screenshot_path: str,
        task,
        bmc_ip: str,
        page_url: str = "",
        result: ExecutionResult | None = None,
        page=None,
    ) -> None:
        """Save raw screenshot to raw/ then composite address bar in place."""
        # 1. Save raw
        raw_dir = os.path.join(os.path.dirname(screenshot_path), "raw")
        os.makedirs(raw_dir, exist_ok=True)
        raw_path = os.path.join(raw_dir, os.path.basename(screenshot_path))
        shutil.copy2(screenshot_path, raw_path)

        if result is not None:
            result.raw_screenshots = tuple(result.raw_screenshots or ()) + (raw_path,)

        # 2. Composite address bar via existing browser
        try:
            # Record pre-compose size for diagnostics
            raw_img = Image.open(screenshot_path)
            raw_w, raw_h = raw_img.size
            raw_img.close()
            logger.info("Address bar compose starting: raw_size=%dx%d", raw_w, raw_h)

            await self._compose_addressbar_async(
                screenshot_path, task, bmc_ip, page_url=page_url, result=result, page=page,
            )

            # Verify compose succeeded
            final_img = Image.open(screenshot_path)
            final_w, final_h = final_img.size
            final_img.close()
            added = final_h - raw_h
            if added <= 0:
                logger.error("Address bar compose had no effect: height unchanged (%d → %d)", raw_h, final_h)
                if result is not None:
                    result.step_results.append(StepResult(
                        step_index=len(result.step_results),
                        step_name="addressbar_compose",
                        status="FAILED",
                        details=f"height unchanged ({raw_h} → {final_h})",
                    ))
            else:
                logger.info("Address bar compose success: %dx%d → %dx%d (+%dpx)", raw_w, raw_h, final_w, final_h, added)
        except Exception:
            logger.exception(
                "Address bar composite failed for %s — raw screenshot preserved at %s",
                screenshot_path, raw_path,
            )
            if result is not None:
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name="addressbar_compose",
                    status="FAILED",
                    details=f"exception during compose",
                ))

    async def _compose_addressbar_async(
        self, screenshot_path, task, bmc_ip, page_url="", result=None, page=None,
    ) -> None:
        """Composite final SVG address bar using the existing browser page.

        The final SVGs are full-page templates (1920x1080 etc). The address bar
        occupies the top ~96px of the design. We render the SVG at the screenshot
        width, then clip only the address bar portion.
        """
        from ..out.addressbar import _select_svg_template, _svg_template_ratio_name

        address_url = page_url or (f"https://{bmc_ip}" if bmc_ip else "about:blank")
        tab_title = f"iBMC {bmc_ip}" if bmc_ip else "iBMC"

        image = Image.open(screenshot_path).convert("RGB")
        w, h = image.size

        # Address bar design height = 96px within 1080px viewBox → scale by width
        bar_design_h = 96  # SVG design: address bar is y=0..96 in viewBox
        svg_viewbox_w = 1920
        bar_height = max(64, min(108, round(bar_design_h * w / svg_viewbox_w)))

        # Select SVG and read its content
        svg_path = _select_svg_template(w, h)
        if not svg_path.exists():
            raise FileNotFoundError(f"SVG template not found: {svg_path}")
        with open(str(svg_path), "r", encoding="utf-8") as f:
            svg_content = f.read()

        # Render SVG via set_content with proper viewport and styling
        svg_page = await page.context.new_page()
        try:
            await svg_page.set_viewport_size({"width": w, "height": bar_height})
            await svg_page.set_content(
                f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                f"html,body{{margin:0;padding:0;width:{w}px;height:{bar_height}px;overflow:hidden;}}"
                f"svg{{display:block;width:{w}px;height:auto;}}"
                f"</style></head><body>{svg_content}</body></html>",
                wait_until="load",
            )
            await svg_page.wait_for_selector("svg", timeout=5000)
            svg_png = await svg_page.screenshot(
                clip={"x": 0, "y": 0, "width": w, "height": bar_height},
            )
            bar_img = Image.open(io.BytesIO(svg_png)).convert("RGB")

            if bar_img.width < 10 or bar_img.height < 10:
                raise RuntimeError(f"Rendered bar image too small: {bar_img.size}")
        finally:
            await svg_page.close()

        # Composite: address bar on top, screenshot below
        output = Image.new("RGB", (w, h + bar_img.height), (255, 255, 255))
        output.paste(bar_img, (0, 0))
        output.paste(image, (0, bar_img.height))
        output.save(screenshot_path, "PNG")

        if result is not None:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="addressbar_composite",
                status="SUCCESS",
                screenshot=screenshot_path,
                details=f"tab={tab_title}; url={address_url}; source=final_svg; template={svg_path.name}; ratio={_svg_template_ratio_name(svg_path)}",
            ))

    def _compose_addressbar(
        self,
        screenshot_path: str,
        task,
        bmc_ip: str,
        page_url: str = "",
        result: ExecutionResult | None = None,
    ) -> None:
        """Add an address bar to BMC evidence screenshots in place.

        Tab title: iBMC {bmc_ip}
        Address URL: actual page.url (page_url parameter)
        """
        address_url = page_url or ""
        if not address_url:
            address_url = f"https://{bmc_ip}" if bmc_ip else "about:blank"
            logger.warning("page_url is empty, using fallback address URL: %s", address_url)

        tab_title = f"iBMC {bmc_ip}" if bmc_ip else "iBMC"
        if not bmc_ip:
            logger.warning("bmc_ip is empty, address bar tab title will have no IP")

        try:
            meta = render_final_addressbar(
                screenshot_path,
                screenshot_path,
                address_url,
                title=tab_title,
            )
            if result is not None:
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name="addressbar_composite",
                    status="SUCCESS",
                    screenshot=screenshot_path,
                    details=(
                        f"tab={tab_title}; url={address_url}; "
                        f"source={meta['addressbar_source']}; "
                        f"template={meta['addressbar_template']}; "
                        f"ratio={meta['addressbar_ratio']}"
                    ),
                ))
        except Exception as e:
            logger.warning("Failed to composite BMC address bar for %s: %s", screenshot_path, e)
            if result is not None:
                result.step_results.append(StepResult(
                    step_index=len(result.step_results),
                    step_name="addressbar_composite",
                    status="FAILED",
                    screenshot=screenshot_path,
                    details=str(e),
                ))

    def _resolve_addressbar_url(self, task, bmc_ip: str, page_url: str = "") -> str:
        """Prefer explicit task target, then trusted current page URL."""
        flow = task.to_capture_flow() if hasattr(task, "to_capture_flow") else {}
        target_url = normalize_bmc_addressbar_url(flow.get("target_url", ""), bmc_ip)
        if target_url:
            return target_url
        return normalize_bmc_addressbar_url(page_url, bmc_ip)

    # --- Deprecated: kept for reference, not called by new pipeline ---
    async def _deprecated_run_bmc_url(self, *args, **kwargs):
        """Deprecated. Replaced by _run_capture_flow."""
        return await self._run_bmc_url(*args, **kwargs)

    async def _deprecated_run_bmc_actions(self, *args, **kwargs):
        """Deprecated. Replaced by _run_capture_flow."""
        return await self._run_bmc_actions(*args, **kwargs)

    def _resolve_var(self, template: str, variables: dict) -> str:
        """Replace {{var.X}} placeholders with extracted variable values."""
        import re
        def _replace(m):
            key = m.group(1)
            return variables.get(key, m.group(0))
        return re.sub(r'\{\{var\.(\w+)\}\}', _replace, template)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _find_visible(self, page, selectors: list[str]):
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def _resolve_url(self, raw: str, bmc_ip: str) -> str:
        raw = raw.strip()
        if not raw:
            # Base URL for login / root access — valid for login stage, not for capture target.
            return f"https://{bmc_ip}"
        raw = raw.replace("{带外管理IP}", bmc_ip)
        raw = raw.replace("{bmc_ip}", bmc_ip)
        if raw.startswith("/"):
            return f"https://{bmc_ip}{raw}"
        if not raw.startswith("http"):
            return f"https://{bmc_ip}{raw}"
        return raw

    def _validate_goto_url(self, url: str, bmc_ip: str) -> None:
        """Raise ValueError if URL host does not match the device BMC IP."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        expected = bmc_ip.lower()
        if host != expected:
            raise ValueError(
                f"URL host mismatch: expected {expected}, got {host!r}. "
                f"Rendered URL: {url}. "
                f"Check _resolve_url bmc_ip parameter."
            )

    def _build_output_dir(self, root: str, device, task) -> str:
        tmpl = task.output_dir_template
        resolved = resolve_template(tmpl, device, task)
        unreplaced = check_unreplaced_vars(resolved)
        if unreplaced:
            logger.warning(f"BMC output_dir_template 残留未替换变量: {unreplaced} in '{tmpl}'")
        return os.path.join(root, resolved)

    def _build_log(self, result: ExecutionResult) -> str:
        lines = [
            f"Plan ID: {result.plan_id}",
            f"Device: {result.device_name} ({result.device_group})",
            f"BMC IP: {result.bmc_ip}",
            f"Task: {result.task_name}  Type: {result.task_type}  Mode: {result.execution_mode}",
            f"Status: {result.execution_status}",
            f"Duration: {result.duration_seconds:.1f}s",
        ]
        if result.execution_failure_reason:
            lines.append(f"Failure: {result.execution_failure_reason}")
        for s in result.step_results:
            lines.append(f"  Step {s.step_index} [{s.status}] {s.step_name}: {s.details}")
        return "\n".join(lines)
