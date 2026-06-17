from __future__ import annotations

from types import SimpleNamespace

from src.rules.result_rules import (
    ResultRuleContext,
    evaluate_result_rules,
    evaluate_task_result_rules,
    extract_result_rules,
)


def test_result_rules_json_checks_text_and_line_count():
    rules = [{
        "rule_id": "basic_output",
        "enabled": True,
        "checks": [
            {"type": "contains", "target": "EXPECTED", "desc": "has expected marker"},
            {"type": "not_contains", "target": "ERROR", "desc": "no error marker"},
            {"type": "min_output_lines", "target": "3", "desc": "enough lines"},
        ],
    }]
    ctx = ResultRuleContext(combined_output="EXPECTED\nline2\nline3")

    evaluation = evaluate_result_rules(rules, ctx)
    check = evaluation.to_check_result(check_id="ssh.result_rules", source="result_rules")

    assert evaluation.passed is True
    assert evaluation.rule_status == "RULE_PASSED"
    assert check.status == "PASS"
    assert check.details["evaluated_checks"] == 3


def test_result_rules_failure_summary_keeps_rule_and_check_detail():
    rules = [{
        "rule_id": "basic_output",
        "checks": [
            {"type": "contains", "target": "EXPECTED", "desc": "has expected marker"},
            {"type": "not_contains", "target": "ERROR", "desc": "no error marker"},
        ],
    }]
    ctx = ResultRuleContext(combined_output="ERROR\nline2")

    evaluation = evaluate_result_rules(rules, ctx)
    check = evaluation.to_check_result(check_id="ssh.result_rules", source="result_rules")

    assert evaluation.rule_status == "RULE_FAILED"
    assert "'EXPECTED' not found" in evaluation.failure_summary()
    assert check.status == "FAIL"
    assert check.details["failures"][0]["rule_name"] == "basic_output"


def test_result_rules_support_required_and_forbidden_pattern_lists():
    rules = [{
        "rule_name": "patterns",
        "checks": [
            {"type": "required_patterns", "patterns": ["alpha", "beta"]},
            {"type": "forbidden_patterns", "patterns": ["fatal", "traceback"]},
        ],
    }]

    evaluation = evaluate_result_rules(rules, ResultRuleContext(combined_output="alpha\nbeta\nok"))

    assert evaluation.rule_status == "RULE_PASSED"
    assert evaluation.evaluated_checks == 2


def test_result_rules_support_legacy_action_aliases_from_task_def():
    task = SimpleNamespace(_task_def={
        "result_rules": [{
            "rule_name": "legacy_alias",
            "enabled": True,
            "actions": [
                {"action_type": "assert_text", "value": "up"},
                {"action_type": "assert_no_text", "value": "down"},
            ],
        }]
    })

    evaluation = evaluate_task_result_rules(task, ResultRuleContext(combined_output="status up"))

    assert len(extract_result_rules(task)) == 1
    assert evaluation.rule_status == "RULE_PASSED"


def test_result_rules_interface_status_failure_is_structured():
    rules = [{
        "name": "端口状态检查",
        "checks": [{
            "type": "interface_status",
            "fields": ["protocol"],
            "forbidden": ["down"],
            "desc": "真实接口记录的 protocol 状态不得为 down",
        }],
    }]
    output = """
display interface brief | include up
Interface                   PHY   Protocol Description
100GE1/0/1                  up    down     normal uplink
<HUAWEI>
"""
    ctx = ResultRuleContext(
        combined_output=output,
        cmd_outputs={"cmd_0": output},
        strategy="interactive_shell",
        resolved_commands=[("cmd_0", "display interface brief | include up")],
    )

    evaluation = evaluate_result_rules(rules, ctx)
    summary = evaluation.failure_summary()
    failure = evaluation.failures[0]
    check = evaluation.to_check_result(check_id="ssh.result_rules", source="result_rules")

    assert evaluation.rule_status == "RULE_FAILED"
    assert "interface=100GE1/0/1" in summary
    assert "field=protocol" in summary
    assert "raw_line=" in summary
    assert failure.details["interface"] == "100GE1/0/1"
    assert failure.details["field"] == "protocol"
    assert failure.details["value"] == "down"
    assert failure.details["raw_line"].startswith("100GE1/0/1")
    assert check.details["failures"][0]["details"]["field"] == "protocol"


def test_result_rules_unparseable_interface_status_is_parse_failed():
    rules = [{
        "name": "端口状态检查",
        "checks": [{
            "type": "interface_status",
            "fields": ["physical", "protocol"],
            "forbidden": ["down"],
            "desc": "真实接口记录状态不得为 down",
        }],
    }]
    output = "display interface brief | include up\nInfo: unavailable\n<HUAWEI>"
    ctx = ResultRuleContext(
        combined_output=output,
        cmd_outputs={"cmd_0": output},
        resolved_commands=[("cmd_0", "display interface brief | include up")],
    )

    evaluation = evaluate_result_rules(rules, ctx)
    check = evaluation.to_check_result(check_id="ssh.result_rules", source="result_rules")

    assert evaluation.rule_status == "RULE_PARSE_FAILED"
    assert "RULE_PARSE_FAILED no parseable interface rows" in evaluation.failure_summary()
    assert check.status == "ERROR"


def test_result_rules_unsupported_enabled_check_is_parse_failed():
    rules = [{"name": "bad_rule", "checks": [{"type": "unknown_check"}]}]

    evaluation = evaluate_result_rules(rules, ResultRuleContext(combined_output="ok"))

    assert evaluation.rule_status == "RULE_PARSE_FAILED"
    assert "unsupported check type" in evaluation.failure_summary()


def test_result_rules_support_regex_all_of_and_min_body_lines():
    rules = [{
        "rule_id": "vrp_interface_brief_shape",
        "checks": [
            {"type": "regex_all_of", "patterns": ["PHY", "Protocol"]},
            {"type": "min_body_lines", "target": "2"},
        ],
    }]
    output = """
display interface brief
Interface                   PHY   Protocol Description
100GE1/0/1                  up    up       uplink
<HUAWEI>
"""
    ctx = ResultRuleContext(
        combined_output=output,
        strategy="interactive_shell",
        resolved_commands=[("cmd_0", "display interface brief")],
    )

    evaluation = evaluate_result_rules(rules, ctx)

    assert evaluation.rule_status == "RULE_PASSED"
    assert evaluation.evaluated_checks == 2


def test_result_rules_regex_all_of_reports_missing_patterns():
    rules = [{
        "rule_id": "missing_shape",
        "checks": [{"type": "regex_all_of", "patterns": ["PHY", "Protocol"]}],
    }]

    evaluation = evaluate_result_rules(rules, ResultRuleContext(combined_output="no table here"))

    assert evaluation.rule_status == "RULE_FAILED"
    assert "regex patterns not matched" in evaluation.failure_summary()
