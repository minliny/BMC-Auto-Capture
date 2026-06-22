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
    stdout: str = ""
    stderr: str = ""
    exit_codes: tuple[int, ...] = ()
    cmd_outputs: dict[str, str] = field(default_factory=dict)
    stdout_outputs: dict[str, str] = field(default_factory=dict)
    stderr_outputs: dict[str, str] = field(default_factory=dict)
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


def extract_result_rules(
    task: Any,
    *,
    command_spec: dict[str, Any] | None = None,
    context: ResultRuleContext | None = None,
) -> list[dict[str, Any]]:
    tdef = getattr(task, "_task_def", None) or {}
    raw = tdef.get("result_rules")
    if raw is None:
        raw = tdef.get("ssh_rules")
    if raw is None:
        raw = tdef.get("rules")
    rules = normalize_result_rules(raw)
    rules.extend(_legacy_execution_rules(tdef, command_spec, context))
    return rules


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


def evaluate_task_result_rules(
    task: Any,
    context: ResultRuleContext,
    *,
    command_spec: dict[str, Any] | None = None,
) -> ResultRuleEvaluation:
    return evaluate_result_rules(extract_result_rules(task, command_spec=command_spec, context=context), context)


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
    source_text = _source_text(check, context)

    if check_type in (
        "text_exists", "required_pattern", "required_patterns",
        "text_contains", "assert_text", "contains",
    ):
        for value in _coerce_values(target or check.get("patterns")):
            if value and value not in source_text:
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
            elif _pattern_found(value, source_text, bool(check.get("regex") or check.get("match") == "regex")):
                return _failure(rule_name, check_type, f"[{rule_name}] {desc}: forbidden '{value}' found")
        return None

    if check_type in ("regex_exists", "regex_match"):
        pattern = str(target or "")
        if pattern and re.search(pattern, source_text) is None:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: regex '{pattern}' not matched")
        return None

    if check_type == "regex_all_of":
        patterns = _coerce_values(check.get("patterns", target))
        missing = [pattern for pattern in patterns if re.search(pattern, source_text) is None]
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
        if not any(re.search(pattern, source_text) for pattern in patterns):
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: none of regex patterns matched: {patterns}",
            )
        return None

    if check_type in ("regex_not_exists", "regex_not_match"):
        patterns = _coerce_values(check.get("patterns", target))
        matched = [pattern for pattern in patterns if pattern and re.search(pattern, source_text) is not None]
        if matched:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: forbidden regex matched: {matched}")
        return None

    if check_type == "sentinel_seen":
        patterns = _coerce_values(check.get("patterns", target or check.get("sentinel")))
        if not patterns:
            return _parse_failure(rule_name, check_type, f"[{rule_name}] RULE_PARSE_FAILED no sentinel configured")
        use_regex = bool(check.get("regex") or check.get("pattern") or check.get("patterns"))
        missing = []
        for pattern in patterns:
            found = re.search(pattern, source_text) is not None if use_regex else pattern in source_text
            if not found:
                missing.append(pattern)
        if missing:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: sentinel not seen: {missing}")
        return None

    if check_type == "exit_code_in":
        allowed = _coerce_int_values(
            check.get("allowed", check.get("values", check.get("codes", target))),
        )
        if not allowed:
            return _parse_failure(rule_name, check_type, f"[{rule_name}] RULE_PARSE_FAILED no allowed exit codes")
        exit_codes = _extract_exit_codes(context)
        if not exit_codes:
            return _parse_failure(rule_name, check_type, f"[{rule_name}] RULE_PARSE_FAILED no exit code marker found")
        disallowed = [code for code in exit_codes if code not in allowed]
        if disallowed:
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: exit codes {disallowed} not in {allowed}",
                details={"exit_codes": exit_codes, "allowed": allowed},
            )
        return None

    if check_type == "pager_exhausted":
        patterns = _coerce_values(check.get("patterns", check.get("forbidden")))
        if not patterns:
            patterns = [
                r"----\s*More\s*----",
                r"--\s*More\s*--",
                r"More:\s*$",
                r"Press\s+any\s+key\s+to\s+continue",
            ]
        matches = [pattern for pattern in patterns if re.search(pattern, source_text, re.IGNORECASE | re.MULTILINE)]
        if matches:
            return _failure(rule_name, check_type, f"[{rule_name}] {desc}: pager prompt remains: {matches}")
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
            actual_lines = len(_body_lines(context, source_text))
        else:
            actual_lines = len(source_text.split("\n"))
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
        if context.strategy == "interactive_shell" and not VRP_PROMPT_RE.search(source_text):
            return _failure(rule_name, check_type, f"[{rule_name}] VRP prompt not detected in output")
        return None

    if check_type in ("allowed_patterns", "allowlist_patterns"):
        if not source_text.strip():
            return None
        allowed = _coerce_values(check.get("patterns", check.get("allowed", check.get("allow_patterns"))))
        ignored = _coerce_values(check.get("ignore_patterns", check.get("ignored", check.get("ignore"))))
        unmatched_lines = [
            line for line in source_text.splitlines()
            if line.strip() and not _matches_any_pattern(line, allowed + ignored)
        ]
        if unmatched_lines:
            return _failure(
                rule_name,
                check_type,
                f"[{rule_name}] {desc}: source {check.get('source', 'combined')} not allowlisted: "
                f"{' | '.join(unmatched_lines)[:160]}",
                details={"unmatched_lines": unmatched_lines[:5], "source": check.get("source", "combined")},
            )
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


def _coerce_int_values(raw: Any) -> list[int]:
    values: list[int] = []
    for item in _coerce_values(raw):
        for part in re.split(r"[,\s]+", item):
            if not part:
                continue
            try:
                values.append(int(part))
            except ValueError:
                continue
    return values


def _extract_exit_codes(context: ResultRuleContext) -> list[int]:
    if context.exit_codes:
        return list(context.exit_codes)
    text = "\n".join([context.combined_output, *context.cmd_outputs.values()])
    patterns = [
        r"\[exit_code:(-?\d+)\]",
        r"\bexit[_ -]?code\s*[:=]\s*(-?\d+)\b",
        r"\breturn[_ -]?code\s*[:=]\s*(-?\d+)\b",
        r"\brc\s*[:=]\s*(-?\d+)\b",
        r"__EXIT_CODE__\s*[:=]\s*(-?\d+)",
    ]
    codes: list[int] = []
    seen: set[int] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                code = int(match.group(1))
            except ValueError:
                continue
            if code not in seen:
                codes.append(code)
                seen.add(code)
    return codes


def _body_lines(context: ResultRuleContext, text: str | None = None) -> list[str]:
    commands = {cmd.strip() for _name, cmd in context.resolved_commands if cmd.strip()}
    lines: list[str] = []
    source = context.combined_output if text is None else text
    for raw_line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
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


def _source_text(check: dict[str, Any], context: ResultRuleContext) -> str:
    source = str(check.get("source") or check.get("stream") or "combined").strip().lower()
    if source in {"combined", "output", "transcript", ""}:
        return context.combined_output
    if source == "stdout":
        return context.stdout or "\n".join(context.stdout_outputs.values())
    if source == "stderr":
        return context.stderr or "\n".join(context.stderr_outputs.values())
    if source.startswith("cmd:"):
        key = source.split(":", 1)[1]
        return context.cmd_outputs.get(key, "")
    return context.combined_output


def _pattern_found(pattern: str, text: str, regex: bool) -> bool:
    if regex:
        return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None
    return pattern in text


def _matches_any_pattern(text: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def _legacy_execution_rules(
    task_def: dict[str, Any],
    command_spec: dict[str, Any] | None,
    context: ResultRuleContext | None,
) -> list[dict[str, Any]]:
    spec = command_spec or {}
    fail_patterns = list(spec.get("stderr_fail_patterns", task_def.get("stderr_fail_patterns", [])) or [])
    allow_patterns = list(spec.get("stderr_allow_patterns", task_def.get("stderr_allow_patterns", [])) or [])
    ignore_patterns = list(spec.get("stderr_ignore_patterns", task_def.get("stderr_ignore_patterns", [])) or [])
    allow_exit_codes = _coerce_int_values(spec.get("allow_exit_codes", task_def.get("allow_exit_codes", [])))

    checks: list[dict[str, Any]] = []
    stderr_source = "stderr"
    if context is not None and not (context.stderr or "").strip() and context.strategy in {"terminal_session", "interactive_shell"}:
        stderr_source = "combined"
    if fail_patterns:
        checks.append({
            "type": "forbidden_patterns",
            "source": stderr_source,
            "patterns": fail_patterns,
            "match": "regex",
            "desc": "legacy stderr fail patterns",
        })
    if (
        allow_patterns
        or ignore_patterns
        or (context is not None and (context.stderr or "").strip())
    ):
        checks.append({
            "type": "allowed_patterns",
            "source": "stderr",
            "patterns": allow_patterns,
            "ignore_patterns": ignore_patterns,
            "desc": "legacy stderr allowlist",
        })
    if context is not None:
        exit_codes = list(context.exit_codes) or _extract_exit_codes(context)
        if exit_codes:
            allowed = sorted({0, *allow_exit_codes})
            checks.append({
                "type": "exit_code_in",
                "source": "exit_code",
                "allowed": allowed,
                "desc": "legacy allowed exit codes",
            })
    if not checks:
        return []
    return [{
        "rule_id": "ssh.execution_result",
        "rule_class": "action_completion",
        "priority": "P0",
        "result_layer": "availability",
        "effect_on_final": "fail",
        "checks": checks,
    }]


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
