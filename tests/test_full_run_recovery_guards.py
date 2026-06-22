from __future__ import annotations

import time
import asyncio
import threading
from pathlib import Path

from src.app import App
from src.executor.bmc_executor import BMCExecutor
from src.executor.bmc_health_check import HealthResult
from src.executor.retry import execute_with_retry
from src.models.app_config import AppConfig
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.out import console
from src.scheduler.bmc_session_runner import BMCEndpointSessionRunner


class _FakePage:
    def __init__(self, name="page"):
        self.name = name
        self.closed = False
        self.default_timeout = None
        self.goto_calls = []
        self.reload_calls = 0

    def set_default_timeout(self, value):
        self.default_timeout = value

    async def close(self):
        self.closed = True

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        if timeout is not None and timeout < 20000:
            raise TimeoutError("Page.goto Timeout 5000ms exceeded")

    async def reload(self, wait_until=None, timeout=None):
        self.reload_calls += 1


class _FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = _FakePage(f"page-{len(self.pages) + 1}")
        self.pages.append(page)
        return page


class _FakeBrowserManager:
    headless = True

    def __init__(self):
        self.context = _FakeContext()

    async def get_context(self):
        return self.context


def _bmc_plan(name: str, sequence: int = 1, bmc_ip: str = "192.0.2.10") -> TaskPlan:
    return TaskPlan(
        plan_id=f"plan-{sequence}",
        device=Device(
            sequence,
            "redacted-device",
            "A3",
            bmc_ip,
            "",
            "",
        ),
        task=Task(
            sequence,
            sequence,
            name,
            "BMC",
            "BMC_URL",
            command_or_url="/UI/Static/#/test",
            timeout_seconds=5,
            enabled=True,
        ),
    )


async def _login_ok(self, page, device, bmc_url):
    return True, ""


async def _logout_noop(self, page, device):
    return None


async def _capture_success(self, page, task, device, bmc_ip, output_dir, result):
    result.execution_status = "EXEC_SUCCESS"


def test_bmc_session_expired_replaces_page_and_retries_current_task(monkeypatch, tmp_path):
    bm = _FakeBrowserManager()
    plans = [_bmc_plan("ok", 1), _bmc_plan("needs-relogin", 2)]
    health_calls = []

    async def health(page, stage, target_url=""):
        health_calls.append((page.name, stage))
        hr = HealthResult(stage)
        if stage == "before_plan" and len([c for c in health_calls if c[1] == "before_plan"]) == 2:
            hr.healthy = False
            hr.status = "BMC_SESSION_EXPIRED"
            hr.details = "BMC_SESSION_EXPIRED: redacted"
            hr.recoverable = True
        return hr

    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_login", _login_ok)
    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_logout", _logout_noop)
    monkeypatch.setattr("src.scheduler.bmc_session_runner.check_bmc_page_health", health)
    monkeypatch.setattr(BMCExecutor, "_run_capture_flow", _capture_success)
    monkeypatch.setattr(BMCExecutor, "_build_output_dir", lambda self, root, device, task: str(tmp_path / task.task_name))

    results = BMCEndpointSessionRunner(
        bm, plans[0].endpoint_key, plans, str(tmp_path),
    ).run()

    assert [r.execution_status for r in results] == ["EXEC_SUCCESS", "EXEC_SUCCESS"]
    assert len(bm.context.pages) == 2
    assert bm.context.pages[0].closed is True


def test_bmc_session_relogin_failure_only_fails_current_task(monkeypatch, tmp_path):
    bm = _FakeBrowserManager()
    plans = [_bmc_plan("ok", 1), _bmc_plan("relogin-fails", 2), _bmc_plan("continues", 3)]
    login_calls = []
    before_plan_count = 0

    async def login(self, page, device, bmc_url):
        login_calls.append(page.name)
        return (len(login_calls) != 2), "relogin failed"

    async def health(page, stage, target_url=""):
        nonlocal before_plan_count
        hr = HealthResult(stage)
        if stage == "before_plan":
            before_plan_count += 1
        if stage == "before_plan" and before_plan_count == 2:
            hr.healthy = False
            hr.status = "BMC_SESSION_EXPIRED"
            hr.details = "BMC_SESSION_EXPIRED: redacted"
            hr.recoverable = True
        return hr

    calls = []

    async def capture(self, page, task, device, bmc_ip, output_dir, result):
        calls.append(task.task_name)
        result.execution_status = "EXEC_SUCCESS"

    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_login", login)
    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_logout", _logout_noop)
    monkeypatch.setattr("src.scheduler.bmc_session_runner.check_bmc_page_health", health)
    monkeypatch.setattr(BMCExecutor, "_run_capture_flow", capture)
    monkeypatch.setattr(BMCExecutor, "_build_output_dir", lambda self, root, device, task: str(tmp_path / task.task_name))

    results = BMCEndpointSessionRunner(
        bm, plans[0].endpoint_key, plans, str(tmp_path),
    ).run()

    assert len(results) == 3
    assert results[0].execution_status == "EXEC_SUCCESS"
    assert results[1].execution_status == "EXEC_FAILED"
    assert results[2].execution_status == "EXEC_SUCCESS"
    assert calls == ["ok", "continues"]


def test_bmc_timeout_dialog_result_discards_old_page_and_retries_current_task(monkeypatch, tmp_path):
    bm = _FakeBrowserManager()
    plans = [_bmc_plan("dialog-timeout", 1)]
    login_calls = []
    capture_calls = []

    async def login(self, page, device, bmc_url):
        login_calls.append(page.name)
        return True, ""

    async def health(page, stage, target_url=""):
        return HealthResult(stage)

    async def capture(self, page, task, device, bmc_ip, output_dir, result):
        capture_calls.append(page.name)
        if len(capture_calls) == 1:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = (
                "BMC_PAGE_HEALTH_FAILED [BMC_TIMEOUT_DIALOG]: custom-dialog timeout"
            )
            return
        await page.goto(f"https://{device.bmc_ip}/UI/Static/#/test", timeout=20000)
        result.execution_status = "EXEC_SUCCESS"

    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_login", login)
    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_logout", _logout_noop)
    monkeypatch.setattr("src.scheduler.bmc_session_runner.check_bmc_page_health", health)
    monkeypatch.setattr(BMCExecutor, "_run_capture_flow", capture)
    monkeypatch.setattr(BMCExecutor, "_build_output_dir", lambda self, root, device, task: str(tmp_path / task.task_name))

    results = BMCEndpointSessionRunner(
        bm, plans[0].endpoint_key, plans, str(tmp_path),
    ).run()

    assert [r.execution_status for r in results] == ["EXEC_SUCCESS"]
    assert capture_calls == ["page-1", "page-2"]
    assert login_calls == ["page-1", "page-2"]
    assert bm.context.pages[0].closed is True
    assert getattr(bm.context.pages[0], "_bmc_invalid", False) is True
    assert bm.context.pages[1].goto_calls[-1][0].endswith("/UI/Static/#/test")


def test_one_endpoint_session_expired_does_not_affect_other_endpoint(monkeypatch, tmp_path):
    bad_bm = _FakeBrowserManager()
    good_bm = _FakeBrowserManager()
    bad_plan = _bmc_plan("bad-session", 1, bmc_ip="192.0.2.10")
    good_plan = _bmc_plan("good-session", 2, bmc_ip="192.0.2.11")
    login_counts = {}

    async def login(self, page, device, bmc_url):
        count = login_counts.get(device.bmc_ip, 0) + 1
        login_counts[device.bmc_ip] = count
        if device.bmc_ip == "192.0.2.10" and count >= 2:
            return False, "relogin failed"
        return True, ""

    async def health(page, stage, target_url=""):
        return HealthResult(stage)

    async def capture(self, page, task, device, bmc_ip, output_dir, result):
        if device.bmc_ip == "192.0.2.10":
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = (
                "BMC_PAGE_HEALTH_FAILED [BMC_TIMEOUT_DIALOG]: custom-dialog timeout"
            )
            return
        result.execution_status = "EXEC_SUCCESS"

    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_login", login)
    monkeypatch.setattr(BMCEndpointSessionRunner, "_do_logout", _logout_noop)
    monkeypatch.setattr("src.scheduler.bmc_session_runner.check_bmc_page_health", health)
    monkeypatch.setattr(BMCExecutor, "_run_capture_flow", capture)
    monkeypatch.setattr(BMCExecutor, "_build_output_dir", lambda self, root, device, task: str(tmp_path / device.bmc_ip / task.task_name))

    bad_results = BMCEndpointSessionRunner(
        bad_bm, bad_plan.endpoint_key, [bad_plan], str(tmp_path),
    ).run()
    good_results = BMCEndpointSessionRunner(
        good_bm, good_plan.endpoint_key, [good_plan], str(tmp_path),
    ).run()

    assert [r.execution_status for r in bad_results] == ["EXEC_FAILED"]
    assert [r.execution_status for r in good_results] == ["EXEC_SUCCESS"]
    assert login_counts["192.0.2.10"] == 2
    assert login_counts["192.0.2.11"] == 1


def test_bmc_login_goto_uses_page_timeout_not_fixed_5s(monkeypatch):
    page = _FakePage()
    executor = BMCExecutor(None, connect_timeout=5, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")

    async def false(*args, **kwargs):
        return False

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(BMCExecutor, "_bypass_cert_warning", false)
    monkeypatch.setattr(BMCExecutor, "_detect_account_conflict", false)
    monkeypatch.setattr(BMCExecutor, "_find_visible", none)
    monkeypatch.setattr("src.executor.bmc_executor.detect_captcha", false)

    ok, reason = asyncio.run(executor._bmc_login(page, "https://192.0.2.10", device))

    assert ok is True, reason
    assert page.goto_calls[0][2] == 20000


def test_bmc_page_recoverable_health_triggers_reload_and_regoto():
    page = _FakePage()
    executor = BMCExecutor(None, page_timeout=20)
    health = HealthResult("before_screenshot")
    health.healthy = False
    health.status = "BMC_PAGE_EMPTY"
    health.recoverable = True
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")

    recovered = asyncio.run(executor._recover_page_health_once(
        page, "https://192.0.2.10/UI/Static/#/test", device, "before_screenshot", health,
    ))

    assert recovered is True
    assert page.reload_calls == 1
    assert page.goto_calls


class _PreActionLocator:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self._page.click_calls += 1
        if getattr(self._page, "click_exception", None) is not None:
            raise self._page.click_exception
        if self._page.click_calls == 1:
            raise RuntimeError("overlay intercepted click")

    async def fill(self, value, timeout=None):
        self._page.fill_calls.append((value, timeout))

    async def press(self, value):
        self._page.press_calls.append(value)

    async def wait_for(self, timeout=None):
        self._page.wait_calls.append(timeout)


class _PreActionPage:
    def __init__(self):
        self.click_calls = 0
        self.fill_calls = []
        self.press_calls = []
        self.wait_calls = []
        self.goto_calls = []
        self.url = "https://192.0.2.10/UI/Static/#/test"
        self.click_exception = None

    def locator(self, selector):
        return _PreActionLocator(self)

    async def query_selector(self, selector):
        return None

    async def content(self):
        return ""

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))


class _DialogElement:
    def __init__(self, page, text=""):
        self._page = page
        self._text = text

    async def is_visible(self):
        return self._page.dialog_visible

    async def inner_text(self):
        return self._text

    async def click(self, timeout=None):
        self._page.dialog_close_calls += 1
        self._page.dialog_visible = False
        if self._page.close_reveals_text:
            self._page.dialog_visible = True
            self._page.dialog_text = self._page.close_reveals_text


class _SessionDialogPage(_PreActionPage):
    def __init__(self, dialog_text="", close_reveals_text=""):
        super().__init__()
        self.dialog_visible = bool(dialog_text)
        self.dialog_text = dialog_text
        self.close_reveals_text = close_reveals_text
        self.dialog_close_calls = 0
        self.screenshots = []
        self.closed = False

    def is_closed(self):
        return self.closed

    async def query_selector(self, selector):
        if selector in (
            '.custom-dialog.timeout',
            '[class*="custom-dialog"][class*="timeout"]',
            '[role="dialog"][aria-label*="提示"]',
            '.el-dialog__wrapper [role="dialog"]',
        ):
            return _DialogElement(self, self.dialog_text) if self.dialog_visible else None
        if selector.startswith("text="):
            keyword = selector.split("=", 1)[1]
            if self.dialog_visible and keyword in self.dialog_text:
                return _DialogElement(self, self.dialog_text)
        if "button:has-text" in selector and self.dialog_visible:
            return _DialogElement(self, self.dialog_text)
        return None

    async def content(self):
        if not self.dialog_visible:
            return "<html><body>normal</body></html>"
        return (
            '<div role="dialog" aria-label="提示" class="custom-dialog timeout">'
            f"{self.dialog_text}</div>"
        )


def test_pre_capture_action_retries_after_blocker_recovery(monkeypatch, tmp_path):
    page = _PreActionPage()
    executor = BMCExecutor(None, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)
    dismiss_calls = []

    async def dismiss(self, page_arg):
        dismiss_calls.append(page_arg)

    async def health(page_arg, stage, target_url=""):
        hr = HealthResult(stage)
        hr.healthy = True
        return hr

    monkeypatch.setattr(BMCExecutor, "_dismiss_all_blockers", dismiss)
    monkeypatch.setattr("src.executor.bmc_executor.check_bmc_page_health", health)

    asyncio.run(executor._execute_pre_capture_actions(
        page,
        [{"action": "click", "selector": "#target", "required": True}],
        device,
        str(tmp_path),
        result,
        target_url=page.url,
    ))

    assert page.click_calls == 2
    assert len(dismiss_calls) == 1
    assert result.execution_status == "EXEC_SUCCESS"
    assert [s.status for s in result.step_results] == ["SUCCESS"]


def test_pre_capture_session_health_failure_marks_session_recoverable(monkeypatch, tmp_path):
    page = _PreActionPage()
    executor = BMCExecutor(None, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)

    async def always_fail(self, page_arg, action, index, device_arg, output_dir):
        raise RuntimeError("click failed")

    async def dismiss(self, page_arg):
        return None

    async def health(page_arg, stage, target_url=""):
        hr = HealthResult(stage)
        hr.healthy = False
        hr.status = "BMC_SESSION_EXPIRED"
        hr.details = "redacted session expired"
        return hr

    monkeypatch.setattr(BMCExecutor, "_execute_one_pre_capture_action", always_fail)
    monkeypatch.setattr(BMCExecutor, "_dismiss_all_blockers", dismiss)
    monkeypatch.setattr("src.executor.bmc_executor.check_bmc_page_health", health)

    asyncio.run(executor._execute_pre_capture_actions(
        page,
        [{"action": "click", "selector": "#target", "required": True}],
        device,
        str(tmp_path),
        result,
        target_url=page.url,
    ))

    assert result.execution_status == "EXEC_FAILED"
    assert "BMC_PAGE_HEALTH_FAILED [BMC_SESSION_EXPIRED]" in result.execution_failure_reason


def test_pre_capture_timeout_dialog_precheck_skips_click(tmp_path):
    page = _SessionDialogPage("登录超时，请重新登录")
    executor = BMCExecutor(None, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)

    asyncio.run(executor._execute_pre_capture_actions(
        page,
        [{"action": "click", "selector": "#target", "required": True}],
        device,
        str(tmp_path),
        result,
        target_url=page.url,
    ))

    assert page.click_calls == 0
    assert result.execution_status == "EXEC_FAILED"
    assert (
        "BMC_TIMEOUT_DIALOG" in result.execution_failure_reason
        or "BMC_SESSION_EXPIRED" in result.execution_failure_reason
    )


def test_pre_capture_click_intercepted_by_dialog_is_session_failure(tmp_path):
    page = _PreActionPage()
    page.click_exception = RuntimeError(
        'Locator.click Timeout 5000ms exceeded: '
        '<div role="dialog" aria-label="提示" class="custom-dialog timeout">登录超时</div> '
        'intercepts pointer events'
    )
    executor = BMCExecutor(None, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)

    asyncio.run(executor._execute_pre_capture_actions(
        page,
        [{"action": "click", "selector": "#target", "required": True}],
        device,
        str(tmp_path),
        result,
        target_url=page.url,
    ))

    assert page.click_calls == 1
    assert result.execution_status == "EXEC_FAILED"
    assert "BMC_PAGE_HEALTH_FAILED [BMC_TIMEOUT_DIALOG]" in result.execution_failure_reason
    assert "Locator.click Timeout" not in (result.ready_failure_reason or "")


def test_before_screenshot_session_expired_saves_failure_evidence(monkeypatch, tmp_path):
    page = _SessionDialogPage("请重新登录")
    executor = BMCExecutor(None, page_timeout=20, artifact_profile="fast")
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    task = Task(1, 1, "bmc-empty-target", "BMC", "BMC_URL", command_or_url="")
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)

    class Ready:
        results = []

        def rollup(self):
            return "PASS"

        def summary(self):
            return ""

    async def ready(*args, **kwargs):
        return Ready()

    async def screenshot(self, page_arg, ss_path, task_arg, result_arg):
        Path(ss_path).write_bytes(b"png")

    async def save_raw(*args, **kwargs):
        return None

    async def redacted_html(page_arg):
        return "<html>failure</html>"

    async def healthy_page(page_arg, stage, target_url=""):
        return HealthResult(stage)

    monkeypatch.setattr(BMCExecutor, "_evaluate_capture_ready_conditions", ready)
    monkeypatch.setattr(BMCExecutor, "_content_aware_screenshot", screenshot)
    monkeypatch.setattr(BMCExecutor, "_save_raw_and_compose", save_raw)
    monkeypatch.setattr("src.executor.bmc_executor.check_bmc_page_health", healthy_page)
    monkeypatch.setattr("src.executor.bmc_executor.capture_redacted_html", redacted_html)

    asyncio.run(executor._run_capture_flow(
        page, task, device, device.bmc_ip, str(tmp_path), result,
    ))

    assert result.execution_status == "EXEC_FAILED"
    assert "BMC_PAGE_HEALTH_FAILED [BMC_SESSION_EXPIRED]" in result.execution_failure_reason
    final_screenshot = [s for s in result.step_results if s.step_name == "final_screenshot"]
    assert final_screenshot
    assert final_screenshot[-1].status == "FAILURE_EVIDENCE"
    assert not any(s.step_name == "final_screenshot" and s.status == "SUCCESS" for s in result.step_results)


def test_capture_flow_skips_business_actions_after_terminal_health_failure(monkeypatch, tmp_path):
    page = _PreActionPage()
    executor = BMCExecutor(None, page_timeout=20)
    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    task = Task(
        1, 1, "bmc-action", "BMC", "BMC_ACTIONS",
        actions_json='[{"action":"goto","value":"/UI/Static/#/test"},'
                     '{"action":"click","selector":"#target"}]',
    )
    result = ExecutionResult(plan_id="p1", device_name=device.device_name)
    final_capture_calls = []

    async def health(page_arg, stage, target_url=""):
        hr = HealthResult(stage)
        if stage == "after_navigate":
            hr.healthy = False
            hr.status = "BMC_TERMINAL_PAGE_ERROR"
            hr.details = "terminal"
        return hr

    async def no_blockers(self, page_arg):
        return None

    async def final_capture(self, page_arg, task_arg, bmc_ip, file_base, output_dir, result_arg):
        final_capture_calls.append((task_arg.task_name, result_arg.execution_status))

    async def should_not_run(*args, **kwargs):
        raise AssertionError("business action path should be skipped")

    monkeypatch.setattr("src.executor.bmc_executor.check_bmc_page_health", health)
    monkeypatch.setattr(BMCExecutor, "_dismiss_all_blockers", no_blockers)
    monkeypatch.setattr(BMCExecutor, "_execute_final_capture", final_capture)
    monkeypatch.setattr(BMCExecutor, "_execute_pre_capture_actions", should_not_run)
    monkeypatch.setattr(BMCExecutor, "_evaluate_capture_ready_conditions", should_not_run)
    monkeypatch.setattr(BMCExecutor, "_evaluate_rules", should_not_run)
    monkeypatch.setattr(BMCExecutor, "_evaluate_checkpoints", should_not_run)

    asyncio.run(executor._run_capture_flow(
        page, task, device, device.bmc_ip, str(tmp_path), result,
    ))

    assert result.execution_status == "EXEC_FAILED"
    assert final_capture_calls == [("bmc-action", "EXEC_FAILED")]


def test_bmc_page_timeout_propagates_to_sequential_and_fallback(monkeypatch):
    created_timeouts = []

    class CapturingBMCExecutor:
        def __init__(self, *args, **kwargs):
            created_timeouts.append(kwargs.get("page_timeout"))

        def execute(self, plan, output_root):
            return ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                task_name=plan.task.task_name,
                execution_status="EXEC_SUCCESS",
            )

    cfg = AppConfig()
    cfg.bmc_page_timeout = 33
    app = App(cfg)
    monkeypatch.setattr("src.app.BMCExecutor", CapturingBMCExecutor)
    app._execute_sequential([])

    from src.scheduler.dynamic_scheduler import DynamicScheduler

    scheduler = DynamicScheduler(cfg)
    scheduler._bm = object()
    monkeypatch.setattr("src.scheduler.dynamic_scheduler.BMCExecutor", CapturingBMCExecutor)
    scheduler._execute_plan(_bmc_plan("fallback", 9))

    assert created_timeouts == [33, 33]


def test_browser_reset_thread_closes_current_thread_browser():
    from src.executor.browser_manager import BrowserManager

    class FakeThreadBrowser:
        def __init__(self):
            self.closed = False
            self.dropped = False

        async def close(self):
            self.closed = True

        def _drop_refs(self):
            self.dropped = True

    bm = BrowserManager()
    fake = FakeThreadBrowser()
    tid = threading.get_ident()
    with bm._tls_lock:
        bm._tls[tid] = fake

    bm.reset_thread()

    assert fake.closed is True
    assert fake.dropped is False
    assert tid not in bm._tls


def test_executor_app_registers_current_http_surface_only():
    from src.executor_api_server.app import create_app
    from src.executor_api_server.status_service import ExecutorRuntimeStatusService
    from src.plan_run_service import PlanRunService

    app = create_app(
        ExecutorRuntimeStatusService(executor_id="test-routes"),
        plan_run_service=PlanRunService(),
    )
    paths = {getattr(route, "path", "") for route in app.routes}

    expected = {
        "/health",
        "/version",
        "/network/ping",
        "/routes",
        "/executor/v1/status",
        "/executor/v1/contracts",
        "/executor/v1/contracts/{contract_id}",
        "/executor/v1/config/excel:path",
        "/executor/v1/config/excel",
        "/executor/v1/config/latest",
        "/executor/v1/plans/{plan_id}:run",
        "/executor/v1/plans/{plan_id}",
        "/executor/v1/plans/{plan_id}/items",
        "/executor/v1/plans/{plan_id}/callbacks:retry",
        "/executor/v1/plans",
    }
    assert expected <= paths


def test_runtime_entrypoint_uses_dynamic_app_imports():
    root = Path(__file__).resolve().parent.parent
    text = (root / "run.py").read_text(encoding="utf-8")
    spec = (root / "scripts" / "build.spec").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "from src." not in text
    assert "import src." not in text
    assert "_resolve_app_dir_from_argv" in text
    assert 'str(_root / "run.py")' in spec
    assert '"src"' in spec
    assert "--exclude-module src" in workflow


def test_plan_run_service_reuses_session_for_same_endpoint_bmc_group(monkeypatch, tmp_path):
    from src.job_runner_adapter import JobResult
    from src.plan_item_status_callback_client import (
        FakeCallbackTransport,
        PlanItemStatusCallbackClient,
    )
    from src.plan_run_service.service import PlanRun, PlanRunItem, PlanRunService
    from src.resource_lock_manager import ResourceLockManager

    device = Device(1, "redacted-device", "A3", "192.0.2.10", "", "")
    task1 = Task(1, 1, "bmc-1", "BMC", "BMC_URL", command_or_url="/UI/Static/#/one")
    task2 = Task(2, 2, "bmc-2", "BMC", "BMC_URL", command_or_url="/UI/Static/#/two")
    items = [
        PlanRunItem(
            plan_id="p1", device_name=device.device_name, task_name=task1.task_name,
            device_group=device.device_group, task_type="BMC", execution_mode="BMC_URL",
            lock_uri="bmc://192.0.2.10:443", _device=device, _task=task1,
        ),
        PlanRunItem(
            plan_id="p1", device_name=device.device_name, task_name=task2.task_name,
            device_group=device.device_group, task_type="BMC", execution_mode="BMC_URL",
            lock_uri="bmc://192.0.2.10:443", _device=device, _task=task2,
        ),
    ]
    run = PlanRun(
        plan_id="p1", run_id="run-p1", runner_mode="real",
        output_root=str(tmp_path / "out"), items=items,
        item_status_url="http://cb/items", callback_mode="single",
    )
    group_calls = []

    def fake_group(self, payloads):
        group_calls.append(payloads)
        return [JobResult(status="SUCCEEDED") for _ in payloads]

    monkeypatch.setattr(
        "src.job_runner_adapter.RealRunnerAdapter.run_bmc_session_group",
        fake_group,
    )

    transport = FakeCallbackTransport()
    svc = PlanRunService(
        callback_transport=transport,
        lock_manager=ResourceLockManager(),
        workspace_root=str(tmp_path),
        allow_real_runner=True,
    )

    svc._execute_run(run, PlanItemStatusCallbackClient(transport=transport))

    assert [len(call) for call in group_calls] == [2]
    assert [item.status for item in run.items] == ["SUCCESS", "SUCCESS"]
    assert len(svc._lock_mgr.snapshot()) == 0


def test_real_runner_adapter_session_group_assigns_unique_plan_ids_without_job_ids(monkeypatch, tmp_path):
    from src.job_runner_adapter import RealRunnerAdapter

    seen_plan_ids = []

    def fake_run(self):
        seen_plan_ids.extend(plan.plan_id for plan in self._plans)
        return [
            ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                task_name=plan.task.task_name,
                execution_status="EXEC_SUCCESS",
            )
            for plan in self._plans
        ]

    monkeypatch.setattr(BMCEndpointSessionRunner, "run", fake_run)

    adapter = RealRunnerAdapter(output_root=str(tmp_path))
    adapter._bm = object()
    payload = {
        "device_snapshot": {
            "device_name": "redacted-device",
            "device_group": "A3",
            "oob_ip": "192.0.2.10",
        },
        "task_snapshot": {
            "task_name": "bmc",
            "task_type": "BMC",
            "execution_mode": "BMC_URL",
            "command_or_url": "/UI/Static/#/test",
        },
    }

    results = adapter.run_bmc_session_group([dict(payload), dict(payload)])

    assert len(results) == 2
    assert len(set(seen_plan_ids)) == 2
    assert [result.status for result in results] == ["SUCCEEDED", "SUCCEEDED"]


def test_route_guard_observes_local_changes_and_stops_only_global_storm():
    cfg = AppConfig()
    cfg.route_guard_stop_threshold = 100
    app = App(cfg)

    app._on_route_change([f"change-{i}" for i in range(46)])
    assert app._stop_event.is_set() is False

    cfg2 = AppConfig()
    cfg2.route_guard_stop_threshold = 3
    app2 = App(cfg2)
    app2._on_route_change(["a", "b", "c"])
    assert app2._stop_event.is_set() is True
    assert app2._stop_reason == "route_change"


def test_ssh_winerror_10054_gets_one_retry_without_task_retry(monkeypatch):
    monkeypatch.setattr("src.executor.retry.time.sleep", lambda _seconds: None)
    plan = TaskPlan(
        plan_id="ssh-plan",
        device=Device(
            1, "redacted-device", "L1", "", "", "",
            inband_ip="192.0.2.20", inband_username="", inband_password="",
        ),
        task=Task(1, 1, "ssh", "SSH", "SSH_CMD", command_or_url="display version", retry_count=0),
    )
    calls = []

    class Executor:
        def execute(self, plan, output_root):
            calls.append(time.time())
            if len(calls) == 1:
                return ExecutionResult(
                    plan_id=plan.plan_id,
                    device_name=plan.device.device_name,
                    execution_status="EXEC_ERROR",
                    execution_failure_reason="TRANSIENT_NETWORK_ERROR: [WinError 10054] remote host closed",
                )
            return ExecutionResult(
                plan_id=plan.plan_id,
                device_name=plan.device.device_name,
                execution_status="EXEC_SUCCESS",
            )

    result = execute_with_retry(Executor(), plan, "/tmp/redacted")

    assert result.execution_status == "EXEC_SUCCESS"
    assert result.attempt_count == 2
    assert len(calls) == 2


def test_console_done_does_not_show_attempt_count_as_completed_total(capsys):
    console.done(2981, 580, "FAIL", device_group="A3", device="redacted-device", task="task")
    out = capsys.readouterr().out

    assert "[2981/580]" not in out
    assert "[ 580/580]" in out
    assert "attempts=2981" in out
