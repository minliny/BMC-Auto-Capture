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
    command_or_url: str = ""
    actions_json: str = ""
    rules_json: str = ""
    output_dir_template: str = "{device_name}/{task_name}"
    image_name_template: str = "{device_name}_{task_name}_{step}_{timestamp}"
    timeout_seconds: int = 60
    retry_count: int = 0
    enabled: bool = True
    full_screenshot: bool = False
    screenshot_mode: str = "auto"
    sequence_str: str = ""

    def parsed_rules(self) -> tuple[Rule, ...]:
        """Parse rules_json into Rule objects.

        Supports two formats:
        1. Simplified (v2): {"name":..., "desc":..., "checks":[...]}
           Each check: {"type":..., "target":..., "expect":..., "desc":...}
        2. Legacy (v1): {"rule_name":..., "rule_type":..., "actions":[...]}
        """
        if not self.rules_json.strip():
            return ()
        try:
            raw = json.loads(self.rules_json)
            items = raw if isinstance(raw, list) else [raw]
        except (json.JSONDecodeError, TypeError):
            return ()

        result: list[Rule] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Detect simplified format: has "checks" key
            if "checks" in item:
                result.append(self._parse_simplified_rule(item))
            else:
                result.append(Rule.from_dict(item))
        return tuple(result)

    @staticmethod
    def _parse_simplified_rule(item: dict) -> Rule:
        """Convert simplified rule format to internal Rule model."""
        checks = item.get("checks", [])
        actions: list[RuleAction] = []
        for c in checks:
            if not isinstance(c, dict):
                continue
            t = c.get("type", "")
            target = c.get("target", "")
            expect = c.get("expect", "")
            # Map simplified types to internal action_types
            if t == "text_exists":
                actions.append(RuleAction("assert_text", "", target))
            elif t == "text_not_exists":
                actions.append(RuleAction("assert_no_text", "", target))
            elif t == "element_exists":
                actions.append(RuleAction("assert_element", target, ""))
            elif t == "element_not_exists":
                actions.append(RuleAction("assert_no_element", target, ""))
            elif t == "element_text_is":
                # Combine: check element exists AND its text equals expect
                actions.append(RuleAction("assert_element", target, ""))
                actions.append(RuleAction("assert_element_text", target, expect))
            elif t == "element_text_contains":
                actions.append(RuleAction("assert_element", target, ""))
                actions.append(RuleAction("assert_text", "", expect))
            elif t in ("screenshot", "save_html", "save_txt",
                        "click", "fill", "wait_for", "wait_millis"):
                actions.append(RuleAction(t, target, expect))
            else:
                logger.warning("Unknown rule check type: %s", t)
        return Rule(
            rule_name=item.get("name", item.get("rule_name", "")),
            rule_type="advanced",  # Simplified rules are always validation-only
            enabled=item.get("enabled", True),
            actions=tuple(actions),
        )

    def basic_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.parsed_rules() if r.rule_type == "basic" and r.enabled)

    def advanced_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.parsed_rules() if r.rule_type == "advanced" and r.enabled)

    def has_advanced_rules(self) -> bool:
        return len(self.advanced_rules()) > 0

    def to_capture_flow(self) -> dict:
        """Unify BMC_URL / BMC_ACTIONS into a single capture flow dict.

        Returns dict with keys:
          - target_url: str
          - pre_capture_actions: list[dict]

        BMC_URL  → target_url from command_or_url, pre_capture_actions=[]
        BMC_ACTIONS → target_url from first goto action, pre_capture_actions=rest
        """
        if self.execution_mode == "BMC_URL":
            return {
                "target_url": self.command_or_url,
                "pre_capture_actions": [],
            }
        if self.execution_mode == "BMC_ACTIONS":
            return self._parse_actions_to_flow()
        return {"target_url": "", "pre_capture_actions": []}

    def _parse_actions_to_flow(self) -> dict:
        """Extract target_url + pre_capture_actions from actions_json."""
        import json
        try:
            actions = json.loads(self.actions_json) if self.actions_json else []
        except json.JSONDecodeError:
            return {"target_url": "", "pre_capture_actions": []}

        if not isinstance(actions, list):
            actions = [actions]

        target_url = ""
        pre_actions = []
        for a in actions:
            at = a.get("action", a.get("type", ""))
            if at == "goto" and not target_url:
                target_url = a.get("value", "")
            elif at in ("screenshot", "save_html"):
                # Downgrade to intermediate only
                a_copy = dict(a)
                a_copy["action"] = "intermediate_screenshot"
                pre_actions.append(a_copy)
            else:
                pre_actions.append(dict(a))

        return {
            "target_url": target_url,
            "pre_capture_actions": pre_actions,
        }
