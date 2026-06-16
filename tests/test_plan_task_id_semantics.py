from __future__ import annotations

import json

import openpyxl

from src.loader.excel_reader import load_tasks
from src.models.device import Device
from src.models.execution_result import ExecutionResult
from src.models.task import Task
from src.out.summary import write_failure_csv
from src.plan_run_service.job_payload import PlanRunJobPayloadBuilder
from src.plan_run_service.models import PlanRunItem
from src.scheduler.plan_generator import generate_plans


def test_excel_task_id_matches_tasks_json_when_task_name_changes(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "任务列表"
    ws.append([
        "任务ID(TaskID)",
        "任务序号(TaskSequence)",
        "任务名称(TaskName)",
        "任务类型(TaskType)",
        "设备分组(DeviceGroup)",
        "截图保存目录(OutputDir)",
        "图片命名格式(FileNamePattern)",
        "是否启用(Enabled)",
    ])
    ws.append(["task.ssh.port", 1, "用户改过的新名称", "SSH", "L1", "out", "img", "是"])
    excel_path = tmp_path / "tasks.xlsx"
    wb.save(excel_path)
    wb.close()

    tasks_json = tmp_path / "tasks.json"
    tasks_json.write_text(
        json.dumps(
            {
                "tasks": {
                    "task.ssh.port": {
                        "task_id": "task.ssh.port",
                        "task_name": "旧名称不会参与匹配",
                        "task_type": "SSH",
                        "execution_mode": "SSH_CMD",
                        "command_or_url": "display interface brief",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    tasks = load_tasks(excel_path, tasks_json_path=tasks_json)

    assert len(tasks) == 1
    assert tasks[0].task_id == "task.ssh.port"
    assert tasks[0].task_name == "用户改过的新名称"
    assert tasks[0].command_or_url == "display interface brief"
    assert tasks[0].execution_mode == "SSH_CMD"


def test_generate_plans_uses_one_batch_plan_id_and_unique_plan_item_ids():
    device = Device(
        row_index=1,
        device_name="node-1",
        device_group="L1",
        bmc_ip="",
        bmc_username="",
        bmc_password="",
        inband_ip="192.0.2.10",
        inband_username="u",
        inband_password="p",
    )
    task = Task(
        1,
        1,
        "端口状态检查",
        "SSH",
        "SSH_CMD",
        match_group="L1",
        command_or_url="display interface brief",
        task_id="task.ssh.port",
    )

    plans = generate_plans([device], [task], plan_id="plan-batch-001")

    assert len(plans) == 1
    assert plans[0].plan_id == "plan-batch-001"
    assert plans[0].task_id == "task.ssh.port"
    assert plans[0].plan_item_id == "plan-batch-001:node-1:task.ssh.port"


def test_job_payload_uses_plan_item_id_not_task_name():
    device = Device(
        row_index=1,
        device_name="node-1",
        device_group="L1",
        bmc_ip="",
        bmc_username="",
        bmc_password="",
        inband_ip="192.0.2.10",
        inband_username="u",
        inband_password="p",
    )
    task = Task(
        1,
        1,
        "展示名称",
        "SSH",
        "SSH_CMD",
        command_or_url="display version",
        task_id="task.ssh.version",
    )
    item = PlanRunItem(
        plan_id="plan-1",
        device_name="node-1",
        task_name="展示名称可修改",
        task_id="task.ssh.version",
        plan_item_id="plan-1:node-1:task.ssh.version",
        device_group="L1",
        task_type="SSH",
        execution_mode="SSH_CMD",
        _device=device,
        _task=task,
    )

    payload = PlanRunJobPayloadBuilder().build(item)

    assert payload["job_id"] == "plan-1:node-1:task.ssh.version"
    assert payload["plan_id"] == "plan-1"
    assert payload["task_snapshot"]["task_id"] == "task.ssh.version"
    assert payload["task_snapshot"]["plan_item_id"] == "plan-1:node-1:task.ssh.version"
    assert payload["task_snapshot"]["task_name"] == "展示名称可修改"


def test_failure_detail_includes_rule_failed_successful_execution(tmp_path):
    result = ExecutionResult(
        plan_id="plan-1",
        task_id="task.ssh.port",
        plan_item_id="plan-1:node-1:task.ssh.port",
        device_name="node-1",
        device_group="L1",
        task_name="端口状态检查",
        task_type="SSH",
        execution_status="EXEC_SUCCESS",
        rule_status="RULE_FAILED",
        rule_failure_reason="interface=GE1/0/1 field=protocol value='down'",
    )

    path = write_failure_csv([result], str(tmp_path))
    content = (tmp_path / "failure_detail.csv").read_text(encoding="utf-8-sig")

    assert path
    assert "task.ssh.port" in content
    assert "plan-1:node-1:task.ssh.port" in content
    assert "RULE_FAILED" in content
    assert "field=protocol" in content
