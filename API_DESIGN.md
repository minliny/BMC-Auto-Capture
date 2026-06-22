# BMC Auto-Capture Executor API Design

## Scope

The current HTTP surface exposes one Executor API only. Server-side systems
should integrate through the Excel config, plan start, plan query, and callback
retry endpoints. Removed job/command/runId HTTP designs are not part of the
current contract.

## Current Inbound API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/executor/v1/status` | Query executor status. |
| `GET` | `/executor/v1/contracts` | List available API contracts. |
| `GET` | `/executor/v1/contracts/{contract_id}` | Query one API contract. |
| `POST` | `/executor/v1/config/excel` | Upload and activate an Excel config. |
| `POST` | `/executor/v1/config/excel:path` | Activate an executor-local Excel file for local/debug use. |
| `GET` | `/executor/v1/config/latest` | Query the active Excel config. |
| `POST` | `/executor/v1/plans` | Start an external plan by `excelHash` and `callback.planId`. |
| `POST` | `/executor/v1/plans/{plan_id}:run` | Start a local plan by ID using the active config. |
| `GET` | `/executor/v1/plans/{plan_id}` | Query plan summary. |
| `GET` | `/executor/v1/plans/{plan_id}/items` | Query plan item details. |
| `POST` | `/executor/v1/plans/{plan_id}/callbacks:retry` | Retry pending callbacks for a plan. |
| `GET` | `/executor/v1/config/rule-capabilities` | Query supported RulePack classes and check types. |
| `POST` | `/executor/v1/config/rule-packs:validate` | Validate RulePack JSON. |
| `POST` | `/executor/v1/config/rule-packs:import` | Import RulePack JSON. |
| `GET` | `/executor/v1/config/rule-packs` | List stored RulePacks. |
| `GET` | `/executor/v1/config/rule-packs/{task_id}` | Query one task RulePack. |
| `PUT` | `/executor/v1/config/rule-packs/{task_id}` | Replace one task RulePack. |

## Utility Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check. |
| `GET` | `/version` | Runtime version and build metadata. |
| `GET` | `/network/ping` | Network reachability check. |
| `GET` | `/routes` | Route listing for diagnostics. |

These utility endpoints are served by the same Executor API process. They are
not a separate API family.

## Outbound Callback

The executor posts item status updates to `callback.itemStatusUrl` from the
plan request. Callback payloads use public plan item fields only:

- `planId`
- `taskId`
- `planItemId`
- `deviceGroup`
- `deviceName`
- `taskName`
- `status`
- `updater`
- `errorMessage`
- `startedAt`
- `finishedAt`

Callback retries use `/executor/v1/plans/{plan_id}/callbacks:retry`.

## RulePacks

Task audit rules use `rulepack.v1`. The framework defines four rule classes:
`stage_gate`, `action_completion`, `content_integrity`, and
`evidence_validation`. Concrete selectors, text markers, and regex patterns
are generated from real BMC/SSH evidence by the authoring skills and imported
through the RulePack config API. Runtime compatibility is handled by the
RulePack adapter, which maps RulePacks into the current executor fields.

## Removed HTTP Designs

The previous job/command/runId API drafts have been removed from the active
HTTP contract. Do not add a second API family for new work; extend the current
`config`, `plans`, `contracts`, and callback retry surfaces instead.

Authoritative runtime contracts live in `src/executor_api_server/contracts.py`
and are exposed at `/executor/v1/contracts`.
