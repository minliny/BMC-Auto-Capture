from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.executor_api_server.app import create_app
from src.executor_api_server.status_service import ExecutorRuntimeStatusService
from src.plan_item_status_callback_client import FakeCallbackTransport
from src.plan_run_service import PlanRunService
from src.rulepacks import RulePackStore, adapt_rule_pack_to_task_def, validate_rule_pack
from src.rules.result_rules import ResultRuleContext, evaluate_result_rules


def _ssh_rule_pack(task_id: str = "task.019") -> dict:
    return {
        "schema_version": "rulepack.v1",
        "rule_pack_id": f"rulepack.{task_id}.v1",
        "task_id": task_id,
        "protocol": "SSH",
        "execution_mode": "SSH_CMD",
        "applies_to": {
            "task_ids": [task_id],
            "task_type": "SSH",
            "execution_modes": ["SSH_CMD"],
        },
        "audit_metadata": {
            "created_by": "bmc-auto-capture-ssh-output-rules",
            "created_from_artifacts": ["output/task.019.txt"],
            "artifact_hashes": {"output/task.019.txt": "sha256:abc"},
            "review_status": "generated",
        },
        "rule_classes": {
            "stage_gate": [],
            "action_completion": [
                {
                    "rule_id": "ssh.prompt_seen",
                    "priority": "P1",
                    "effect_on_final": "partial",
                    "checks": [{"type": "prompt_required"}],
                }
            ],
            "content_integrity": [
                {
                    "rule_id": "ssh.body_not_empty",
                    "priority": "P1",
                    "effect_on_final": "partial",
                    "checks": [{"type": "min_body_lines", "target": "1"}],
                }
            ],
            "evidence_validation": [
                {
                    "rule_id": "ssh.transcript_marker",
                    "priority": "P1",
                    "effect_on_final": "partial",
                    "checks": [{"type": "text_contains", "target": "PHY"}],
                }
            ],
        },
    }


def _bmc_rule_pack(task_id: str = "task.001") -> dict:
    return {
        "schema_version": "rulepack.v1",
        "rule_pack_id": f"rulepack.{task_id}.v1",
        "task_id": task_id,
        "protocol": "BMC",
        "execution_mode": "BMC_URL",
        "applies_to": {
            "task_ids": [task_id],
            "task_type": "BMC",
            "execution_modes": ["BMC_URL"],
        },
        "rule_classes": {
            "stage_gate": [
                {
                    "rule_id": "bmc.route_reached",
                    "priority": "P0",
                    "effect_on_final": "fail",
                    "checks": [{"type": "url_contains", "target": "/navigate/system"}],
                }
            ],
            "action_completion": [],
            "content_integrity": [
                {
                    "rule_id": "bmc.body_stable",
                    "priority": "P1",
                    "effect_on_final": "partial",
                    "checks": [{"type": "region_stable", "selector": "body", "stable_for_ms": 1000}],
                }
            ],
            "evidence_validation": [
                {
                    "rule_id": "bmc.html_contains_system",
                    "priority": "P1",
                    "effect_on_final": "partial",
                    "checks": [{"type": "text_contains", "target": "System"}],
                }
            ],
        },
    }


def test_rulepack_validator_rejects_unsupported_check_type():
    pack = _bmc_rule_pack()
    pack["rule_classes"]["content_integrity"][0]["checks"] = [{"type": "interface_status"}]

    report = validate_rule_pack(pack)

    assert not report.valid
    assert report.errors[0].code == "RULEPACK_CHECK_TYPE_UNSUPPORTED"


def test_rulepack_adapter_maps_bmc_classes_to_current_fields():
    task_def = {
        "task_id": "task.001",
        "task_type": "BMC",
        "execution_mode": "BMC_URL",
        "command_or_url": "/UI/Static/#/navigate/system",
    }

    merged = adapt_rule_pack_to_task_def(_bmc_rule_pack(), task_def)

    ready = merged["capture_ready_conditions"]
    checkpoints = merged["evidence_checkpoints"]
    assert [item["rule_id"] for item in ready] == ["bmc.route_reached", "bmc.body_stable"]
    assert ready[0]["priority"] == "P0"
    assert checkpoints[0]["rule_id"] == "bmc.html_contains_system"
    assert merged["rule_pack"]["rule_pack_id"] == "rulepack.task.001.v1"


def test_rulepack_adapter_maps_ssh_classes_to_current_fields():
    task_def = {
        "task_id": "task.019",
        "task_type": "SSH",
        "execution_mode": "SSH_CMD",
        "command_or_url": "display interface brief",
    }

    merged = adapt_rule_pack_to_task_def(_ssh_rule_pack(), task_def)

    assert [rule["rule_id"] for rule in merged["result_rules"]] == [
        "ssh.prompt_seen",
        "ssh.body_not_empty",
    ]
    assert merged["checkpoints"][0]["rule_id"] == "ssh.transcript_marker"
    assert merged["rule_pack"]["audit_metadata"]["created_by"] == "bmc-auto-capture-ssh-output-rules"


def test_load_task_defs_merges_rulepack_from_config_dir(tmp_path):
    from src.loader.excel_reader import _load_task_defs

    tasks_json = tmp_path / "tasks.json"
    tasks_json.write_text(json.dumps({
        "tasks": {
            "task.019": {
                "task_id": "task.019",
                "task_name": "Interface brief",
                "task_type": "SSH",
                "execution_mode": "SSH_CMD",
                "command_or_url": "display interface brief",
            }
        }
    }), encoding="utf-8")
    RulePackStore(tmp_path).put(_ssh_rule_pack())

    defs = _load_task_defs(tasks_json)

    assert defs["task.019"]["result_rules"][0]["rule_id"] == "ssh.prompt_seen"
    assert defs["task.019"]["rule_pack"]["schema_version"] == "rulepack.v1"


def test_rulepack_api_validate_import_get_put():
    app = create_app(
        ExecutorRuntimeStatusService(executor_id="rulepack-test"),
        plan_run_service=PlanRunService(callback_transport=FakeCallbackTransport()),
    )
    client = TestClient(app)
    pack = _ssh_rule_pack()

    caps = client.get("/executor/v1/config/rule-capabilities")
    assert caps.status_code == 200
    assert "SSH" in caps.json()["protocols"]

    validation = client.post("/executor/v1/config/rule-packs:validate", json=pack)
    assert validation.status_code == 200
    assert validation.json()["valid"] is True

    imported = client.post("/executor/v1/config/rule-packs:import", json={"rulePacks": [pack]})
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1

    fetched = client.get("/executor/v1/config/rule-packs/task.019")
    assert fetched.status_code == 200
    assert fetched.json()["task_id"] == "task.019"

    updated_pack = dict(pack)
    updated_pack["audit_metadata"] = dict(pack["audit_metadata"], review_status="reviewed")
    updated = client.put("/executor/v1/config/rule-packs/task.019", json=updated_pack)
    assert updated.status_code == 200
    assert updated.json()["accepted"] is True


def test_p2_result_rule_failure_becomes_warning_not_rule_failed():
    rules = [{
        "rule_id": "quality_marker",
        "priority": "P2",
        "effect_on_final": "warning",
        "checks": [{"type": "contains", "target": "optional-marker"}],
    }]

    evaluation = evaluate_result_rules(rules, ResultRuleContext(combined_output="normal output"))
    check = evaluation.to_check_result(check_id="ssh.result_rules", source="result_rules")

    assert evaluation.rule_status == "RULE_WARN"
    assert check.status == "WARN"
    assert check.severity == "WARNING"
