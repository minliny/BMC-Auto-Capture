---
name: bmc-auto-capture-rule-authoring
description: Route BMC Auto-Capture rule authoring requests to the correct protocol-specific workflow. Use when generating, reviewing, or patching tasks.json rules from real BMC/SSH evidence and the task may involve BMC capture_ready_conditions/evidence_checkpoints, SSH/TELNET result_rules, or mixed protocol tasks.
---

# BMC Auto-Capture Rule Authoring

Use this as the total entrypoint. It decides whether to use the BMC page rules skill, the SSH output rules skill, or both.

## Route First

Identify each target task by `task_id`, `task_type`, `execution_mode`, and evidence path.

- For `task_type: "BMC"` or `execution_mode` in `BMC_URL` / `BMC_ACTIONS`, read and follow the sibling skill at `../bmc-auto-capture-bmc-page-rules/SKILL.md`.
- For `task_type: "SSH"` / `"TELNET"` or `execution_mode` in `SSH_CMD` / `TELNET_CMD`, read and follow the sibling skill at `../bmc-auto-capture-ssh-output-rules/SKILL.md`.
- For mixed requests, process task groups separately and return one patch-ready result per task.

Do not use SSH `result_rules` for BMC page readiness. Do not use BMC `capture_ready_conditions` for SSH command output.

## Required Repo Checks

Before editing, inspect the current checkout because rule support may drift:

```bash
git status --short --branch
rg -n "result_rules|capture_ready_conditions|evidence_checkpoints" tasks.json src/rules src/executor docs
```

Read the target task entry in `tasks.json` before proposing changes. Preserve unrelated fields and task order.

## Evidence Preference

Prefer real execution evidence over guesses:

- BMC: `html/*.state.json`, `html/*.html`, `html/*.evidence.html`, `html/*.metadata.json`, then screenshot.
- SSH/TELNET: `*.txt`, `*.metadata.json`, then terminal screenshot.

Never infer exact business assertions from screenshot pixels alone when text evidence exists.

## Output Contract

When the user asks for JSON only, return only JSON. Otherwise summarize assumptions and include patch-ready fragments.

For direct edits, modify only the target task entries in `tasks.json`, then run:

```bash
.venv/bin/python -m json.tool tasks.json >/tmp/bmc_tasks_json_check.json
.venv/bin/python -m pytest tests/test_result_rules.py tests/test_condition_evaluator.py tests/test_task_ready_profiles.py tests/test_bmc_default_ready_conditions.py -q
```

If a requested assertion needs an unsupported rule type, stop and say that executor code must be extended first. Do not invent unsupported JSON keys.
