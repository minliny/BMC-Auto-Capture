---
name: bmc-auto-capture-bmc-page-rules
description: Generate, review, and patch BMC Auto-Capture BMC page rule JSON for BMC_URL and BMC_ACTIONS tasks. Use when creating capture_ready_conditions, evidence_checkpoints, or BMC page evidence assertions from real BMC HTML, state JSON, MHTML, screenshot, and metadata artifacts.
---

# BMC Page Rule Authoring

Use this skill only for BMC page capture and evidence rules. Its outputs belong in `capture_ready_conditions` and `evidence_checkpoints`; use legacy BMC `rules` only when the user explicitly asks for live page assertions through the existing rule engine.

## Inspect First

Read these files before generating or editing rules:

```bash
sed -n '1,130p' src/rules/condition_evaluator.py
sed -n '190,430p' src/rules/condition_evaluator.py
sed -n '1228,1288p' src/executor/bmc_executor.py
sed -n '1568,1635p' src/executor/bmc_executor.py
sed -n '1773,1888p' src/executor/bmc_executor.py
sed -n '270,360p' docs/TASK_ADDING_GUIDE.md
```

Inspect the target task in `tasks.json` and the real output artifacts:

- `html/*.state.json`
- `html/*.html`
- `html/*.evidence.html`
- `html/*.metadata.json`
- Screenshot only as supporting evidence

## Rule Layer Decision

Use `capture_ready_conditions` for live page readiness before final screenshot:

- route reached
- login page not visible
- key container visible
- key fields are non-empty
- loading placeholders are gone
- dynamic region is stable

Use `evidence_checkpoints` for saved evidence after final capture:

- required business text appears in saved evidence
- forbidden status/error text is absent
- regex pattern matches evidence text
- expected artifact exists

Do not put post-capture business assertions in ready conditions unless the page cannot be considered screenshot-ready without them.

## Supported Ready Conditions

Generate only condition types the executor supports:

- `url_contains`, `url_not_contains`
- `selector_visible`, `selector_hidden`, `selector_not_visible`
- `selector_count_ge`, `count_ge`
- `text_contains`, `text_contains_any`
- `text_nonempty`
- `text_not_in`
- `region_stable`

For important pages, prefer this minimum pattern:

```json
[
  {"type": "url_contains", "target": "/navigate/system/storage"},
  {"type": "selector_visible", "selector": "#LogicalDrive0"},
  {"type": "text_nonempty", "selector": "#LogicalDrive0"},
  {
    "type": "region_stable",
    "selector": "body",
    "stable_for_ms": 1000,
    "sample_interval_ms": 250,
    "timeout_ms": 3000
  }
]
```

For dynamic tables, add `selector_count_ge`. For placeholder-prone fields, add `text_not_in` with values such as `""`, `"--"`, `"N/A"`, `"Loading"`, and `"加载中"`.

## Supported Evidence Checkpoints

Generate only checkpoint types the executor supports:

- `file_exists`
- `html_contains`
- `txt_contains`
- `text_contains`
- `text_contains_any`
- `text_not_contains`
- `not_contains_any`
- `regex_match`

Use `severity: "ERROR"` only for hard failures. Use `severity: "WARNING"` for BMC-version-dependent labels or optional quality checks.

## Selector Rules

Prefer stable selectors:

1. IDs and explicit attributes
2. Short semantic selectors
3. Text checks as fallback

Avoid generated classes, absolute DOM paths, nth-child selectors, device-specific IPs, and volatile timestamps.

## Output Contract

For one BMC task, output:

```json
{
  "task_id": "task.xxx",
  "capture_ready_conditions": [],
  "evidence_checkpoints": []
}
```

When editing `tasks.json`, preserve unrelated task fields and order.

## Example

```json
{
  "task_id": "task.029",
  "capture_ready_conditions": [
    {"type": "url_contains", "target": "/navigate/system/storage"},
    {"type": "selector_visible", "selector": "#LogicalDrive0"},
    {"type": "text_nonempty", "selector": "#LogicalDrive0"},
    {
      "type": "text_contains_any",
      "values": ["Logical Drive 0", "RAID", "Status", "状态"]
    },
    {
      "type": "region_stable",
      "selector": "body",
      "stable_for_ms": 1000,
      "sample_interval_ms": 250,
      "timeout_ms": 3000
    }
  ],
  "evidence_checkpoints": [
    {
      "name": "logical_drive_info",
      "type": "text_contains_any",
      "values": ["Logical Drive 0", "RAID", "Status", "状态"],
      "severity": "WARNING"
    }
  ]
}
```

## Validate

```bash
.venv/bin/python -m json.tool tasks.json >/tmp/bmc_tasks_json_check.json
.venv/bin/python -m pytest tests/test_condition_evaluator.py tests/test_task_ready_profiles.py tests/test_bmc_default_ready_conditions.py -q
```
