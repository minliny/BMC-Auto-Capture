---
name: bmc-auto-capture-rule-authoring
description: Route BMC Auto-Capture RulePack authoring requests to the correct protocol-specific workflow. Use when generating, reviewing, or patching rulepack.v1 JSON from real BMC/SSH evidence for stage_gate, action_completion, content_integrity, and evidence_validation rule classes.
---

# BMC Auto-Capture Rule Authoring

Use this as the total entrypoint. It decides whether to use the BMC page rules skill, the SSH output rules skill, or both. Outputs must be task-scoped `rulepack.v1` JSON, not direct edits to `tasks.json` rule fields.

## Route First

Identify each target task by `task_id`, `task_type`, `execution_mode`, and evidence path.

- For `task_type: "BMC"` or `execution_mode` in `BMC_URL` / `BMC_ACTIONS`, read and follow the sibling skill at `../bmc-auto-capture-bmc-page-rules/SKILL.md`.
- For `task_type: "SSH"` / `"TELNET"` or `execution_mode` in `SSH_CMD` / `TELNET_CMD`, read and follow the sibling skill at `../bmc-auto-capture-ssh-output-rules/SKILL.md`.
- For mixed requests, process task groups separately and return one patch-ready result per task.

Do not use SSH output checks for BMC page readiness. Do not use BMC page checks for SSH command output. The current runtime adapter maps valid RulePacks into executor fields.

## Required Repo Checks

Before editing, inspect the current checkout because rule support may drift:

```bash
git status --short --branch
rg -n "result_rules|capture_ready_conditions|evidence_checkpoints" tasks.json src/rules src/executor docs
sed -n '1,240p' src/rulepacks/capabilities.py
```

Read the target task entry in `tasks.json` before proposing changes. Match RulePacks by `task_id`; `task_name` is display-only.

## Evidence Preference

Prefer real execution evidence over guesses:

- BMC: `html/*.state.json`, `html/*.html`, `html/*.evidence.html`, `html/*.metadata.json`, then screenshot.
- SSH/TELNET: `*.txt`, `*.metadata.json`, then terminal screenshot.

Never infer exact business assertions from screenshot pixels alone when text evidence exists.

## Output Contract

When the user asks for JSON only, return only JSON. Otherwise summarize evidence assumptions and include a validate-ready RulePack.

For one task, output:

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.xxx.v1",
  "task_id": "task.xxx",
  "protocol": "BMC",
  "execution_mode": "BMC_URL",
  "audit_metadata": {
    "created_by": "bmc-auto-capture-rule-authoring",
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

For direct edits, write only `config/rule_packs/{protocol}/{task_id}.json`, then run:

```bash
.venv/bin/python -m pytest tests/test_rulepacks.py tests/test_result_rules.py tests/test_condition_evaluator.py -q
```

If a requested assertion needs an unsupported rule type, stop and say that executor code must be extended first. Do not invent unsupported JSON keys.
