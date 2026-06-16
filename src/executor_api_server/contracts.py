"""
Static API contract definitions for the Executor API.

These contracts describe the current runtime behavior of the executor
without reading any Excel files, connecting to external services, or
modifying any state.  All example values use generic placeholders.

Contracts are derived from code audit — they describe what the code
actually does, not what a future version might do.
"""

from __future__ import annotations

from typing import Any

from src._version import APP_VERSION


# ---------------------------------------------------------------------------
# Contract index
# ---------------------------------------------------------------------------

CONTRACT_INDEX: dict[str, Any] = {
    "version": APP_VERSION,
    "description": "Executor API contract index — read-only, no side effects",
    "contracts": [
        {
            "id": "plan-item-status-callback",
            "name": "PlanItem Status Callback (outbound)",
            "method": "POST",
            "direction": "outbound",
            "path": "{callback.itemStatusUrl}",
            "detailPath": "/executor/v1/contracts/plan-item-status-callback",
        },
        {
            "id": "excel-upload",
            "name": "Excel Config Upload",
            "method": "POST",
            "direction": "inbound",
            "path": "/executor/v1/config/excel",
            "detailPath": "/executor/v1/contracts/excel-upload",
        },
        {
            "id": "excel-path",
            "name": "Excel Config Set by Path",
            "method": "POST",
            "direction": "inbound",
            "path": "/executor/v1/config/excel:path",
            "detailPath": "/executor/v1/contracts/excel-path",
        },
        {
            "id": "config-latest",
            "name": "Latest Config Query",
            "method": "GET",
            "direction": "inbound",
            "path": "/executor/v1/config/latest",
            "detailPath": "/executor/v1/contracts/config-latest",
        },
        {
            "id": "plan-run",
            "name": "Start Plan Run",
            "method": "POST",
            "direction": "inbound",
            "path": "/executor/v1/plans/{plan_id}:run",
            "detailPath": "/executor/v1/contracts/plan-run",
        },
        {
            "id": "external-plan",
            "name": "Start External Plan",
            "method": "POST",
            "direction": "inbound",
            "path": "/executor/v1/plans",
            "detailPath": "/executor/v1/contracts/external-plan",
        },
        {
            "id": "plan-query",
            "name": "Plan Status Query",
            "method": "GET",
            "direction": "inbound",
            "path": "/executor/v1/plans/{plan_id}",
            "detailPath": "/executor/v1/contracts/plan-query",
        },
        {
            "id": "plan-items",
            "name": "Plan Items Query",
            "method": "GET",
            "direction": "inbound",
            "path": "/executor/v1/plans/{plan_id}/items",
            "detailPath": "/executor/v1/contracts/plan-items",
        },
        {
            "id": "callback-retry",
            "name": "Retry Pending Plan Callbacks",
            "method": "POST",
            "direction": "inbound",
            "path": "/executor/v1/plans/{plan_id}/callbacks:retry",
            "detailPath": "/executor/v1/contracts/callback-retry",
        },
    ],
}


# ---------------------------------------------------------------------------
# PlanItem Status Callback contract (outbound)
# ---------------------------------------------------------------------------

PLAN_ITEM_STATUS_CALLBACK_CONTRACT: dict[str, Any] = {
    "id": "plan-item-status-callback",
    "name": "PlanItem Status Callback",
    "description": (
        "The executor POSTs plan-item status updates to the URL specified "
        "in the plan run request's callback.itemStatusUrl field. "
        "This is an OUTBOUND request from the executor to an external server."
    ),
    "direction": "outbound",
    "method": "POST",
    "path": "{callback.itemStatusUrl}",
    "contentType": "application/json; charset=utf-8",

    "callbackUrlSource": {
        "field": "callback.itemStatusUrl",
        "description": "URL to which callback payloads are POSTed",
        "resolutionPriority": [
            "1. master_registry active server (EXECUTOR_MASTER_REGISTRY_URL env var)",
            "2. callback.itemStatusUrl from plan run request",
            "3. EXECUTOR_PLAN_ITEM_STATUS_URL env var",
            "4. empty string (no callback sent)",
        ],
    },

    "transportPreconditions": {
        "httpTransportEnabledWhen": (
            "Auto-detected: if callback.itemStatusUrl is non-empty in the "
            "plan run request, HttpCallbackTransport is used automatically. "
            "If itemStatusUrl is empty or missing, FakeCallbackTransport is used. "
            "An explicit callback_transport passed at construction time takes priority."
        ),
        "defaultTransport": "Auto: HttpCallbackTransport when itemStatusUrl provided, FakeCallbackTransport otherwise",
        "realHttpTransport": "HttpCallbackTransport (stdlib urllib)",
        "urlPolicy": (
            "Only http/https URLs are accepted. URL userinfo is forbidden. "
            "A parseable host is required. Intranet and loopback literal IPs "
            "are accepted for on-prem deployments."
        ),
    },

    "modes": {
        "batch": {
            "description": "One or more changed items sent in a batch wrapper when item status changes",
            "payloadStructure": {
                "planId": "string — business plan ID",
                "items": "array of item objects",
                "summary": "optional object — only present for final batch summaries",
            },
            "maxBatchSize": 1000,
            "examplePayload": {
                "planId": "<planId>",
                "items": [
                    {
                        "planId": "<planId>",
                        "deviceGroup": "<deviceGroup>",
                        "deviceName": "<deviceName>",
                        "taskName": "<taskName>",
                        "status": "IN_PROGRESS",
                        "updater": "downstream-system",
                        "errorMessage": None,
                        "startedAt": "2026-01-01T00:00:00+00:00",
                        "finishedAt": None,
                    }
                ],
            },
        },
        "summary": {
            "description": "Final plan summary sent after the batch completes",
            "payloadStructure": {
                "planId": "string — business plan ID",
                "summary": "object — aggregate counts, failureSummary, outputRoot",
            },
            "examplePayload": {
                "planId": "<planId>",
                "summary": {
                    "total": 1,
                    "success": 1,
                    "failed": 0,
                    "in_progress": 0,
                    "pending": 0,
                    "failureSummary": [],
                    "outputRoot": "<outputRoot>",
                },
            },
        },
        "single": {
            "description": "Each changed item sent as a separate POST when item status changes",
            "payloadStructure": "flat object (no items wrapper) — same item fields as batch mode",
            "examplePayload": {
                "planId": "<planId>",
                "deviceGroup": "<deviceGroup>",
                "deviceName": "<deviceName>",
                "taskName": "<taskName>",
                "status": "FAILED",
                "updater": "downstream-system",
                "errorMessage": "<error description>",
                "startedAt": "2026-01-01T00:00:00+00:00",
                "finishedAt": "2026-01-01T00:00:01+00:00",
            },
        },
    },

    "callbackTiming": {
        "description": "Item callbacks are sent whenever an item status changes; a final summary is sent after all items finish",
        "isPerItemImmediate": True,
        "isBatchAfterAllComplete": False,
        "hasFinalSummary": True,
    },

    "fields": [
        {
            "name": "planId",
            "type": "string",
            "required": True,
            "source": "path plan_id for /plans/{plan_id}:run; callback.planId only for legacy /plans request bodies",
            "description": "Business plan ID / batch ID used by both executor and scheduler",
            "example": "<planId>",
        },
        {
            "name": "deviceGroup",
            "type": "string",
            "required": True,
            "source": "Excel device row device_group column",
            "description": "Device group used with planId/deviceName/taskName to locate the task",
            "example": "<deviceGroup>",
        },
        {
            "name": "deviceName",
            "type": "string",
            "required": True,
            "source": "Excel device row device_name column",
            "description": "Name of the device this item targets",
            "example": "<deviceName>",
        },
        {
            "name": "taskName",
            "type": "string",
            "required": True,
            "source": "Excel task row task_name column",
            "description": "Name of the task this item targets",
            "example": "<taskName>",
        },
        {
            "name": "status",
            "type": "string",
            "required": True,
            "source": "Internal PlanRunItem.status mapped via _STATUS_TO_SERVER",
            "allowedValues": ["PENDING", "IN_PROGRESS", "SUCCESS", "FAILED"],
            "description": "Mapped status value sent to server",
            "mapping": {
                "PENDING": "PENDING",
                "RUNNING": "IN_PROGRESS",
                "IN_PROGRESS": "IN_PROGRESS",
                "SUCCESS": "SUCCESS",
                "FAILED": "FAILED",
            },
        },
        {
            "name": "updater",
            "type": "string",
            "required": True,
            "source": "updater field from plan run request, default 'downstream-system'",
            "defaultValue": "downstream-system",
            "example": "downstream-system",
        },
        {
            "name": "errorMessage",
            "type": "string | null",
            "required": False,
            "source": "PlanRunItem.error_message, redacted for sensitive values",
            "defaultValue": None,
            "description": "Error description if status is FAILED, null otherwise",
            "example": None,
        },
        {
            "name": "startedAt",
            "type": "string | null",
            "required": False,
            "source": "PlanRunItem.started_at converted to ISO 8601",
            "defaultValue": None,
            "description": "ISO 8601 timestamp when the item started execution",
            "example": "2026-01-01T00:00:00+00:00",
        },
        {
            "name": "finishedAt",
            "type": "string | null",
            "required": False,
            "source": "PlanRunItem.finished_at converted to ISO 8601",
            "defaultValue": None,
            "description": "ISO 8601 timestamp when the item finished execution",
            "example": "2026-01-01T00:00:01+00:00",
        },
    ],

    "statusesActuallySent": {
        "description": "Current code sends status changes as they happen",
        "sent": ["IN_PROGRESS", "SUCCESS", "FAILED"],
        "notSent": ["PENDING"],
        "reason": "PENDING is the initial in-memory state before execution starts.",
    },

    "forbiddenFields": {
        "description": "These fields are explicitly excluded from per-item callback payloads",
        "fields": [
            "excelHash", "configId", "configVersion", "storedPath",
            "executorPlanId", "callbackPlanId", "serverPlanId",
            "runId", "password", "token", "secret", "Authorization",
        ],
        "note": "runId is internal/debug-only and is not serialized in callback payloads",
    },

    "serverExpectedResponse": {
        "description": "The executor parses the server's JSON response",
        "format": {
            "code": "integer — 0 means success, non-zero means error",
            "message": "string — human-readable message",
            "data": {
                "total": "integer — total items processed",
                "success": "integer — items successfully accepted",
                "failed": "integer — items rejected",
                "errors": "array — per-item error details",
            },
        },
        "exampleSuccess": {
            "code": 0,
            "message": "success",
            "data": {"total": 1, "success": 1, "failed": 0, "errors": []},
        },
    },

    "failureHandling": {
        "callbackFailureDoesNotBlockExecution": True,
        "description": (
            "If the callback POST fails (network error, non-2xx response, etc.), "
            "the local plan/run status is NOT changed.  Failed items are written "
            "to the outbox as FAILED_RETRYABLE or FAILED_FINAL for later retry."
        ),
        "outboxRetry": {
            "maxAttempts": 5,
            "backoffBase": 5.0,
            "backoffStrategy": "exponential",
        },
    },
}


# ---------------------------------------------------------------------------
# Excel Upload contract
# ---------------------------------------------------------------------------

EXCEL_UPLOAD_CONTRACT: dict[str, Any] = {
    "id": "excel-upload",
    "name": "Excel Config Upload",
    "method": "POST",
    "direction": "inbound",
    "path": "/executor/v1/config/excel",
    "contentType": "multipart/form-data",
    "requestBody": {
        "type": "multipart/form-data",
        "fields": [
            {
                "name": "file",
                "type": "file",
                "required": True,
                "description": "Excel (.xlsx) configuration file",
            },
        ],
    },
    "responseBody": {
        "fields": [
            {"name": "accepted", "type": "boolean", "required": True},
            {"name": "excelHash", "type": "string", "description": "SHA-256 of the uploaded file"},
            {"name": "filename", "type": "string"},
            {"name": "deviceCount", "type": "integer"},
            {"name": "enabledDeviceCount", "type": "integer"},
            {"name": "taskCount", "type": "integer"},
            {"name": "enabledTaskCount", "type": "integer"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Excel Path contract
# ---------------------------------------------------------------------------

EXCEL_PATH_CONTRACT: dict[str, Any] = {
    "id": "excel-path",
    "name": "Excel Config Set by Path",
    "method": "POST",
    "direction": "inbound",
    "path": "/executor/v1/config/excel:path",
    "contentType": "application/json",
    "requestBody": {
        "fields": [
            {
                "name": "excelPath",
                "type": "string",
                "required": True,
                "description": "Executor-local absolute path to the Excel file",
                "note": "Local debug only — do not expose to external callers",
            },
        ],
    },
    "responseBody": {
        "fields": [
            {"name": "accepted", "type": "boolean", "required": True},
            {"name": "excelHash", "type": "string"},
            {"name": "filename", "type": "string"},
            {"name": "deviceCount", "type": "integer"},
            {"name": "enabledDeviceCount", "type": "integer"},
            {"name": "taskCount", "type": "integer"},
            {"name": "enabledTaskCount", "type": "integer"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Config Latest contract
# ---------------------------------------------------------------------------

CONFIG_LATEST_CONTRACT: dict[str, Any] = {
    "id": "config-latest",
    "name": "Latest Config Query",
    "method": "GET",
    "direction": "inbound",
    "path": "/executor/v1/config/latest",
    "responseBody": {
        "fields": [
            {"name": "hasLatest", "type": "boolean", "required": True},
            {"name": "excelHash", "type": "string", "description": "SHA-256 hash of the Excel file"},
            {"name": "filename", "type": "string", "description": "Original filename of the Excel file"},
            {"name": "sha256", "type": "string", "description": "Alias for excelHash"},
            {"name": "deviceCount", "type": "integer"},
            {"name": "enabledDeviceCount", "type": "integer"},
            {"name": "taskCount", "type": "integer"},
            {"name": "enabledTaskCount", "type": "integer"},
            {"name": "source", "type": "string", "description": "How the config was activated (upload, path, etc.)"},
            {"name": "createdAt", "type": "string", "description": "ISO 8601 timestamp when config was activated"},
        ],
        "note": "storedPath is intentionally excluded from this response to avoid leaking local file paths",
    },
    "errorResponses": [
        {"statusCode": 409, "code": "CONFIG_CORRUPTED", "description": "latest.json is not valid JSON"},
        {"statusCode": 409, "code": "LATEST_EXCEL_MISSING", "description": "latest Excel file does not exist"},
        {"statusCode": 409, "code": "LATEST_EXCEL_HASH_MISMATCH", "description": "latest Excel file hash does not match metadata"},
    ],
}


# ---------------------------------------------------------------------------
# Plan Run contract
# ---------------------------------------------------------------------------

PLAN_RUN_CONTRACT: dict[str, Any] = {
    "id": "plan-run",
    "name": "Start Plan Run",
    "method": "POST",
    "direction": "inbound",
    "path": "/executor/v1/plans/{plan_id}:run",
    "pathParams": [
        {"name": "plan_id", "type": "integer", "required": True, "description": "Numeric plan ID"},
    ],
    "requestBody": {
        "fields": [
            {
                "name": "callback",
                "type": "object",
                "required": False,
                "defaultValue": {},
                "description": "Callback configuration for status reporting",
                "children": [
                    {
                        "name": "callback.itemStatusUrl",
                        "type": "string",
                        "required": False,
                        "defaultValue": "",
                        "description": "URL to receive plan-item status callbacks",
                        "example": "http://<server>:<port>/api/plan-item-statuses",
                    },
                    {
                        "name": "callback.planId",
                        "type": "string | integer",
                        "required": False,
                        "defaultValue": "",
                        "description": (
                            "Business plan ID included in callback payloads. "
                            "NOTE: path plan_id is ALWAYS authoritative. "
                            "If callback.planId differs from path plan_id, a WARNING "
                            "is logged and path plan_id is used — callback.planId is "
                            "never used to override the run's ownership."
                        ),
                        "example": "<planId>",
                    },
                    {
                        "name": "callback.mode",
                        "type": "string",
                        "required": False,
                        "defaultValue": "batch",
                        "allowedValues": ["batch", "single"],
                        "description": "Callback delivery mode",
                    },
                ],
            },
            {
                "name": "updater",
                "type": "string",
                "required": False,
                "defaultValue": "downstream-system",
                "description": "Value sent in the updater field of each callback payload",
            },
            {
                "name": "runner",
                "type": "string",
                "required": False,
                "defaultValue": "fake",
                "allowedValues": ["fake", "real"],
                "description": "Runner mode: fake executes instantly; real connects to devices only when server-side real runner enablement is active",
            },
        ],
    },
	    "responseBody": {
	        "fields": [
	            {"name": "accepted", "type": "boolean", "required": True},
	            {"name": "planId", "type": "integer"},
	            {"name": "status", "type": "string", "description": "Always 'ACCEPTED' on success"},
	            {"name": "excelHash", "type": "string"},
	            {"name": "message", "type": "string"},
            {"name": "callbackTransportMode", "type": "string", "description": "'http' if itemStatusUrl provided, 'fake' otherwise"},
        ],
    },
	    "behavior": {
	        "description": "Starts a background thread that executes all items serially",
	        "callbackTiming": "On each item status change, with a final summary after all items complete",
	        "callbackTransport": "Auto: HttpCallbackTransport when itemStatusUrl provided, FakeCallbackTransport otherwise",
	        "realRunnerGate": "runner=real requires --enable-real-runner, PlanRunService(allow_real_runner=True), or EXECUTOR_ENABLE_REAL_RUNNER=1",
	    },
    "errorResponses": [
        {"statusCode": 400, "code": "REAL_RUNNER_NOT_ENABLED", "description": "runner=real requested without server-side enablement"},
        {"statusCode": 400, "code": "INVALID_CALLBACK_URL", "description": "callback URL rejected by URL policy"},
    ],
}


# ---------------------------------------------------------------------------
# External Plan contract
# ---------------------------------------------------------------------------

EXTERNAL_PLAN_CONTRACT: dict[str, Any] = {
    "id": "external-plan",
    "name": "Start External Plan",
    "method": "POST",
    "direction": "inbound",
    "path": "/executor/v1/plans",
    "requestBody": {
        "fields": [
            {
                "name": "excelHash",
                "type": "string",
                "required": True,
                "description": "SHA-256 hash of the currently activated Excel config",
            },
            {
                "name": "callback",
                "type": "object",
                "required": False,
                "defaultValue": {},
                "children": [
                    {"name": "callback.itemStatusUrl", "type": "string", "required": False, "defaultValue": ""},
                    {"name": "callback.planId", "type": "string", "required": True, "description": "Business plan ID"},
                    {"name": "callback.mode", "type": "string", "defaultValue": "batch", "allowedValues": ["batch", "single"]},
                ],
            },
            {"name": "updater", "type": "string", "defaultValue": "downstream-system"},
            {
                "name": "runner",
                "type": "string",
                "defaultValue": "fake",
                "allowedValues": ["fake", "real"],
                "description": "runner=real requires server-side real runner enablement",
            },
        ],
    },
	    "responseBody": {
	        "fields": [
	            {"name": "accepted", "type": "boolean"},
	            {"name": "excelHash", "type": "string"},
	            {"name": "planId", "type": "string"},
	            {"name": "status", "type": "string"},
	            {"name": "callbackTransportMode", "type": "string", "description": "'http' if itemStatusUrl provided, 'fake' otherwise"},
	        ],
    },
}


# ---------------------------------------------------------------------------
# Plan Query contract
# ---------------------------------------------------------------------------

PLAN_QUERY_CONTRACT: dict[str, Any] = {
    "id": "plan-query",
    "name": "Plan Status Query",
    "method": "GET",
    "direction": "inbound",
    "path": "/executor/v1/plans/{plan_id}",
    "pathParams": [
        {"name": "plan_id", "type": "string", "required": True},
    ],
    "queryParams": [
        {"name": "excelHash", "type": "string", "required": False, "description": "Required for external plan queries"},
    ],
    "responseBody": {
        "fields": [
            {"name": "planId", "type": "string | integer"},
            {"name": "status", "type": "string", "allowedValues": ["ACCEPTED", "RUNNING", "COMPLETED", "FAILED"]},
            {"name": "summary", "type": "object", "children": [
                {"name": "total", "type": "integer"},
                {"name": "success", "type": "integer"},
                {"name": "failed", "type": "integer"},
                {"name": "in_progress", "type": "integer"},
                {"name": "pending", "type": "integer"},
                {"name": "failureSummary", "type": "array"},
                {"name": "outputRoot", "type": "string"},
            ]},
            {"name": "outputRoot", "type": "string"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Plan Items contract
# ---------------------------------------------------------------------------

PLAN_ITEMS_CONTRACT: dict[str, Any] = {
    "id": "plan-items",
    "name": "Plan Items Query",
    "method": "GET",
    "direction": "inbound",
    "path": "/executor/v1/plans/{plan_id}/items",
    "pathParams": [
        {"name": "plan_id", "type": "string", "required": True},
    ],
    "queryParams": [
        {"name": "excelHash", "type": "string", "required": False, "description": "Required for external plan queries"},
    ],
    "responseBody": {
        "fields": [
            {"name": "planId", "type": "string | integer"},
            {"name": "status", "type": "string"},
            {"name": "summary", "type": "object"},
            {"name": "items", "type": "array", "itemFields": [
                {"name": "deviceGroup", "type": "string"},
                {"name": "deviceName", "type": "string"},
                {"name": "taskName", "type": "string"},
                {"name": "status", "type": "string", "allowedValues": ["PENDING", "IN_PROGRESS", "SUCCESS", "FAILED"]},
                {"name": "errorMessage", "type": "string | null"},
                {"name": "startedAt", "type": "string", "description": "ISO 8601 or null"},
                {"name": "finishedAt", "type": "string", "description": "ISO 8601 or null"},
                {"name": "infoEvents", "type": "array"},
            ]},
        ],
    },
}


# ---------------------------------------------------------------------------
# Run Query + Callback Retry contracts
# ---------------------------------------------------------------------------

RUN_QUERY_CONTRACT: dict[str, Any] = {
    "id": "run-query",
    "name": "Run Status Query (Deprecated/Internal)",
    "method": "GET",
    "direction": "inbound",
    "path": "/executor/v1/runs/{run_id}",
    "deprecated": True,
    "debugOnly": True,
    "pathParams": [
        {"name": "run_id", "type": "string", "required": True},
    ],
    "responseBody": PLAN_QUERY_CONTRACT["responseBody"],
    "relatedPath": "/executor/v1/runs/{run_id}/items",
}


CALLBACK_RETRY_CONTRACT: dict[str, Any] = {
    "id": "callback-retry",
    "name": "Retry Pending Plan Callbacks",
    "method": "POST",
    "direction": "inbound",
    "path": "/executor/v1/plans/{plan_id}/callbacks:retry",
    "pathParams": [
        {"name": "plan_id", "type": "string", "required": True},
    ],
    "requestBody": {
        "fields": [
            {"name": "callbackUrl", "type": "string", "required": False, "description": "One-time retry URL override"},
            {"name": "mode", "type": "string", "defaultValue": "batch", "allowedValues": ["batch", "single"]},
        ],
    },
    "responseBody": {
        "fields": [
            {"name": "accepted", "type": "boolean"},
            {"name": "planId", "type": "string | integer"},
            {"name": "attempted", "type": "integer"},
            {"name": "sent", "type": "integer"},
            {"name": "failed", "type": "integer"},
            {"name": "pendingAfter", "type": "integer"},
            {"name": "status", "type": "string"},
            {"name": "message", "type": "string"},
        ],
    },
    "note": "The outbox stores redacted callback URLs; retry uses callbackUrl, registry, run request URL, or env resolution.",
}


# ---------------------------------------------------------------------------
# Contract lookup
# ---------------------------------------------------------------------------

_CONTRACTS: dict[str, dict[str, Any]] = {
    "plan-item-status-callback": PLAN_ITEM_STATUS_CALLBACK_CONTRACT,
    "excel-upload": EXCEL_UPLOAD_CONTRACT,
    "excel-path": EXCEL_PATH_CONTRACT,
    "config-latest": CONFIG_LATEST_CONTRACT,
    "plan-run": PLAN_RUN_CONTRACT,
    "external-plan": EXTERNAL_PLAN_CONTRACT,
    "plan-query": PLAN_QUERY_CONTRACT,
    "plan-items": PLAN_ITEMS_CONTRACT,
    "run-query": RUN_QUERY_CONTRACT,
    "callback-retry": CALLBACK_RETRY_CONTRACT,
}


def get_contract_index() -> dict[str, Any]:
    """Return the contract index (list of all available contracts)."""
    return CONTRACT_INDEX


def get_contract(contract_id: str) -> dict[str, Any] | None:
    """Return a specific contract by ID, or None if not found."""
    return _CONTRACTS.get(contract_id)
