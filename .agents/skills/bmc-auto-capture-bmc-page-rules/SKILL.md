---
name: bmc-auto-capture-bmc-page-rules
description: Generate, review, and patch BMC Auto-Capture BMC RulePack JSON for BMC_URL and BMC_ACTIONS tasks. Use when filling stage_gate, action_completion, content_integrity, and evidence_validation rule classes from real BMC HTML, state JSON, MHTML, screenshot, and metadata artifacts.
---

# BMC Page Rule Authoring

Use this skill only for BMC page capture and evidence rules. Its output is a task-scoped `rulepack.v1`; the runtime adapter maps BMC RulePack classes into `capture_ready_conditions` and `evidence_checkpoints`.

## Inspect First

Read these files before generating or editing rules:

```bash
sed -n '1,130p' src/rules/condition_evaluator.py
sed -n '190,430p' src/rules/condition_evaluator.py
sed -n '1228,1288p' src/executor/bmc_executor.py
sed -n '1568,1635p' src/executor/bmc_executor.py
sed -n '1773,1888p' src/executor/bmc_executor.py
sed -n '1,240p' src/rulepacks/capabilities.py
sed -n '270,360p' docs/TASK_ADDING_GUIDE.md
```

Inspect the target task in `tasks.json` and the real output artifacts:

- `html/*.state.json`
- `html/*.html`
- `html/*.evidence.html`
- `html/*.metadata.json`
- Screenshot only as supporting evidence

## Rule Class Decision

Use `stage_gate` for whether the page can continue:

- route reached
- login page not visible
- error/session-expired/overlay absent
- target container visible

Use `action_completion` for post-action state:

- click/tab/expand resulted in the expected active state
- detail area belongs to the clicked object
- post-action loading disappeared

Use `content_integrity` for screenshot-ready business content:

- key container visible
- key fields are non-empty
- loading placeholders are gone
- dynamic region is stable

Use `evidence_validation` for saved evidence after final capture:

- required business text appears in saved evidence
- forbidden status/error text is absent
- regex pattern matches evidence text
- expected artifact exists

Do not invent concrete selectors or text from the task name. Use only observed HTML/state/metadata evidence.

## Supported Ready Conditions

Generate only condition types the executor supports:

- `url_contains`, `url_not_contains`
- `selector_visible`, `selector_hidden`, `selector_not_visible`
- `selector_count_ge`, `count_ge`
- `text_contains`, `text_contains_any`
- `text_nonempty`
- `text_not_in`
- `region_stable`
- `active_tab_changed`
- `post_action_state_changed`

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
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.xxx.v1",
  "task_id": "task.xxx",
  "protocol": "BMC",
  "execution_mode": "BMC_URL",
  "audit_metadata": {
    "created_by": "bmc-auto-capture-bmc-page-rules",
    "created_from_artifacts": [],
    "artifact_hashes": {},
    "review_status": "generated"
  },
  "applies_to": {
    "task_ids": ["task.xxx"],
    "task_type": "BMC",
    "execution_modes": ["BMC_URL"]
  },
  "rule_classes": {
    "stage_gate": [],
    "action_completion": [],
    "content_integrity": [],
    "evidence_validation": []
  }
}
```

When editing, write the RulePack to `config/rule_packs/bmc/{task_id}.json`.

## Example

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.029.v1",
  "task_id": "task.029",
  "protocol": "BMC",
  "execution_mode": "BMC_ACTIONS",
  "rule_classes": {
    "stage_gate": [
      {
        "rule_id": "bmc.storage.route_reached",
        "priority": "P0",
        "effect_on_final": "fail",
        "checks": [{"type": "url_contains", "target": "/navigate/system/storage"}]
      }
    ],
    "action_completion": [],
    "content_integrity": [
      {
        "rule_id": "bmc.storage.logical_drive_ready",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [
          {"type": "selector_visible", "selector": "#LogicalDrive0"},
          {"type": "text_nonempty", "selector": "#LogicalDrive0"},
          {"type": "region_stable", "selector": "body", "stable_for_ms": 1000}
        ]
      }
    ],
    "evidence_validation": [
      {
        "rule_id": "bmc.storage.logical_drive_evidence",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [{"type": "text_contains_any", "values": ["Logical Drive 0", "RAID", "Status"]}]
      }
    ]
  }
}
```

## Validate

```bash
.venv/bin/python -m json.tool tasks.json >/tmp/bmc_tasks_json_check.json
.venv/bin/python -m pytest tests/test_rulepacks.py tests/test_condition_evaluator.py tests/test_task_ready_profiles.py tests/test_bmc_default_ready_conditions.py -q
```
