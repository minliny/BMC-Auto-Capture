# Executor API Implementation Plan

## Current Scope

The executor exposes one HTTP API surface under `/executor/v1`. The API is
centered on configuration import, plan execution, plan query, callback retry,
and read-only contract discovery.

## Implemented Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/executor/v1/status` | Process-level executor status. |
| `GET` | `/executor/v1/contracts` | List API contracts. |
| `GET` | `/executor/v1/contracts/{contract_id}` | Read one API contract. |
| `POST` | `/executor/v1/config/excel` | Upload and activate an Excel config. |
| `POST` | `/executor/v1/config/excel:path` | Activate an executor-local Excel config. |
| `GET` | `/executor/v1/config/latest` | Read the active config metadata. |
| `POST` | `/executor/v1/plans` | Start a plan by `excelHash` and external `planId`. |
| `POST` | `/executor/v1/plans/{plan_id}:run` | Start a local plan from the active config. |
| `GET` | `/executor/v1/plans/{plan_id}` | Query plan summary. |
| `GET` | `/executor/v1/plans/{plan_id}/items` | Query plan item details. |
| `POST` | `/executor/v1/plans/{plan_id}/callbacks:retry` | Retry pending callbacks. |

Utility endpoints (`/health`, `/version`, `/network/ping`, `/routes`) are
served by the same FastAPI app and are operational diagnostics, not a separate
API family.

## Runtime Modules

| Module | Responsibility |
|---|---|
| `src/executor_api_server/app.py` | FastAPI app and route registration. |
| `src/executor_api_server/schemas.py` | Pydantic request/response models. |
| `src/executor_api_server/contracts.py` | Read-only runtime contract metadata. |
| `src/executor_api_server/status_service.py` | Process-level status provider. |
| `src/excel_config_store/` | Managed config storage and latest metadata. |
| `src/plan_run_service/` | Plan execution, query projection, persistence, and callback delivery. |
| `src/plan_item_status_callback_client/` | Outbound plan item callback transport and payload sanitization. |

## Rule Configuration Follow-Up

The next implementation slice should keep rules inside the current config and
plan API surface:

- Import task config through the current config activation flow.
- Split BMC page rules and SSH output rules into structured rule files or API
  payload sections before execution.
- Expose rule metadata through contracts or config query endpoints only when
  needed by the server.
- Keep callback payloads stable; rule evaluation details belong in plan item
  query output (`checkResults`), not outbound callback items.
