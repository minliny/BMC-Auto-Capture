"""Supported RulePack schema and check capabilities."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SCHEMA_VERSION = "rulepack.v1"

RULE_CLASSES = (
    "stage_gate",
    "action_completion",
    "content_integrity",
    "evidence_validation",
)

PRIORITIES = ("P0", "P1", "P2")
RESULT_LAYERS = ("availability", "evidence_integrity", "business_match")
EFFECTS_ON_FINAL = ("fail", "partial", "warning", "none")
PROTOCOLS = ("BMC", "SSH", "TELNET")

BMC_READY_CHECKS = frozenset({
    "page_alive",
    "not_login_page",
    "url_contains",
    "url_not_contains",
    "selector_visible",
    "selector_enabled",
    "selector_hidden",
    "selector_not_visible",
    "selector_count_ge",
    "count_ge",
    "text_contains",
    "text_contains_any",
    "text_nonempty",
    "text_not_in",
    "region_stable",
    "active_tab_changed",
    "post_action_state_changed",
})

BMC_EVIDENCE_CHECKS = frozenset({
    "file_exists",
    "html_contains",
    "txt_contains",
    "text_contains",
    "text_contains_any",
    "text_not_contains",
    "not_contains_any",
    "regex_match",
    "regex_not_match",
})

SSH_RESULT_CHECKS = frozenset({
    "contains",
    "text_exists",
    "required_pattern",
    "required_patterns",
    "not_contains",
    "text_not_exists",
    "forbidden_pattern",
    "forbidden_patterns",
    "regex_exists",
    "regex_match",
    "regex_all_of",
    "regex_any_of",
    "regex_not_exists",
    "regex_not_match",
    "allowed_patterns",
    "min_output_lines",
    "min_body_lines",
    "command_echo_required",
    "prompt_required",
    "interface_status",
    "interface_status_not",
    "sentinel_seen",
    "exit_code_in",
    "pager_exhausted",
})

SSH_EVIDENCE_CHECKS = frozenset({
    "text_contains",
    "text_not_contains",
    "regex_match",
    "regex_not_match",
})

DEFAULT_EFFECT_BY_PRIORITY = {
    "P0": "fail",
    "P1": "partial",
    "P2": "warning",
}

DEFAULT_RESULT_LAYER_BY_CLASS = {
    "stage_gate": "availability",
    "action_completion": "availability",
    "content_integrity": "business_match",
    "evidence_validation": "evidence_integrity",
}

DEFAULT_STAGE_BY_CLASS = {
    "stage_gate": "READY_CHECK",
    "action_completion": "EXECUTION_CHECK",
    "content_integrity": "RESULT_CHECK",
    "evidence_validation": "ARTIFACT_CHECK",
}

CAPABILITY_REGISTRY: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "rule_classes": list(RULE_CLASSES),
    "priorities": list(PRIORITIES),
    "result_layers": list(RESULT_LAYERS),
    "effects_on_final": list(EFFECTS_ON_FINAL),
    "protocols": {
        "BMC": {
            "stage_gate": {
                "runtime_binding": "capture_ready_conditions",
                "check_types": sorted(BMC_READY_CHECKS),
            },
            "action_completion": {
                "runtime_binding": "capture_ready_conditions",
                "check_types": sorted(BMC_READY_CHECKS),
            },
            "content_integrity": {
                "runtime_binding": "capture_ready_conditions",
                "check_types": sorted(BMC_READY_CHECKS),
            },
            "evidence_validation": {
                "runtime_binding": "evidence_checkpoints",
                "check_types": sorted(BMC_EVIDENCE_CHECKS),
            },
        },
        "SSH": {
            "stage_gate": {
                "runtime_binding": "result_rules",
                "check_types": sorted({
                    "command_echo_required",
                    "prompt_required",
                    "min_body_lines",
                    "min_output_lines",
                }),
            },
            "action_completion": {
                "runtime_binding": "result_rules",
                "check_types": sorted({
                    "command_echo_required",
                    "prompt_required",
                    "sentinel_seen",
                    "exit_code_in",
                    "pager_exhausted",
                }),
            },
            "content_integrity": {
                "runtime_binding": "result_rules",
                "check_types": sorted(SSH_RESULT_CHECKS),
            },
            "evidence_validation": {
                "runtime_binding": "evidence_checkpoints",
                "check_types": sorted(SSH_EVIDENCE_CHECKS),
            },
        },
        "TELNET": {
            "stage_gate": {
                "runtime_binding": "result_rules",
                "check_types": sorted({
                    "command_echo_required",
                    "prompt_required",
                    "min_body_lines",
                    "min_output_lines",
                }),
            },
            "action_completion": {
                "runtime_binding": "result_rules",
                "check_types": sorted({
                    "command_echo_required",
                    "prompt_required",
                    "sentinel_seen",
                    "exit_code_in",
                    "pager_exhausted",
                }),
            },
            "content_integrity": {
                "runtime_binding": "result_rules",
                "check_types": sorted(SSH_RESULT_CHECKS),
            },
            "evidence_validation": {
                "runtime_binding": "evidence_checkpoints",
                "check_types": sorted(SSH_EVIDENCE_CHECKS),
            },
        },
    },
}


def get_rule_capabilities() -> dict[str, Any]:
    """Return a JSON-serializable copy of the RulePack capability registry."""
    return deepcopy(CAPABILITY_REGISTRY)


def allowed_check_types(protocol: str, rule_class: str) -> set[str]:
    protocol_key = normalize_protocol(protocol)
    class_key = str(rule_class or "")
    data = CAPABILITY_REGISTRY["protocols"].get(protocol_key, {}).get(class_key, {})
    return set(data.get("check_types", []))


def normalize_protocol(protocol: str) -> str:
    value = str(protocol or "").strip().upper()
    if value == "INBAND":
        return "SSH"
    return value


def default_effect(priority: str) -> str:
    return DEFAULT_EFFECT_BY_PRIORITY.get(str(priority or "").upper(), "partial")


def default_result_layer(rule_class: str) -> str:
    return DEFAULT_RESULT_LAYER_BY_CLASS.get(str(rule_class or ""), "business_match")


def default_stage(rule_class: str) -> str:
    return DEFAULT_STAGE_BY_CLASS.get(str(rule_class or ""), "RESULT_CHECK")


def severity_for_effect(priority: str, effect_on_final: str) -> str:
    priority_key = str(priority or "").upper()
    effect = str(effect_on_final or default_effect(priority_key)).lower()
    if priority_key == "P0" or effect == "fail":
        return "ERROR"
    if effect in {"partial", "warning"} or priority_key in {"P1", "P2"}:
        return "WARNING"
    return "INFO"
