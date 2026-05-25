"""
Abstract base class for task executors.
"""

from abc import ABC, abstractmethod

from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult


class AbstractExecutor(ABC):

    @abstractmethod
    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        ...
