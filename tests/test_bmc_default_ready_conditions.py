from __future__ import annotations

import asyncio

from src.executor.bmc_executor import BMCExecutor
from src.models.task import Task


class _FakePage:
    def __init__(self, url: str, body: str = "READY"):
        self.url = url
        self.body = body

    def is_closed(self):
        return False

    async def query_selector(self, _selector: str):
        return None

    async def inner_text(self, selector: str):
        return self.body if selector == "body" else ""


def _run_ready(task: Task, page: _FakePage):
    executor = BMCExecutor(browser_manager=None)
    return asyncio.run(executor._evaluate_capture_ready_conditions(page, task, device=None))


def test_bmc_url_without_explicit_ready_conditions_derives_target_route():
    task = Task(
        1,
        1,
        "asset",
        "BMC",
        "BMC_URL",
        command_or_url="/UI/Static/#/navigate/system/info/product",
    )
    page = _FakePage("https://192.0.2.10/UI/Static/#/navigate/system/info/product")

    result = _run_ready(task, page)
    checks = {cr.condition_type: cr for cr in result.results}

    assert checks["page_alive"].status == "PASS"
    assert checks["not_login_page"].status == "PASS"
    assert checks["url_contains"].target == "/navigate/system/info/product"
    assert checks["url_contains"].status == "PASS"


def test_bmc_actions_without_explicit_ready_conditions_derives_first_goto_route():
    task = Task(
        1,
        1,
        "action-flow",
        "BMC",
        "BMC_ACTIONS",
        actions_json='[{"action":"goto","value":"/UI/Static/#/navigate/system/storage"},'
                     '{"action":"click","selector":"#LogicalDrive0"}]',
    )
    page = _FakePage("https://192.0.2.10/UI/Static/#/navigate/system/storage")

    result = _run_ready(task, page)
    checks = {cr.condition_type: cr for cr in result.results}

    assert checks["url_contains"].target == "/navigate/system/storage"
    assert checks["url_contains"].status == "PASS"


def test_explicit_capture_ready_conditions_are_not_augmented():
    task = Task(1, 1, "explicit", "BMC", "BMC_URL", command_or_url="/wrong")
    object.__setattr__(task, "_task_def", {
        "capture_ready_conditions": [{"type": "text_contains", "target": "READY"}],
    })
    page = _FakePage("https://192.0.2.10/not-the-command-url", body="READY")

    result = _run_ready(task, page)

    assert [cr.condition_type for cr in result.results] == ["text_contains"]
    assert result.results[0].status == "PASS"
