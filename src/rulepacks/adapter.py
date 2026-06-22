"""Adapters from RulePack classes to the current executor rule fields."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .capabilities import normalize_protocol, severity_for_effect
from .store import RulePackStore
from .validator import validate_rule_pack


def adapt_rule_pack_to_task_def(rule_pack: dict[str, Any], task_def: dict[str, Any]) -> dict[str, Any]:
    """Return task_def with a validated RulePack merged into runtime fields."""
    report = validate_rule_pack(rule_pack, task_def=task_def)
    if not report.valid:
        raise ValueError("; ".join(m.message for m in report.errors))

    pack = report.normalized
    protocol = normalize_protocol(pack.get("protocol"))
    merged = deepcopy(task_def)
    merged["rule_pack"] = pack

    if protocol == "BMC":
        ready_conditions: list[dict[str, Any]] = []
        for rule_class in ("stage_gate", "action_completion", "content_integrity"):
            ready_conditions.extend(_checks_with_metadata(pack, rule_class))
        if ready_conditions:
            merged["capture_ready_conditions"] = ready_conditions

        evidence = _checks_with_metadata(pack, "evidence_validation")
        if evidence:
            merged["evidence_checkpoints"] = [
                _checkpoint_check_from_rule_check(c, index)
                for index, c in enumerate(evidence)
            ]
        return merged

    if protocol in {"SSH", "TELNET"}:
        result_rules: list[dict[str, Any]] = []
        for rule_class in ("stage_gate", "action_completion", "content_integrity"):
            result_rules.extend(_result_rules_from_class(pack, rule_class))
        if result_rules:
            merged["result_rules"] = result_rules

        checkpoints = _checks_with_metadata(pack, "evidence_validation")
        if checkpoints:
            merged["evidence_checkpoints"] = [
                _checkpoint_check_from_rule_check(c, index)
                for index, c in enumerate(checkpoints)
            ]
        return merged

    return merged


def merge_rule_packs_into_task_defs(
    task_defs: dict[str, dict],
    *,
    workspace_root: str | None = None,
) -> dict[str, dict]:
    """Merge task-specific RulePacks into loaded tasks.json definitions."""
    store = RulePackStore(workspace_root=workspace_root)
    merged: dict[str, dict] = {}
    by_object_id: dict[int, dict] = {}

    for key, raw in task_defs.items():
        if not isinstance(raw, dict):
            continue
        obj_id = id(raw)
        if obj_id in by_object_id:
            merged[key] = by_object_id[obj_id]
            continue

        tdef = dict(raw)
        task_id = str(tdef.get("task_id") or key)
        pack = store.get(task_id)
        if pack is None:
            merged[key] = tdef
            by_object_id[obj_id] = tdef
            continue
        try:
            adapted = adapt_rule_pack_to_task_def(pack, tdef)
        except ValueError:
            adapted = tdef
        merged[key] = adapted
        by_object_id[obj_id] = adapted
    return merged


def _checks_with_metadata(pack: dict[str, Any], rule_class: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for rule in pack.get("rule_classes", {}).get(rule_class, []) or []:
        if rule.get("enabled", True) is False:
            continue
        for check in rule.get("checks", []) or []:
            item = dict(check)
            item.setdefault("rule_id", rule.get("rule_id", ""))
            item.setdefault("rule_class", rule_class)
            item.setdefault("priority", rule.get("priority", "P1"))
            item.setdefault("result_layer", rule.get("result_layer", ""))
            item.setdefault("effect_on_final", rule.get("effect_on_final", "partial"))
            item.setdefault("severity", severity_for_effect(
                item.get("priority", "P1"),
                item.get("effect_on_final", "partial"),
            ))
            item.setdefault("source", "rulepack")
            return_name = item.get("name") or rule.get("rule_id")
            if return_name:
                item.setdefault("name", return_name)
            checks.append(item)
    return checks


def _result_rules_from_class(pack: dict[str, Any], rule_class: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for rule in pack.get("rule_classes", {}).get(rule_class, []) or []:
        if rule.get("enabled", True) is False:
            continue
        item = {
            "rule_id": rule.get("rule_id", ""),
            "enabled": bool(rule.get("enabled", True)),
            "rule_class": rule_class,
            "priority": rule.get("priority", "P1"),
            "result_layer": rule.get("result_layer", ""),
            "effect_on_final": rule.get("effect_on_final", "partial"),
            "checks": [dict(c) for c in rule.get("checks", []) or []],
        }
        rules.append(item)
    return rules


def _checkpoint_check_from_rule_check(check: dict[str, Any], index: int) -> dict[str, Any]:
    data = dict(check)
    data.setdefault("name", data.get("rule_id") or f"rulepack_checkpoint_{index + 1}")
    data.setdefault("severity", severity_for_effect(
        data.get("priority", "P1"),
        data.get("effect_on_final", "partial"),
    ))
    return data
