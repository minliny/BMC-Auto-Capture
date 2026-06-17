from __future__ import annotations

import json
from pathlib import Path

from src.executor.bmc_executor import BMCExecutor
from src.models.task import Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _tasks():
    data = json.loads((PROJECT_ROOT / "tasks.json").read_text(encoding="utf-8"))
    return data["tasks"]


def _types(task):
    return [condition.get("type") for condition in task.get("capture_ready_conditions", [])]


def test_storage_bmc_tasks_have_business_ready_conditions():
    tasks = _tasks()

    for task_id in ("task.001", "task.029"):
        task = tasks[task_id]
        types = _types(task)

        assert "url_contains" in types
        assert "selector_visible" in types
        assert "text_nonempty" in types
        assert "text_contains_any" in types
        assert "region_stable" in types
        assert task.get("evidence_checkpoints"), f"{task_id} must keep post-capture evidence checks"


def test_alarm_bmc_tasks_have_ready_conditions_and_evidence_checks():
    tasks = _tasks()

    for task_id in ("task.027", "task.028"):
        task = tasks[task_id]
        types = _types(task)

        assert "url_contains" in types
        assert "text_contains" in types
        assert "selector_visible" in types
        assert "region_stable" in types
        assert task.get("evidence_checkpoints"), f"{task_id} must keep post-capture evidence checks"


def test_homepage_bmc_task_has_dashboard_ready_conditions():
    task = _tasks()["task.010"]
    types = _types(task)

    assert "url_contains" in types
    assert "text_contains_any" in types
    assert "region_stable" in types


def _task_model(task_id: str, raw: dict) -> Task:
    return Task(
        row_index=0,
        sequence=0,
        task_id=task_id,
        task_name=raw.get("task_name", task_id),
        task_type=raw.get("task_type", ""),
        execution_mode=raw.get("execution_mode", ""),
        command_or_url=raw.get("command_or_url", ""),
        actions_json=raw.get("actions_json", ""),
    )


def test_all_bmc_tasks_have_explicit_ready_conditions_or_default_route():
    executor = BMCExecutor(browser_manager=None)
    missing: list[str] = []

    for task_id, raw in _tasks().items():
        if str(raw.get("task_type", "")).upper() != "BMC":
            continue
        if raw.get("capture_ready_conditions"):
            continue

        model = _task_model(task_id, raw)
        default_specs = executor._default_capture_ready_specs(model)
        default_types = {spec.condition_type for spec in default_specs}
        if not {"page_alive", "not_login_page", "url_contains"}.issubset(default_types):
            missing.append(task_id)

    assert not missing, f"BMC tasks missing explicit ready conditions and default route: {missing}"
