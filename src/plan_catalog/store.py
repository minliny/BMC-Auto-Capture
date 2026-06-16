"""
TaskCatalogStore — in-memory plan_item_id → PlannedTask lookup.
"""

from __future__ import annotations
from .models import PlannedTask


class TaskCatalogStore:
    """Lookup store: plan_item_id → PlannedTask, with unique task_id fallback."""

    def __init__(self):
        self._by_id: dict[str, PlannedTask] = {}
        self._by_task_id: dict[str, list[PlannedTask]] = {}

    def add(self, task: PlannedTask):
        self._by_id[task.effective_plan_item_id] = task
        self._by_task_id.setdefault(task.task_id, []).append(task)

    def get(self, identifier: str) -> PlannedTask | None:
        exact = self._by_id.get(identifier)
        if exact is not None:
            return exact
        matches = self._by_task_id.get(identifier, [])
        if len(matches) == 1:
            return matches[0]
        return None

    def get_by_task_id(self, task_id: str) -> list[PlannedTask]:
        return list(self._by_task_id.get(task_id, []))

    def __len__(self) -> int:
        return len(self._by_id)

    def to_dict(self) -> dict[str, dict]:
        return {pid: t.to_catalog_dict() for pid, t in self._by_id.items()}
