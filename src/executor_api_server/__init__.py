"""
Executor API server — local HTTP endpoints for server-to-executor communication.

Exposes:
  GET  /executor/v1/status        — executor status
  POST /executor/v1/config/excel  — upload and activate Excel config
  POST /executor/v1/plans         — start an external plan by excelHash + planId
  GET  /executor/v1/plans/{id}    — query plan status

Uses FastAPI + uvicorn. Plan/config routes delegate to the plan-run services.
"""

from .status_service import ExecutorRuntimeStatusService
from .app import create_app

__all__ = ["ExecutorRuntimeStatusService", "create_app"]
