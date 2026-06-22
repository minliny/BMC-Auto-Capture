---
name: bmc-auto-capture-ssh-output-rules
description: Generate, review, and patch BMC Auto-Capture SSH/TELNET RulePack JSON from command transcripts. Use for SSH_CMD or TELNET_CMD tasks, prompt/echo checks, content integrity, evidence validation, interface status validation, and converting real TXT evidence into rulepack.v1.
---

# SSH/TELNET Output Rule Authoring

Use this skill only for SSH/TELNET command-output rule authoring. Its output is a task-scoped `rulepack.v1`; the runtime adapter maps compatible rule classes into `result_rules` and `evidence_checkpoints`.

## Inspect First

Read these files before generating or editing rules:

```bash
sed -n '1,390p' src/rules/result_rules.py
sed -n '1,180p' src/rules/interface_status.py
sed -n '548,660p' src/executor/ssh_executor.py
sed -n '741,777p' src/executor/ssh_executor.py
sed -n '1,240p' src/rulepacks/capabilities.py
sed -n '99,115p' docs/rules_guide.md
```

Inspect the target task in `tasks.json` and the real output artifacts:

- full `*.txt` transcript
- `*.metadata.json`
- terminal screenshot only as supporting evidence

## Rule Class Decision

Use `stage_gate` for terminal prerequisites that can be checked in transcript form:

- output body exists
- prompt evidence is present when expected

Use `action_completion` for command completion evidence:

- command echo is present for interactive shell evidence
- final prompt is present for VRP/terminal-style evidence

Use `content_integrity` for business output:

- body line count
- literal/regex markers
- structured `interface_status`

Use `evidence_validation` for saved transcript evidence:

- transcript contains expected markers
- transcript does not contain session loss or pager leftovers

## Supported Checks

Generate only canonical check types for new RulePacks:

- `contains`, `text_exists`, `required_pattern`, `required_patterns`
- `not_contains`, `text_not_exists`
- `regex_exists`, `regex_match`
- `regex_all_of`
- `regex_any_of`
- `regex_not_exists`, `regex_not_match`
- `allowed_patterns`
- `min_output_lines`
- `min_body_lines`
- `command_echo_required`
- `prompt_required`
- `sentinel_seen`
- `exit_code_in`
- `pager_exhausted`
- `interface_status`, `interface_status_not`

If a task uses `display interface brief`, prefer `interface_status` over substring checks for `down`. The parser ignores command echo, prompts, headers, legends, and descriptions.

For `evidence_validation`, generate only saved-transcript checkpoint checks:

- `text_contains`
- `text_not_contains`
- `regex_match`
- `regex_not_match`

## Rule Choice

Use text checks for stable literal markers:

```json
{"type": "contains", "target": "EXPECTED", "desc": "输出包含预期标记"}
```

Use regex checks for tables or variable spacing:

```json
{"type": "regex_all_of", "patterns": ["PHY", "Protocol"]}
```

Use `min_body_lines` instead of `min_output_lines` when prompt, command echo, login banner, or blank lines should not count as evidence body.

Use `command_echo_required` and `prompt_required` only when the task expects terminal-style evidence. Do not use them for plain `exec_command` evidence unless echo/prompt are actually present.

Use `sentinel_seen` for explicit completion markers in the transcript, `exit_code_in` only when an exit-code marker is present, and `pager_exhausted` to reject leftover pager prompts such as `---- More ----`.

Use `source` when a check must target a specific stream:

- `source: "combined"` for the full transcript
- `source: "stdout"` for stdout-only checks when available
- `source: "stderr"` for stderr-only checks when available
- `source: "exit_code"` with `exit_code_in`

Do not generate `stderr_fail_patterns`, `stderr_allow_patterns`, `stderr_ignore_patterns`, `allow_exit_codes`, `ssh_rules`, raw `result_rules`, `checkpoints`, `forbidden_pattern`, or `forbidden_patterns`. Express those assertions inside the RulePack checks instead.

## Security

Never generate rules that include passwords, tokens, cookies, session IDs, Authorization headers, private keys, captcha values, or one-off secrets.

Avoid hardcoding device IPs, hostnames, serial numbers, timestamps, and prompt names unless the task explicitly validates identity.

## Output Contract

For one SSH/TELNET task, output:

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.xxx.v1",
  "task_id": "task.xxx",
  "protocol": "SSH",
  "execution_mode": "SSH_CMD",
  "audit_metadata": {
    "created_by": "bmc-auto-capture-ssh-output-rules",
    "created_from_artifacts": [],
    "artifact_hashes": {},
    "review_status": "generated"
  },
  "applies_to": {
    "task_ids": ["task.xxx"],
    "task_type": "SSH",
    "execution_modes": ["SSH_CMD"]
  },
  "rule_classes": {
    "stage_gate": [],
    "action_completion": [],
    "content_integrity": [],
    "evidence_validation": []
  }
}
```

When editing, write the RulePack to `config/rule_packs/ssh/{task_id}.json`.

## Example: Interface Status

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.019.v1",
  "task_id": "task.019",
  "protocol": "SSH",
  "execution_mode": "SSH_CMD",
  "rule_classes": {
    "stage_gate": [],
    "action_completion": [],
    "content_integrity": [
      {
        "rule_id": "ssh.interface_status_normal",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [
          {
            "type": "interface_status",
            "fields": ["physical", "protocol"],
            "forbidden": ["down"],
            "desc": "真实接口记录的 physical/protocol 状态不得为 down"
          }
        ]
      }
    ],
    "evidence_validation": []
  }
}
```

## Example: Transcript Shape

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.xxx.v1",
  "task_id": "task.xxx",
  "protocol": "SSH",
  "execution_mode": "SSH_CMD",
  "rule_classes": {
    "stage_gate": [],
    "action_completion": [
      {
        "rule_id": "ssh.command_completed",
        "priority": "P0",
        "effect_on_final": "fail",
        "checks": [
          {"type": "command_echo_required"},
          {"type": "prompt_required"},
          {"type": "exit_code_in", "source": "exit_code", "allowed": [0]}
        ]
      }
    ],
    "content_integrity": [
      {
        "rule_id": "ssh.output_shape",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [
          {"type": "min_body_lines", "target": "2"},
          {"type": "regex_any_of", "patterns": ["(?i)success", "(?i)normal", "up"]},
          {"type": "regex_not_match", "source": "stderr", "pattern": "(?i)(permission denied|command not found)"}
        ]
      }
    ],
    "evidence_validation": []
  }
}
```

## Validate

```bash
.venv/bin/python -m json.tool tasks.json >/tmp/bmc_tasks_json_check.json
.venv/bin/python -m pytest tests/test_rulepacks.py tests/test_result_rules.py tests/test_ssh_interface_status_rules.py tests/test_ssh_command_resolution.py -q
```
