"""
Task + Rule + RuleAction models.
"""


from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import json


@dataclass(frozen=True)
class RuleAction:
    action_type: str
    selector: str = ""
    value: str = ""
    timeout_seconds: int = 10

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleAction":
        return cls(
            action_type=str(d.get("action_type", "")),
            selector=str(d.get("selector", "")),
            value=str(d.get("value", "")),
            timeout_seconds=int(d.get("timeout_seconds", 10)),
        )


@dataclass(frozen=True)
class Rule:
    rule_name: str
    rule_type: str  # "basic" | "advanced"
    enabled: bool = True
    actions: tuple[RuleAction, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        raw_actions = d.get("actions", [])
        actions = tuple(RuleAction.from_dict(a) for a in raw_actions)
        return cls(
            rule_name=str(d.get("rule_name", "")),
            rule_type=str(d.get("rule_type", "basic")),
            enabled=bool(d.get("enabled", True)),
            actions=actions,
        )


@dataclass(frozen=True)
class Task:
    row_index: int
    sequence: int
    task_name: str
    task_type: str  # "BMC" | "SSH" | "TELNET"
    execution_mode: str  # "BMC_URL" | "BMC_ACTIONS" | "SSH_CMD" | "TELNET_CMD"
    match_group: str = ""
    match_tags: tuple[str, ...] = ()
    command_or_url: str = ""
    actions_json: str = ""
    rules_json: str = ""
    output_dir_template: str = "{device_name}/{task_name}"
    image_name_template: str = "{device_name}_{task_name}_{step}_{timestamp}"
    timeout_seconds: int = 60
    retry_count: int = 0
    enabled: bool = True

    def parsed_rules(self) -> tuple[Rule, ...]:
        """Parse rules_json into Rule objects. Returns empty tuple on failure."""
        if not self.rules_json.strip():
            return ()
        try:
            raw = json.loads(self.rules_json)
            items = raw if isinstance(raw, list) else [raw]
            return tuple(Rule.from_dict(r) for r in items)
        except (json.JSONDecodeError, TypeError, KeyError):
            return ()

    def basic_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.parsed_rules() if r.rule_type == "basic" and r.enabled)

    def advanced_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.parsed_rules() if r.rule_type == "advanced" and r.enabled)

    def has_advanced_rules(self) -> bool:
        return len(self.advanced_rules()) > 0
