"""
Executor API server — local HTTP endpoints for server-to-executor communication.

Exposes:
  POST /executor/v1/jobs          — receive dispatched job
  GET  /executor/v1/jobs/{job_id} — query job status
  GET  /executor/v1/status        — executor health + job counts

Uses FastAPI + uvicorn. All business logic delegated to DirectDispatchService.
"""

from .service import DirectDispatchService
from .app import create_app

__all__ = ["DirectDispatchService", "create_app"]
