"""
TaskCatalogStore — in-memory task_id → PlannedTask lookup.
"""

from __future__ import annotations
from .models import PlannedTask


class TaskCatalogStore:
    """Lookup store: task_id → PlannedTask."""

    def __init__(self):
        self._by_id: dict[str, PlannedTask] = {}

    def add(self, task: PlannedTask):
        self._by_id[task.task_id] = task

    def get(self, task_id: str) -> PlannedTask | None:
        return self._by_id.get(task_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def to_dict(self) -> dict[str, dict]:
        return {tid: t.to_catalog_dict() for tid, t in self._by_id.items()}
