"""Protocol-neutral execution result rule evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any

from ..checks import CheckResult, CheckStage, CheckStatus
from .interface_status import (
    coerce_status_fields,
    coerce_status_values,
    is_interface_brief_command,
    parse_interface_brief,
    status_matches,
)


VRP_PROMPT_RE = re.compile(
    r"(?m)(?:^|\r?\n)[<\[][^<>\[\]\r\n]{1,128}[>\]][ \t]*(?:\r?\n)?\Z"
)


@dataclass(frozen=True)
class ResultRuleContext:
    combined_output: str = ""
    cmd_outputs: dict[str, str] = field(default_factory=dict)
    strategy: str = ""
    resolved_commands: list[tuple[str, str]] = field(default_factory=list)
    command_or_url: str = ""


@dataclass(frozen=True)
class ResultRuleFailure:
    rule_name: str
    check_type: str
    message: str
    status: str = CheckStatus.FAIL
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "rule_name": self.rule_name,
            "check_type": self.check_type,
            "status": self.status,
            "message": self.message,
        }
        if self.details:
            data["details"] = dict(self.details)
        return data


@dataclass
class ResultRuleEvaluation:
    has_rules: bool = False
    evaluated_checks: int = 0
    failures: list[ResultRuleFailure] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.has_rules and not self.failures

    @property
    def rule_status(self) -> str:
        if not self.has_rules:
            return "RULE_DISABLED"
        if any(f.status == CheckStatus.ERROR for f in self.failures):
            return "RULE_PARSE_FAILED"
        if any(f.status == CheckStatus.FAIL for f in self.failures):
            return "RULE_FAILED"
        if any(f.status == CheckStatus.WARN for f in self.failures):
            return "RULE_WARN"
        return "RULE_PASSED"

    @property
    def has_blocking_failures(self) -> bool:
        return self.rule_status in {"RULE_FAILED", "RULE_PARSE_FAILED"}

    @property
    def has_warnings(self) -> bool:
        return self.rule_status == "RULE_WARN"

    def failure_summary(self, limit: int = 5, *, include_warnings: bool = True) -> str:
        failures = self.failures if include_warnings else [
            f for f in self.failures if f.status in {CheckStatus.FAIL, CheckStatus.ERROR}
        ]
        return "; ".join(f.message for f in failures[:limit])

    def to_check_result(
        self,
        *,
        check_id: str,
        source: str,
        target: str = "",
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        if self.rule_status == "RULE_PARSE_FAILED":
            status = CheckStatus.ERROR
        elif self.rule_status == "RULE_FAILED":
            status = CheckStatus.FAIL
        elif self.rule_status == "RULE_PASSED":
            status = CheckStatus.PASS
        elif self.rule_status == "RULE_WARN":
            status = CheckStatus.WARN
        else:
            status = CheckStatus.SKIP

        merged_details = {
            "rule_status": self.rule_status,
            "evaluated_checks": self.evaluated_checks,
            "failures": [f.to_dict() for f in self.failures],
        }
        if details:
            merged_details.update(details)

        return CheckResult(
            stage=CheckStage.RESULT,
            check_id=check_id,
            status=status,
            severity="WARNING" if status == CheckStatus.WARN else "ERROR",
            message=self.failure_summary() or (
                "Result rules passed" if self.rule_status == "RULE_PASSED" else self.rule_status
            ),
            details=merged_details,
            source=source,
            target=target,
        )


def extract_result_rules(task: Any) -> list[dict[str, Any]]:
    tdef = getattr(task, "_task_def", None) or {}
    raw = tdef.get("result_rules")
    if raw is None:
        raw = tdef.get("ssh_rules")
    if raw is None:
        raw = tdef.get("rules")
    return normalize_result_rules(raw)


def normalize_result_rules(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("rules"), list):
            return [r for r in raw["rules"] if isinstance(r, dict)]
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def has_enabled_result_rules(task: Any) -> bool:
    return any(rule.get("enabled", True) is not False for rule in extract_result_rules(task))


def evaluate_task_result_rules(task: Any, context: ResultRuleContext) -> ResultRuleEvaluation:
    return evaluate_result_rules(extract_result_rules(task), context)


def evaluate_result_rules(
    rules: list[dict[str, Any]],
    context: ResultRuleContext,
) -> ResultRuleEvaluation:
    evaluation = ResultRuleEvaluation(has_rules=bool(rules))
    for rule in rules:
        if rule.get("enabled", True) is False:
            continue

        rule_name = str(rule.get("rule_id") or rule.get("rule_name") or rule.get("name") or "unnamed")
        checks = rule.get("checks", rule.get("actions", []))
        if not isinstance(checks, list):
            evaluation.failures.append(_parse_failure(
                rule_name,
                "checks",
                f"[{rule_name}] RULE_PARSE_FAILED checks must be a list",
            ))
            continue

        for check in checks:
            if not isinstance(check, dict):
                evaluation.failures.append(_parse_failure(
                    rule_name,
                    "check",
                    f"[{rule_name}] RULE_PARSE_FAILED check must be an object",
                ))
                continue
            evaluation.evaluated_checks += 1
            failure = _evaluate_check(rule_name, check, context)
            if failure is not None:
                failure = _attach_rule_metadata(failure, rule)
                evaluation.failures.append(failure)
    evaluation.has_rules = evaluation.evaluated_checks > 0 or any(
        rule.get("enabled", True) is not False for rule in rules
    )
    return evaluation


def _evaluate_check(
    rule_name: str,
    check: dict[str, Any],
    context: ResultRuleContext,
) -> ResultRuleFailure | None:
    check_type = str(check.get("type") or check.get("action_type") or "")
    target = check.get("target", check.get("value", check.get("expect", check.get("pattern", ""))))
    desc = str(check.get("desc", check.get("description", check_type)))

    if check_type in (
        "text_exists", "required_pattern", "required_patterns",
        "text_contains", "assert_text", "contains",
    ):
        for value in _coerce_values(target or check.get("patterns")):
            if value and value not in context.combined_output:
                return _failure(rule_name, check_type, f"[{rule_name}] {desc}: '{value}' not found")
        return None

    if check_type in (
        "text_not_exists", "forbidden_pattern", "forbidden_patterns", "not_contains_any",
        "assert_no_text", "not_contains",
    ):
        for value in _coerce_values(target or check.get("patterns") or check.get("forbidden")):
            if not value:
                continue
            if status_matches(value, "down") and _has_interface_brief_command(context):
                message, details = _evaluate_interface_status_rule(
                    rule_name,
                    desc,
                    _interface_brief_rule_output(context),
                    ["physical", "protocol"],
                    [value],
                )
                if message:
                    return _failure(rule_name, check_type, message, details=details)
            elif value in context.combined_output:
                return _failure(rule_name, check_type, f"[{rule_name}] {desc}: forbidden '{value}' found")
        return None

    if check_type in ("regex_exists", "regex_match"):
        pattern = str(target or "")
        if pattern and re.search(pattern, context.combined_output) is None:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: regex '{pattern}' not matched")
        return None

    if check_type == "regex_all_of":
        patterns = _coerce_values(check.get("patterns", target))
        missing = [pattern for pattern in patterns if re.search(pattern, context.combined_output) is None]
        if missing:
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: regex patterns not matched: {missing}",
            )
        return None

    if check_type == "regex_any_of":
        patterns = _coerce_values(check.get("patterns", target))
        if not patterns:
            return None
        if not any(re.search(pattern, context.combined_output) for pattern in patterns):
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: none of regex patterns matched: {patterns}",
            )
        return None

    if check_type in ("regex_not_exists", "regex_not_match"):
        pattern = str(target or "")
        if pattern and re.search(pattern, context.combined_output) is not None:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: forbidden regex '{pattern}' matched")
        return None

    if check_type in ("interface_status", "interface_status_not"):
        fields = coerce_status_fields(check.get("fields", check.get("field", check.get("target_field"))))
        forbidden_values = coerce_status_values(
            check.get("forbidden", check.get("forbidden_values", check.get("target"))),
        )
        message, details = _evaluate_interface_status_rule(
            rule_name,
            desc,
            _interface_brief_rule_output(context),
            fields,
            forbidden_values,
        )
        if message:
            status = CheckStatus.ERROR if "RULE_PARSE_FAILED" in message else CheckStatus.FAIL
            return _failure(rule_name, check_type, message, status=status, details=details)
        return None

    if check_type in ("min_output_lines", "min_body_lines"):
        min_lines = int(target) if target else 1
        if check_type == "min_body_lines":
            actual_lines = len(_body_lines(context))
        else:
            actual_lines = len(context.combined_output.split("\n"))
        if actual_lines < min_lines:
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: only {actual_lines} lines (min {min_lines})",
            )
        return None

    if check_type == "command_echo_required":
        if context.strategy == "interactive_shell":
            for _cmd_name, cmd in context.resolved_commands:
                if cmd and cmd[:30] not in context.combined_output:
                    return _failure(rule_name, check_type, f"[{rule_name}] command echo missing: {cmd[:50]}")
        return None

    if check_type == "prompt_required":
        if context.strategy == "interactive_shell" and not VRP_PROMPT_RE.search(context.combined_output):
            return _failure(rule_name, check_type, f"[{rule_name}] VRP prompt not detected in output")
        return None

    return _parse_failure(
        rule_name,
        check_type or "unknown",
        f"[{rule_name}] RULE_PARSE_FAILED unsupported check type: {check_type!r}",
    )


def _interface_brief_rule_output(context: ResultRuleContext) -> str:
    selected: list[str] = []
    for cmd_name, cmd in context.resolved_commands:
        if is_interface_brief_command(cmd) and cmd_name in context.cmd_outputs:
            selected.append(context.cmd_outputs.get(cmd_name, ""))
    if selected:
        return "\n".join(selected)
    return context.combined_output


def _has_interface_brief_command(context: ResultRuleContext) -> bool:
    if any(is_interface_brief_command(cmd) for _name, cmd in context.resolved_commands):
        return True
    return is_interface_brief_command(context.command_or_url)


def _evaluate_interface_status_rule(
    rule_name: str,
    desc: str,
    output: str,
    fields: list[str],
    forbidden_values: list[str],
) -> tuple[str, dict[str, Any]]:
    records = parse_interface_brief(output)
    if not records:
        return (
            f"[{rule_name}] {desc}: RULE_PARSE_FAILED no parseable interface rows",
            {"parse_error": "no parseable interface rows"},
        )

    failures: list[str] = []
    matches: list[dict[str, str]] = []
    for record in records:
        for field in fields:
            value = getattr(record, field, "")
            if any(status_matches(value, forbidden) for forbidden in forbidden_values):
                matches.append({
                    "interface": record.interface,
                    "field": field,
                    "value": value,
                    "raw_line": record.raw_line,
                })
                failures.append(
                    f"[{rule_name}] {desc}: interface={record.interface} "
                    f"field={field} value={value!r} raw_line={record.raw_line!r}"
                )
    if not matches:
        return "", {}
    details: dict[str, Any] = dict(matches[0])
    details["matches"] = matches[:5]
    return "; ".join(failures[:5]), details


def _coerce_values(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list | tuple | set):
        return [str(value) for value in raw if str(value)]
    return [str(raw)]


def _body_lines(context: ResultRuleContext) -> list[str]:
    commands = {cmd.strip() for _name, cmd in context.resolved_commands if cmd.strip()}
    lines: list[str] = []
    for raw_line in context.combined_output.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line in commands:
            continue
        if VRP_PROMPT_RE.search(line):
            continue
        if line.lower().startswith(("login:", "password:")):
            continue
        lines.append(line)
    return lines


def _failure(
    rule_name: str,
    check_type: str,
    message: str,
    *,
    status: str = CheckStatus.FAIL,
    details: dict[str, Any] | None = None,
) -> ResultRuleFailure:
    return ResultRuleFailure(
        rule_name=rule_name,
        check_type=check_type,
        message=message,
        status=status,
        details=details or {},
    )


def _parse_failure(rule_name: str, check_type: str, message: str) -> ResultRuleFailure:
    return _failure(rule_name, check_type, message, status=CheckStatus.ERROR)


def _attach_rule_metadata(failure: ResultRuleFailure, rule: dict[str, Any]) -> ResultRuleFailure:
    priority = str(rule.get("priority", "") or "").upper()
    effect = str(rule.get("effect_on_final", "") or "").lower()
    details = dict(failure.details)
    if priority:
        details["priority"] = priority
    if effect:
        details["effect_on_final"] = effect
    if rule.get("rule_class"):
        details["rule_class"] = str(rule.get("rule_class"))
    if rule.get("result_layer"):
        details["result_layer"] = str(rule.get("result_layer"))
    status = failure.status
    if failure.status == CheckStatus.FAIL and (priority == "P2" or effect in {"partial", "warning"}):
        status = CheckStatus.WARN
    return replace(failure, status=status, details=details)
