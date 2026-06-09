"""
RunDispatchService — plan import, run dispatch, execute, query.

Orchestrates:
  - PlanCatalogPlanner for plan import
  - TaskCatalogStore for task_id → PlannedTask lookup
  - ResourceLockManager for concurrency control
  - FakeRunner/RealRunnerAdapter for execution
  - ServerCallbackClient for per-task status callbacks
"""

from .service import RunDispatchService, RunStatus, TaskRunStatus

__all__ = ["RunDispatchService", "RunStatus", "TaskRunStatus"]
