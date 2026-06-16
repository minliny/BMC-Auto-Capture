from __future__ import annotations

import csv
from pathlib import Path

from src.executor.ssh_executor import SSHExecutor
from src.loader.excel_reader import load_all
from src.models.execution_result import ExecutionResult
from src.out.summary import write_failure_csv
from src.rules.interface_status import parse_interface_brief


class MockTask:
    def __init__(self, checks, command="display interface brief | include up"):
        self.command_or_url = command
        self._resolved_commands = [("cmd_0", command)]
        self._task_def = {
            "rules": [
                {
                    "name": "端口状态检查",
                    "enabled": True,
                    "checks": checks,
                }
            ]
        }


def _evaluate(checks, output, command="display interface brief | include up"):
    task = MockTask(checks, command=command)
    return SSHExecutor()._evaluate_ssh_rules(
        task,
        combined_output=output,
        cmd_outputs={"cmd_0": output},
        strategy="interactive_shell",
    )


def _interface_status_check(*fields):
    return {
        "type": "interface_status",
        "fields": list(fields) or ["physical", "protocol"],
        "forbidden": ["down"],
        "desc": "真实接口记录状态不得为 down",
    }


def test_interface_status_ignores_description_down_text():
    output = """
display interface brief | include up
PHY: Physical
*down: administratively down
Interface                   PHY   Protocol Description
100GE1/0/1                  up    up       peer shutdown/down note
<HUAWEI>
"""

    failure = _evaluate([_interface_status_check("physical", "protocol")], output)

    assert failure == ""


def test_legacy_forbidden_down_on_interface_brief_uses_structured_fields():
    output = """
display interface brief | include up
Interface                   PHY   Protocol Description
100GE1/0/1                  up    up       peer downlink shutdown marker
<HUAWEI>
"""
    checks = [{"type": "text_not_exists", "target": "down", "desc": "不存在down端口"}]

    failure = _evaluate(checks, output)

    assert failure == ""


def test_interface_status_protocol_down_fails_with_field_detail():
    output = """
Interface                   PHY   Protocol Description
100GE1/0/2                  up    down     normal uplink
"""

    failure = _evaluate([_interface_status_check("protocol")], output)

    assert "interface=100GE1/0/2" in failure
    assert "field=protocol" in failure
    assert "value='down'" in failure
    assert "raw_line=" in failure


def test_interface_status_physical_down_fails_with_field_detail():
    output = """
Interface                   PHY   Protocol Description
100GE1/0/3                  down  up       normal uplink
"""

    failure = _evaluate([_interface_status_check("physical")], output)

    assert "interface=100GE1/0/3" in failure
    assert "field=physical" in failure
    assert "value='down'" in failure


def test_interface_status_ignores_headers_legends_and_prompts_with_down():
    output = """
display interface brief | include up
PHY: Physical
*down: administratively down
^down: standby
Interface                   PHY   Protocol Description
-------------------------------------------------------
100GE1/0/4                  up    up       normal uplink
<HUAWEI>
"""

    records = parse_interface_brief(output)
    failure = _evaluate([_interface_status_check("physical", "protocol")], output)

    assert [record.interface for record in records] == ["100GE1/0/4"]
    assert failure == ""


def test_interface_status_ignores_command_echo_down_text():
    command = "display interface brief | include down"
    output = """
display interface brief | include down
Interface                   PHY   Protocol Description
100GE1/0/5                  up    up       normal uplink
<HUAWEI>
"""

    failure = _evaluate([_interface_status_check("physical", "protocol")], output, command=command)

    assert failure == ""


def test_interface_status_unparseable_output_returns_parse_failed():
    output = """
display interface brief | include up
Info: The brief output is unavailable.
<HUAWEI>
"""

    failure = _evaluate([_interface_status_check("physical", "protocol")], output)

    assert "RULE_PARSE_FAILED" in failure
    assert "forbidden 'down' found" not in failure


def test_failure_detail_csv_keeps_interface_field_and_raw_line(tmp_path):
    output = """
Interface                   PHY   Protocol Description
100GE1/0/6                  up    down     normal uplink
"""
    failure = _evaluate([_interface_status_check("protocol")], output)
    result = ExecutionResult(
        "p1",
        "device-1",
        device_group="L1",
        task_name="计算节点L1交换网络端口查询测试",
        task_type="SSH",
        execution_status="EXEC_PARTIAL",
        execution_failure_reason=f"规则检查失败: {failure}",
    )

    failure_csv = Path(write_failure_csv([result], str(tmp_path)))

    with failure_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    reason = rows[0]["失败原因"]
    assert "interface=100GE1/0/6" in reason
    assert "field=protocol" in reason
    assert "value='down'" in reason
    assert "raw_line=" in reason


def test_plain_text_forbidden_rule_still_uses_substring_matching():
    output = "system status: down for maintenance"
    checks = [{"type": "text_not_exists", "target": "down", "desc": "普通文本不能包含 down"}]

    failure = _evaluate(checks, output, command="display version")

    assert "forbidden 'down' found" in failure


def test_actual_l1_port_task_uses_structured_interface_status_rule():
    root = Path(__file__).resolve().parent.parent
    _, tasks = load_all(
        str(root / "examples" / "task_template.xlsx"),
        tasks_json_path=str(root / "tasks.json"),
    )
    task = next(t for t in tasks if t.task_name == "计算节点L1交换网络端口查询测试")
    output = """
display interface brief | include up
PHY: Physical
*down: administratively down
Interface                   PHY   Protocol Description
100GE1/0/1                  up    up       peer shutdown/down note
<HUAWEI>
"""
    object.__setattr__(task, "_resolved_commands", [("cmd_0", task.command_or_url)])

    rules = getattr(task, "_task_def", {}).get("rules", [])
    failure = SSHExecutor()._evaluate_ssh_rules(
        task,
        combined_output=output,
        cmd_outputs={"cmd_0": output},
        strategy="interactive_shell",
    )

    assert rules[0]["checks"][0]["type"] == "interface_status"
    assert failure == ""
