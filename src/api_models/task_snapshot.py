"""
TaskSnapshot — the task definition embedded in a Job's task_snapshot field.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskRuleCheck:
    type: str = ""
    target: str = ""
    expect: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "target": self.target, "expect": self.expect}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskRuleCheck":
        return cls(
            type=d.get("type", ""),
            target=d.get("target", ""),
            expect=d.get("expect", ""),
        )


@dataclass
class TaskRule:
    rule_name: str = ""
    rule_type: str = "advanced"
    enabled: bool = True
    checks: list[TaskRuleCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "rule_type": self.rule_type,
            "enabled": self.enabled,
            "checks": [c.to_dict() for c in self.checks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskRule":
        checks = [TaskRuleCheck.from_dict(c) for c in d.get("checks", [])]
        return cls(
            rule_name=d.get("rule_name", ""),
            rule_type=d.get("rule_type", "advanced"),
            enabled=bool(d.get("enabled", True)),
            checks=checks,
        )


@dataclass
class TaskSnapshot:
    task_id: str
    task_name: str = ""
    task_type: str = ""  # BMC | SSH
    execution_mode: str = ""  # BMC_URL | BMC_ACTIONS | SSH_CMD
    match_group: str = ""
    command_or_url: str = ""
    actions_json: str = ""
    rules_json: str = ""  # legacy rules_json compatibility; do not generate for new rules
    rules: list[TaskRule] = field(default_factory=list)  # legacy rules compatibility
    result_rules: list[dict[str, Any]] = field(default_factory=list)  # runtime adapter output
    ssh_rules: list[dict[str, Any]] = field(default_factory=list)  # legacy alias for result_rules
    task_def: dict[str, Any] = field(default_factory=dict)
    output_dir_template: str = "{device_name}/{task_name}"
    image_name_template: str = "{device_name}_{task_name}_{step}_{timestamp}"
    timeout_seconds: int = 60
    retry_count: int = 0
    full_screenshot: bool = False
    screenshot_mode: str = "auto"
    ssh_profile: str = ""  # linux | vrp
    ssh_evidence_mode: str = ""  # terminal | structured
    ssh_transport: str = ""  # optional internal override
    artifact_profile: str = ""  # full | fast, BMC only
    per_group_timeout_seconds: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_type": self.task_type,
            "execution_mode": self.execution_mode,
            "match_group": self.match_group,
            "command_or_url": self.command_or_url,
            "actions_json": self.actions_json,
            "output_dir_template": self.output_dir_template,
            "image_name_template": self.image_name_template,
            "timeout_seconds": self.timeout_seconds,
            "retry_count": self.retry_count,
            "full_screenshot": self.full_screenshot,
            "screenshot_mode": self.screenshot_mode,
        }
        if self.rules_json:
            data["rules_json"] = self.rules_json
        if self.rules:
            data["rules"] = [r.to_dict() for r in self.rules]
        if self.ssh_profile:
            data["ssh_profile"] = self.ssh_profile
        if self.ssh_evidence_mode:
            data["ssh_evidence_mode"] = self.ssh_evidence_mode
        if self.ssh_transport:
            data["ssh_transport"] = self.ssh_transport
        if self.artifact_profile:
            data["artifact_profile"] = self.artifact_profile
        if self.result_rules:
            data["result_rules"] = list(self.result_rules)
        if self.ssh_rules:
            data["ssh_rules"] = list(self.ssh_rules)
        if self.task_def:
            data["task_def"] = dict(self.task_def)
        if self.per_group_timeout_seconds:
            data["per_group_timeout_seconds"] = dict(self.per_group_timeout_seconds)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSnapshot":
        rules = [TaskRule.from_dict(r) for r in d.get("rules", [])]
        return cls(
            task_id=d["task_id"],
            task_name=d.get("task_name", ""),
            task_type=d.get("task_type", ""),
            execution_mode=d.get("execution_mode", ""),
            match_group=d.get("match_group", ""),
            command_or_url=d.get("command_or_url", ""),
            actions_json=d.get("actions_json", ""),
            rules_json=d.get("rules_json", ""),
            rules=rules,
            result_rules=list(d.get("result_rules", []) or []),
            ssh_rules=list(d.get("ssh_rules", []) or []),
            task_def=dict(d.get("task_def", {}) or {}),
            output_dir_template=d.get("output_dir_template", "{device_name}/{task_name}"),
            image_name_template=d.get("image_name_template", "{device_name}_{task_name}_{step}_{timestamp}"),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            retry_count=int(d.get("retry_count", 0)),
            full_screenshot=bool(d.get("full_screenshot", False)),
            screenshot_mode=d.get("screenshot_mode", "auto"),
            ssh_profile=d.get("ssh_profile", ""),
            ssh_evidence_mode=d.get("ssh_evidence_mode", ""),
            ssh_transport=d.get("ssh_transport", ""),
            artifact_profile=d.get("artifact_profile", ""),
            per_group_timeout_seconds=dict(d.get("per_group_timeout_seconds", {}) or {}),
        )
