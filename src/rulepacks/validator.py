"""RulePack schema and capability validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .capabilities import (
    EFFECTS_ON_FINAL,
    PRIORITIES,
    RESULT_LAYERS,
    RULE_CLASSES,
    SCHEMA_VERSION,
    allowed_check_types,
    default_effect,
    default_result_layer,
    default_stage,
    normalize_protocol,
)
from .fingerprint import task_fingerprints


@dataclass
class RulePackValidationMessage:
    severity: str
    code: str
    message: str
    field: str = ""
    source: str = "rulepack.validation"

    def to_dict(self) -> dict[str, str]:
        data = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }
        if self.field:
            data["field"] = self.field
        return data


@dataclass
class RulePackValidationReport:
    valid: bool = True
    errors: list[RulePackValidationMessage] = field(default_factory=list)
    warnings: list[RulePackValidationMessage] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)

    def add_error(self, code: str, message: str, field: str = "") -> None:
        self.valid = False
        self.errors.append(RulePackValidationMessage("ERROR", code, message, field))

    def add_warning(self, code: str, message: str, field: str = "") -> None:
        self.warnings.append(RulePackValidationMessage("WARNING", code, message, field))

    def to_dict(self, include_normalized: bool = True) -> dict[str, Any]:
        data = {
            "valid": self.valid,
            "errors": [m.to_dict() for m in self.errors],
            "warnings": [m.to_dict() for m in self.warnings],
        }
        if include_normalized:
            data["rulePack"] = self.normalized
        return data


def validate_rule_pack(
    raw: dict[str, Any],
    *,
    task_def: dict[str, Any] | None = None,
) -> RulePackValidationReport:
    """Validate and normalize a RulePack.

    The validator checks structure, task binding metadata, and whether every
    check type is supported by the current runtime capability registry.  It
    does not validate business selector/regex correctness; skills must derive
    those values from real artifacts.
    """
    report = RulePackValidationReport()
    if not isinstance(raw, dict):
        report.add_error("RULEPACK_NOT_OBJECT", "RulePack must be a JSON object")
        return report

    pack = dict(raw)
    schema_version = str(pack.get("schema_version", ""))
    if schema_version != SCHEMA_VERSION:
        report.add_error(
            "RULEPACK_SCHEMA_VERSION_UNSUPPORTED",
            f"schema_version must be {SCHEMA_VERSION}",
            "schema_version",
        )

    protocol = normalize_protocol(pack.get("protocol", ""))
    if protocol not in {"BMC", "SSH", "TELNET"}:
        report.add_error("RULEPACK_PROTOCOL_INVALID", "protocol must be BMC, SSH, or TELNET", "protocol")

    task_id = str(pack.get("task_id", "") or "")
    if not task_id:
        report.add_error("RULEPACK_TASK_ID_REQUIRED", "task_id is required", "task_id")

    pack.setdefault("audit_metadata", {})
    if not isinstance(pack["audit_metadata"], dict):
        report.add_error("RULEPACK_AUDIT_METADATA_INVALID", "audit_metadata must be an object", "audit_metadata")
        pack["audit_metadata"] = {}
    pack["audit_metadata"].setdefault("review_status", "generated")
    pack["audit_metadata"].setdefault("created_from_artifacts", [])
    pack["audit_metadata"].setdefault("artifact_hashes", {})

    applies_to = pack.setdefault("applies_to", {})
    if not isinstance(applies_to, dict):
        report.add_error("RULEPACK_APPLIES_TO_INVALID", "applies_to must be an object", "applies_to")
        applies_to = {}
        pack["applies_to"] = applies_to
    applies_to.setdefault("task_ids", [task_id] if task_id else [])
    applies_to.setdefault("task_type", protocol)
    if pack.get("execution_mode"):
        applies_to.setdefault("execution_modes", [pack.get("execution_mode")])

    final_policy = pack.setdefault("final_policy", {})
    if not isinstance(final_policy, dict):
        report.add_error("RULEPACK_FINAL_POLICY_INVALID", "final_policy must be an object", "final_policy")
        final_policy = {}
        pack["final_policy"] = final_policy
    final_policy.setdefault("p0_failed", "FAIL")
    final_policy.setdefault("p1_failed", "WARN")
    final_policy.setdefault("p2_failed", "WARN")

    pack.setdefault("evidence_requirements", {})
    pack.setdefault("capability_requirements", {})

    _validate_task_binding(report, pack, task_def)

    raw_classes = pack.setdefault("rule_classes", {})
    if not isinstance(raw_classes, dict):
        report.add_error("RULEPACK_RULE_CLASSES_INVALID", "rule_classes must be an object", "rule_classes")
        raw_classes = {}
        pack["rule_classes"] = raw_classes

    normalized_classes: dict[str, list[dict[str, Any]]] = {}
    for rule_class in RULE_CLASSES:
        raw_rules = raw_classes.get(rule_class, [])
        if raw_rules in (None, ""):
            raw_rules = []
        if not isinstance(raw_rules, list):
            report.add_error(
                "RULEPACK_RULE_CLASS_NOT_LIST",
                f"rule_classes.{rule_class} must be a list",
                f"rule_classes.{rule_class}",
            )
            raw_rules = []
        normalized_classes[rule_class] = [
            _normalize_rule(report, rule, protocol, rule_class, index)
            for index, rule in enumerate(raw_rules)
            if isinstance(rule, dict) or _record_non_object_rule(report, rule_class, index)
        ]
    pack["rule_classes"] = normalized_classes

    report.normalized = pack
    return report


def _record_non_object_rule(report: RulePackValidationReport, rule_class: str, index: int) -> bool:
    report.add_error(
        "RULEPACK_RULE_NOT_OBJECT",
        "Each rule must be a JSON object",
        f"rule_classes.{rule_class}.{index}",
    )
    return False


def _normalize_rule(
    report: RulePackValidationReport,
    rule: dict[str, Any],
    protocol: str,
    rule_class: str,
    index: int,
) -> dict[str, Any]:
    data = dict(rule)
    field_base = f"rule_classes.{rule_class}.{index}"

    rule_id = str(data.get("rule_id") or data.get("name") or "")
    if not rule_id:
        rule_id = f"{rule_class}.{index + 1}"
        report.add_warning("RULEPACK_RULE_ID_DEFAULTED", f"rule_id defaulted to {rule_id}", field_base)
    data["rule_id"] = rule_id
    data["rule_class"] = rule_class
    data["enabled"] = bool(data.get("enabled", True))

    priority = str(data.get("priority") or "P1").upper()
    if priority not in PRIORITIES:
        report.add_error("RULEPACK_PRIORITY_INVALID", "priority must be P0, P1, or P2", f"{field_base}.priority")
        priority = "P1"
    data["priority"] = priority

    effect = str(data.get("effect_on_final") or default_effect(priority)).lower()
    if effect not in EFFECTS_ON_FINAL:
        report.add_error(
            "RULEPACK_EFFECT_INVALID",
            "effect_on_final must be fail, partial, warning, or none",
            f"{field_base}.effect_on_final",
        )
        effect = default_effect(priority)
    data["effect_on_final"] = effect

    result_layer = str(data.get("result_layer") or default_result_layer(rule_class))
    if result_layer not in RESULT_LAYERS:
        report.add_error(
            "RULEPACK_RESULT_LAYER_INVALID",
            "result_layer must be availability, evidence_integrity, or business_match",
            f"{field_base}.result_layer",
        )
        result_layer = default_result_layer(rule_class)
    data["result_layer"] = result_layer
    data["stage"] = str(data.get("stage") or default_stage(rule_class))

    checks = data.get("checks", [])
    if not isinstance(checks, list):
        report.add_error("RULEPACK_CHECKS_NOT_LIST", "checks must be a list", f"{field_base}.checks")
        checks = []

    allowed = allowed_check_types(protocol, rule_class)
    normalized_checks: list[dict[str, Any]] = []
    for check_index, check in enumerate(checks):
        check_field = f"{field_base}.checks.{check_index}"
        if not isinstance(check, dict):
            report.add_error("RULEPACK_CHECK_NOT_OBJECT", "Each check must be an object", check_field)
            continue
        check_data = dict(check)
        check_type = str(check_data.get("type") or check_data.get("action_type") or "")
        if not check_type:
            report.add_error("RULEPACK_CHECK_TYPE_REQUIRED", "check.type is required", f"{check_field}.type")
        elif check_type not in allowed:
            report.add_error(
                "RULEPACK_CHECK_TYPE_UNSUPPORTED",
                f"{protocol} {rule_class} does not support check type {check_type!r}",
                f"{check_field}.type",
            )
        normalized_checks.append(check_data)
    data["checks"] = normalized_checks
    return data


def _validate_task_binding(
    report: RulePackValidationReport,
    pack: dict[str, Any],
    task_def: dict[str, Any] | None,
) -> None:
    if task_def is None:
        return

    task_id = str(pack.get("task_id") or "")
    applies_to = pack.get("applies_to", {}) or {}
    task_ids = [str(v) for v in applies_to.get("task_ids", [])]
    actual_task_id = str(task_def.get("task_id") or task_def.get("_config_key") or "")
    if task_ids and actual_task_id and actual_task_id not in task_ids and task_id != actual_task_id:
        report.add_error(
            "RULEPACK_TASK_ID_MISMATCH",
            f"RulePack task_ids {task_ids} do not match task {actual_task_id}",
            "applies_to.task_ids",
        )

    expected_task_type = str(applies_to.get("task_type") or pack.get("protocol") or "").upper()
    actual_task_type = str(task_def.get("task_type") or "").upper()
    if expected_task_type and actual_task_type and expected_task_type != actual_task_type:
        report.add_error(
            "RULEPACK_TASK_TYPE_MISMATCH",
            f"RulePack task_type {expected_task_type} does not match task_type {actual_task_type}",
            "applies_to.task_type",
        )

    expected_modes = [str(v) for v in applies_to.get("execution_modes", [])]
    actual_mode = str(task_def.get("execution_mode") or "")
    if expected_modes and actual_mode and actual_mode not in expected_modes:
        report.add_error(
            "RULEPACK_EXECUTION_MODE_MISMATCH",
            f"RulePack execution_modes {expected_modes} do not match execution_mode {actual_mode}",
            "applies_to.execution_modes",
        )

    fingerprints = task_fingerprints(task_def)
    for key in ("command_fingerprint", "route_fingerprint", "actions_fingerprint"):
        expected = str(applies_to.get(key) or "")
        if expected and expected != fingerprints.get(key):
            report.add_error(
                "RULEPACK_FINGERPRINT_MISMATCH",
                f"{key} does not match task definition",
                f"applies_to.{key}",
            )
