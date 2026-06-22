# RulePack Framework

`rulepack.v1` is the task-scoped audit rule package format. It defines rule
classes and audit metadata. It does not prescribe concrete selectors, text
markers, or regex patterns; those values must be generated from real BMC/SSH
evidence by the matching authoring skill.

## Rule Classes

| Rule class | Purpose | Current runtime binding |
|---|---|---|
| `stage_gate` | Whether execution or evidence capture can continue. | BMC `capture_ready_conditions`; SSH `result_rules` for supported transcript checks. |
| `action_completion` | Whether a click, tab switch, expansion, or command completed. | BMC `capture_ready_conditions`; SSH `result_rules`. |
| `content_integrity` | Whether page content or terminal output is complete and credible. | BMC `capture_ready_conditions`; SSH `result_rules`. |
| `evidence_validation` | Whether saved artifacts support the conclusion. | BMC `evidence_checkpoints`; SSH `checkpoints`. |

## RulePack Shape

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.019.v1",
  "task_id": "task.019",
  "protocol": "SSH",
  "execution_mode": "SSH_CMD",
  "audit_metadata": {
    "created_by": "bmc-auto-capture-ssh-output-rules",
    "created_from_artifacts": [],
    "artifact_hashes": {},
    "review_status": "generated"
  },
  "applies_to": {
    "task_ids": ["task.019"],
    "task_type": "SSH",
    "execution_modes": ["SSH_CMD"],
    "command_fingerprint": "sha256:..."
  },
  "final_policy": {
    "p0_failed": "FAIL",
    "p1_failed": "WARN",
    "p2_failed": "WARN"
  },
  "rule_classes": {
    "stage_gate": [],
    "action_completion": [],
    "content_integrity": [],
    "evidence_validation": []
  },
  "evidence_requirements": {},
  "capability_requirements": {}
}
```

## Matching

RulePacks are matched by `task_id` and then checked against task metadata:

- `task_type`
- `execution_mode`
- optional `command_fingerprint`
- optional `route_fingerprint`
- optional `actions_fingerprint`

`task_name` is display text only and must not be used for automatic matching.

## API

RulePacks are managed through the current Executor API config surface:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/executor/v1/config/rule-capabilities` | Return supported rule classes and check types. |
| `POST` | `/executor/v1/config/rule-packs:validate` | Validate a RulePack without writing it. |
| `POST` | `/executor/v1/config/rule-packs:import` | Import one or more RulePacks. |
| `GET` | `/executor/v1/config/rule-packs` | List stored RulePacks. |
| `GET` | `/executor/v1/config/rule-packs/{task_id}` | Read one task RulePack. |
| `PUT` | `/executor/v1/config/rule-packs/{task_id}` | Replace one task RulePack. |

Stored RulePacks live under `config/rule_packs/{protocol}/{task_id}.json`.

## Skill Contract

Skills fill task-specific rule instances from real evidence:

- BMC skill reads state JSON, HTML/MHTML, metadata, and screenshots.
- SSH skill reads full TXT transcripts and metadata.
- The router skill dispatches by `task_id`, `task_type`, and `execution_mode`.

If evidence is missing, a skill should return a missing-evidence response
instead of guessing selectors, text markers, or regex patterns.
