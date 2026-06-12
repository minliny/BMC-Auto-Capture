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


# ---------------------------------------------------------------------------
# Contract index
# ---------------------------------------------------------------------------

CONTRACT_INDEX: dict[str, Any] = {
    "version": "0.2.4",
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
    },

    "modes": {
        "batch": {
            "description": "All items sent in one or more batch POSTs after ALL items complete",
            "payloadStructure": {
                "planId": "string — business plan ID",
                "runId": "string — unique run identifier",
                "items": "array of item objects",
                "summary": "object — aggregate counts",
            },
            "maxBatchSize": 1000,
            "examplePayload": {
                "planId": "<planId>",
                "runId": "<runId>",
                "items": [
                    {
                        "planId": "<planId>",
                        "deviceName": "<deviceName>",
                        "taskName": "<taskName>",
                        "status": "SUCCESS",
                        "updater": "downstream-system",
                        "errorMessage": None,
                        "startedAt": "2026-01-01T00:00:00+00:00",
                        "finishedAt": "2026-01-01T00:00:01+00:00",
                    }
                ],
                "summary": {
                    "total": 1,
                    "success": 1,
                    "failed": 0,
                    "in_progress": 0,
                    "pending": 0,
                },
            },
        },
        "single": {
            "description": "Each item sent as a separate POST after ALL items complete",
            "payloadStructure": "flat object (no items wrapper) — same 8 fields as batch mode items",
            "examplePayload": {
                "planId": "<planId>",
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
        "description": "Callbacks are sent AFTER ALL items in the plan have finished executing",
        "isPerItemImmediate": False,
        "isBatchAfterAllComplete": True,
    },

    "fields": [
        {
            "name": "planId",
            "type": "string",
            "required": True,
            "source": "callback.planId from plan run request",
            "description": "Business plan ID provided by the caller",
            "example": "<planId>",
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
        "description": "Current code only sends final statuses after all items complete",
        "sent": ["SUCCESS", "FAILED"],
        "notSent": ["PENDING", "IN_PROGRESS"],
        "reason": (
            "The _execute_run method sets item.status to IN_PROGRESS during "
            "execution but only calls _deliver_via_outbox after the entire "
            "for-loop completes.  At that point each item is either SUCCESS "
            "or FAILED."
        ),
    },

    "forbiddenFields": {
        "description": "These fields are explicitly excluded from per-item callback payloads",
        "fields": [
            "excelHash", "configId", "configVersion", "storedPath",
            "executorPlanId", "callbackPlanId", "serverPlanId",
            "password", "token", "secret", "Authorization",
        ],
        "note": "runId is included in the batch wrapper (top-level), not in per-item payloads",
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
        {"statusCode": 409, "code": "LATEST_EXCEL_MISSING", "description": "storedPath file does not exist"},
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
                "description": "Runner mode: fake executes instantly, real connects to devices",
            },
        ],
    },
    "responseBody": {
        "fields": [
            {"name": "accepted", "type": "boolean", "required": True},
            {"name": "planId", "type": "integer"},
            {"name": "runId", "type": "string", "description": "Unique run identifier"},
            {"name": "status", "type": "string", "description": "Always 'ACCEPTED' on success"},
            {"name": "excelHash", "type": "string"},
            {"name": "message", "type": "string"},
            {"name": "callbackTransportMode", "type": "string", "description": "'http' if itemStatusUrl provided, 'fake' otherwise"},
        ],
    },
    "behavior": {
        "description": "Starts a background thread that executes all items serially",
        "callbackTiming": "After ALL items complete, not per-item",
        "callbackTransport": "Auto: HttpCallbackTransport when itemStatusUrl provided, FakeCallbackTransport otherwise",
    },
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
            {"name": "runner", "type": "string", "defaultValue": "fake", "allowedValues": ["fake", "real"]},
        ],
    },
    "responseBody": {
        "fields": [
            {"name": "accepted", "type": "boolean"},
            {"name": "excelHash", "type": "string"},
            {"name": "planId", "type": "string"},
            {"name": "runId", "type": "string", "description": "Unique run identifier"},
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
            {"name": "status", "type": "string", "allowedValues": ["ACCEPTED", "RUNNING", "COMPLETED"]},
            {"name": "summary", "type": "object", "children": [
                {"name": "total", "type": "integer"},
                {"name": "success", "type": "integer"},
                {"name": "failed", "type": "integer"},
                {"name": "in_progress", "type": "integer"},
                {"name": "pending", "type": "integer"},
            ]},
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
}


def get_contract_index() -> dict[str, Any]:
    """Return the contract index (list of all available contracts)."""
    return CONTRACT_INDEX


def get_contract(contract_id: str) -> dict[str, Any] | None:
    """Return a specific contract by ID, or None if not found."""
    return _CONTRACTS.get(contract_id)
