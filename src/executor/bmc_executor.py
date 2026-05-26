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
from typing import Optional

from .base import AbstractExecutor
from .browser_manager import BrowserManager
from .captcha_handler import detect_captcha, handle_captcha, CaptchaDetected
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult, StepResult
from ..out.file_writer import write_html_file, write_log_file
from ..out.screenshot import overlay_device_info

logger = logging.getLogger("bmc_auto_capture.bmc")

# --- Common post-login popup patterns ---
POPUP_DISMISS_SELECTORS = [
    # "Password expiring" / "Password insecure" warnings
    'button:has-text("暂不修改")',
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


class BMCLoginError(Exception):
    pass


class BMCExecutor(AbstractExecutor):
    """Execute BMC_URL and BMC_ACTIONS tasks."""

    def __init__(
        self,
        browser_manager: BrowserManager,
        connect_timeout: float = 30.0,
        page_timeout: float = 60.0,
    ):
        self._bm = browser_manager
        self._connect_timeout = connect_timeout
        self._page_timeout = page_timeout
        self._loop: asyncio.AbstractEventLoop | None = None

    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        loop = self._loop or asyncio.new_event_loop()
        self._loop = loop
        try:
            return loop.run_until_complete(self._execute_async(plan, output_root))
        finally:
            if self._loop is None:
                loop.close()

    async def _execute_async(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        device = plan.device
        task = plan.task

        result = ExecutionResult(
            plan_id=plan.plan_id,
            device_name=device.device_name,
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

        try:
            context = await self._bm.get_context()
            page = await context.new_page()
            page.set_default_timeout(self._page_timeout * 1000)

            # --- Build BMC URL ---
            bmc_url = self._resolve_url(task.command_or_url, device.bmc_ip)

            # --- Login flow ---
            login_ok = await self._bmc_login(page, bmc_url, device)
            if not login_ok:
                result.execution_status = "EXEC_FAILED"
                result.execution_failure_reason = "BMC登录失败"
                result.ended_at = time.time()
                result.duration_seconds = result.ended_at - result.started_at
                await page.close()
                return result

            # --- Dismiss post-login popups ---
            await self._dismiss_popups(page)

            # --- Navigate & capture ---
            if task.execution_mode == "BMC_URL":
                await self._run_bmc_url(page, bmc_url, task, output_dir, result)
            elif task.execution_mode == "BMC_ACTIONS":
                await self._run_bmc_actions(page, task, output_dir, result)

            await page.close()
            result.execution_status = "EXEC_SUCCESS"

        except Exception as e:
            result.execution_status = "EXEC_ERROR"
            result.execution_failure_reason = str(e)
            logger.error(f"[{device.device_name}] BMC error: {e}")

        result.ended_at = time.time()
        result.duration_seconds = result.ended_at - result.started_at

        log_path = write_log_file(output_dir, "task.log", self._build_log(result))
        result.log_file = log_path

        return result

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def _bmc_login(self, page, bmc_url: str, device) -> bool:
        """Navigate to BMC, detect login page, fill credentials, submit."""
        logger.info(f"[{device.device_name}] Navigating to BMC: {bmc_url}")

        try:
            await page.goto(bmc_url, wait_until="domcontentloaded", timeout=self._connect_timeout * 1000)
        except Exception as e:
            logger.error(f"[{device.device_name}] Failed to reach BMC page: {e}")
            return False

        await asyncio.sleep(2)  # Allow redirect to login page

        # Check for CAPTCHA before login
        captcha_seen = await detect_captcha(page)
        if captcha_seen and not self._bm.headless:
            solved = await handle_captcha(page, os.path.dirname(page.url), timeout=120)
            if not solved:
                return False

        # Find login form elements
        username_el = await self._find_visible(page, LOGIN_USERNAME_SELECTORS)
        password_el = await self._find_visible(page, LOGIN_PASSWORD_SELECTORS)

        if username_el and password_el:
            logger.info(f"[{device.device_name}] Login form detected, filling credentials")
            await username_el.fill(device.bmc_username)
            await password_el.fill(device.bmc_password)

            # Check for CAPTCHA after filling (some sites show it after username entry)
            captcha_seen = await detect_captcha(page)
            if captcha_seen:
                if self._bm.headless:
                    logger.error("CAPTCHA detected in headless mode — cannot proceed")
                    return False
                solved = await handle_captcha(page, os.path.dirname(page.url), timeout=120)
                if not solved:
                    return False

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

        return True

    async def _dismiss_popups(self, page) -> None:
        """Try to dismiss common post-login popups."""
        for _ in range(5):  # max 5 popups
            dismissed = False
            for sel in POPUP_DISMISS_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        logger.info("Dismissing popup: %s", sel)
                        await el.click()
                        await asyncio.sleep(1)
                        dismissed = True
                        break
                except Exception:
                    continue
            if not dismissed:
                break

    # ------------------------------------------------------------------
    # BMC_URL mode
    # ------------------------------------------------------------------
    async def _run_bmc_url(self, page, bmc_url: str, task, output_dir: str, result: ExecutionResult) -> None:
        """Navigate to target URL (may differ from login URL), screenshot, save HTML."""
        target_url = self._resolve_url(task.command_or_url, "")
        if target_url and target_url != bmc_url:
            logger.info("Navigating to target: %s", target_url)
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=self._page_timeout * 1000)
            except Exception:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=self._page_timeout * 1000)
            await asyncio.sleep(2)

        # Full-page screenshot
        ss_filename = task.image_name_template.replace("{timestamp}", time.strftime("%Y%m%d_%H%M%S"))
        if not ss_filename.endswith(".png"):
            ss_filename += ".png"
        ss_path = os.path.join(output_dir, ss_filename)
        await page.screenshot(path=ss_path, full_page=True)

        # Add overlay
        page_url = page.url
        page_title = await page.title()
        annotated = overlay_device_info(
            ss_path,
            device_name=result.device_name,
            device_ip=result.bmc_ip,
            task_name=result.task_name,
            page_url=page_url,
            page_title=page_title,
        )

        result.screenshots = (annotated,)
        result.step_results.append(StepResult(
            step_index=0,
            step_name="bmc_url_screenshot",
            status="SUCCESS",
            screenshot=annotated,
            details=f"URL: {page_url}",
        ))

        # Save HTML
        html_content = await page.content()
        html_path = write_html_file(output_dir, "page.html", html_content)
        result.html_file = html_path

    # ------------------------------------------------------------------
    # BMC_ACTIONS mode (DSL)
    # ------------------------------------------------------------------
    async def _run_bmc_actions(self, page, task, output_dir: str, result: ExecutionResult) -> None:
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
                    await page.goto(value, wait_until="networkidle", timeout=timeout_ms)
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
                    ss_path = os.path.join(output_dir, f"step_{i:03d}.png")
                    await page.screenshot(path=ss_path, full_page=True)
                    result.screenshots = result.screenshots + (ss_path,)
                    result.step_results.append(StepResult(
                        step_index=i, step_name=action_type,
                        status="SUCCESS", screenshot=ss_path,
                    ))
                elif action_type == "save_html":
                    html = await page.content()
                    html_path = write_html_file(output_dir, f"step_{i:03d}.html", html)
                    if not result.html_file:
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

    def _build_output_dir(self, root: str, device, task) -> str:
        tmpl = task.output_dir_template
        tmpl = tmpl.replace("{device_name}", device.device_name)
        tmpl = tmpl.replace("{device_group}", device.device_group)
        tmpl = tmpl.replace("{task_name}", task.task_name)
        tmpl = tmpl.replace("{task_type}", task.task_type)
        return os.path.join(root, tmpl)

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
