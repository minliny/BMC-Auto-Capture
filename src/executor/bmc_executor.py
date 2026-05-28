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
import logging
import os
import time

from .base import AbstractExecutor
from .browser_manager import BrowserManager
from .captcha_handler import detect_captcha, handle_captcha, CaptchaDetected
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult, StepResult
from ..models.checkpoint import CheckpointSpec
from ..out.file_writer import write_html_file, write_log_file

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


def _resolve_template(tmpl: str, device, task) -> str:
    """Replace Excel header name variables with actual values."""
    seq = task.sequence_str or str(task.sequence)
    return (tmpl
            .replace("{任务序号}", seq)
            .replace("{任务名称}", task.task_name)
            .replace("{任务类型}", task.task_type)
            .replace("{设备分类}", device.device_group)
            .replace("{设备名称}", device.device_name)
            .replace("{设备型号}", device.device_model)
            .replace("{带外管理IP}", device.bmc_ip)
            .replace("{带外管理用户名}", device.bmc_username)
            .replace("{带外管理密码}", device.bmc_password)
            .replace("{带内管理IP}", device.inband_ip)
            .replace("{带内管理用户名}", device.inband_username)
            .replace("{带内管理密码}", device.inband_password))


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
    ):
        self._bm = browser_manager
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

            if task.execution_mode == "BMC_URL":
                await self._run_bmc_url(page, bmc_url, task, device, output_dir, result)
            elif task.execution_mode == "BMC_ACTIONS":
                await self._run_bmc_actions(page, task, device.bmc_ip, output_dir, result)
            logger.info("[%s] 阶段 5/6: 采集完成", dname)

            # --- Stage 6: success ---
            current_stage = "6/6 done"
            logger.info("[%s] Stage %s", dname, current_stage)
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

        file_base = _resolve_template(task.image_name_template, device, task)
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
        file_base = _resolve_template(task.image_name_template, device, task)

        # Full-page screenshot
        ss_path = os.path.join(output_dir, f"{file_base}.png")
        await page.screenshot(path=ss_path, full_page=True)

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
        primary_screenshot: str = "",
    ) -> None:
        """Evaluate evidence checkpoints (non-blocking, runs after artifacts are saved)."""
        from ..rules.checkpoint_engine import CheckpointEngine
        from ..rules.engine import RuleContext
        import json

        # Load checkpoints from tasks.json
        checkpoints_json = None
        if hasattr(task, '_task_def') and task._task_def:
            checkpoints_json = task._task_def.get("checkpoints")
        if not checkpoints_json:
            return

        try:
            specs = [CheckpointSpec.from_dict(c) for c in checkpoints_json]
        except Exception:
            logger.warning("Failed to parse checkpoints for task %s", task.task_name)
            return

        if not specs:
            return

        ctx = RuleContext(
            page=page,
            device=getattr(task, '_device', None),
            task=task,
            output_dir=output_dir,
        )
        ctx.artifacts["screenshot"] = primary_screenshot
        ctx.artifacts["html"] = result.html_file

        engine = CheckpointEngine()
        eval_result = await engine.evaluate(specs, ctx, evidence_ref=primary_screenshot)

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
            # For BMC_ACTIONS tasks with no URL, use the BMC root
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
        return os.path.join(root, _resolve_template(task.output_dir_template, device, task))

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
