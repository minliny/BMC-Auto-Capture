from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.checks import CheckStage, check_result_from_condition
from src.executor_api_server.app import create_app
from src.executor_api_server.status_service import ExecutorRuntimeStatusService
from src.plan_item_status_callback_client import FakeCallbackTransport
from src.plan_run_service import PlanRunService
from src.rulepacks import RulePackStore, adapt_rule_pack_to_task_def, validate_rule_pack
from src.rulepacks.capabilities import CAPABILITY_REGISTRY
from src.rulepacks.fingerprint import sha256_text
from src.rules.condition_evaluator import (
    ArtifactContext,
    evaluate_evidence_checkpoints,
    evaluate_ready_conditions,
    parse_checkpoint_specs,
    parse_ready_specs,
)
from src.rules.result_rules import ResultRuleContext, evaluate_result_rules


class _FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        visible: bool = True,
        enabled: bool = True,
        attrs: dict[str, str] | None = None,
    ):
        self._text = text
        self._visible = visible
        self._enabled = enabled
        self._attrs = attrs or {}

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def inner_text(self):
        return self._text

    async def text_content(self):
        return self._text

    async def get_attribute(self, name: str):
        return self._attrs.get(name, "")


class _FakePage:
    def __init__(self):
        self.url = "https://bmc/UI/Static/#/navigate/system"
        self.body = "System health ready"
        self.elements = {
            ".visible": [_FakeElement("System health ready")],
            ".hidden": [_FakeElement("hidden", visible=False)],
            ".row": [_FakeElement("row 1"), _FakeElement("row 2")],
            "#active-tab": [_FakeElement("System", attrs={"class": "el-tabs__item is-active"})],
            "#post-state": [_FakeElement("System health ready")],
        }

    def is_closed(self):
        return False

    async def query_selector(self, selector: str):
        matches = self.elements.get(selector, [])
        return matches[0] if matches else None

    async def query_selector_all(self, selector: str):
        return self.elements.get(selector, [])

    async def inner_text(self, selector: str):
        if selector == "body":
            return self.body
        element = await self.query_selector(selector)
        return await element.inner_text() if element else ""


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


def _write_tasks_json(root: Path, task_def: dict) -> None:
    task_id = str(task_def.get("task_id") or "task.019")
    (root / "tasks.json").write_text(
        json.dumps({"tasks": {task_id: task_def}}, ensure_ascii=False),
        encoding="utf-8",
    )


def _bmc_ready_spec(check_type: str) -> dict:
    if check_type in {"page_alive", "not_login_page"}:
        return {"type": check_type}
    if check_type == "url_contains":
        return {"type": check_type, "target": "/navigate/system"}
    if check_type == "url_not_contains":
        return {"type": check_type, "target": "/login"}
    if check_type in {"selector_visible", "selector_enabled", "text_nonempty"}:
        return {"type": check_type, "selector": ".visible"}
    if check_type in {"selector_hidden", "selector_not_visible"}:
        return {"type": check_type, "selector": ".hidden"}
    if check_type in {"selector_count_ge", "count_ge"}:
        return {"type": check_type, "selector": ".row", "min_count": 2}
    if check_type == "text_contains":
        return {"type": check_type, "target": "System health"}
    if check_type == "text_contains_any":
        return {"type": check_type, "values": ["System health", "Dashboard"]}
    if check_type == "text_not_in":
        return {"type": check_type, "values": ["Fatal", "Traceback"]}
    if check_type == "region_stable":
        return {
            "type": check_type,
            "selector": ".visible",
            "stable_for_ms": 0,
            "sample_interval_ms": 5,
            "timeout_ms": 50,
        }
    if check_type == "active_tab_changed":
        return {"type": check_type, "selector": "#active-tab", "values": ["is-active"]}
    if check_type == "post_action_state_changed":
        return {"type": check_type, "selector": "#post-state", "expected": "System health"}
    raise AssertionError(f"missing BMC ready fixture for {check_type}")


def _bmc_evidence_spec(check_type: str, artifact_path: Path) -> dict:
    if check_type == "file_exists":
        return {"type": check_type, "target": str(artifact_path)}
    if check_type == "html_contains":
        return {"type": check_type, "target": "System health"}
    if check_type == "txt_contains":
        return {"type": check_type, "target": "System health"}
    if check_type == "text_contains":
        return {"type": check_type, "target": "System health"}
    if check_type == "text_contains_any":
        return {"type": check_type, "values": ["System health", "Dashboard"]}
    if check_type == "text_not_contains":
        return {"type": check_type, "target": "Fatal"}
    if check_type == "not_contains_any":
        return {"type": check_type, "values": ["Fatal", "Traceback"]}
    if check_type == "regex_match":
        return {"type": check_type, "target": r"System\s+health"}
    if check_type == "regex_not_match":
        return {"type": check_type, "target": r"Traceback|Fatal"}
    raise AssertionError(f"missing BMC evidence fixture for {check_type}")


def _ssh_result_check(check_type: str) -> dict:
    if check_type in {"contains", "text_exists", "text_contains", "required_pattern"}:
        return {"type": check_type, "target": "PHY"}
    if check_type == "required_patterns":
        return {"type": check_type, "patterns": ["PHY", "Protocol"]}
    if check_type in {"not_contains", "text_not_exists", "forbidden_pattern", "assert_no_text"}:
        return {"type": check_type, "target": "Fatal"}
    if check_type in {"forbidden_patterns", "not_contains_any"}:
        return {"type": check_type, "patterns": ["Fatal", "Traceback"]}
    if check_type in {"regex_exists", "regex_match"}:
        return {"type": check_type, "target": r"100GE\d+/\d+/\d+"}
    if check_type == "regex_all_of":
        return {"type": check_type, "patterns": ["PHY", "Protocol"]}
    if check_type == "regex_any_of":
        return {"type": check_type, "patterns": ["Fatal", "PHY"]}
    if check_type in {"regex_not_exists", "regex_not_match"}:
        return {"type": check_type, "target": r"Traceback|Fatal"}
    if check_type == "allowed_patterns":
        return {"type": check_type, "source": "stderr", "patterns": [], "ignore_patterns": []}
    if check_type in {"min_output_lines", "min_body_lines"}:
        return {"type": check_type, "target": "2"}
    if check_type == "command_echo_required":
        return {"type": check_type}
    if check_type == "prompt_required":
        return {"type": check_type}
    if check_type in {"interface_status", "interface_status_not"}:
        return {"type": check_type, "fields": ["physical", "protocol"], "forbidden": ["down"]}
    if check_type == "sentinel_seen":
        return {"type": check_type, "target": "PHY"}
    if check_type == "exit_code_in":
        return {"type": check_type, "allowed": [0]}
    if check_type == "pager_exhausted":
        return {"type": check_type}
    raise AssertionError(f"missing SSH result fixture for {check_type}")


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
    assert merged["evidence_checkpoints"][0]["rule_id"] == "ssh.transcript_marker"
    assert merged["rule_pack"]["audit_metadata"]["created_by"] == "bmc-auto-capture-ssh-output-rules"


def test_rulepack_ssh_evidence_checkpoint_warning_is_not_blocking():
    pack = _ssh_rule_pack()
    pack["rule_classes"]["evidence_validation"] = [
        {
            "rule_id": "ssh.no_error_marker",
            "priority": "P2",
            "effect_on_final": "warning",
            "checks": [{"type": "text_not_contains", "target": "ERROR"}],
        }
    ]
    task_def = {
        "task_id": "task.019",
        "task_type": "SSH",
        "execution_mode": "SSH_CMD",
        "command_or_url": "display interface brief",
    }
    merged = adapt_rule_pack_to_task_def(pack, task_def)
    checkpoint = merged["evidence_checkpoints"][0]
    artifacts = ArtifactContext(txt_content="display interface brief\nERROR optional warning marker\n<HUAWEI>")

    evaluation = evaluate_evidence_checkpoints(parse_checkpoint_specs([checkpoint]), artifacts)
    result = evaluation.results[0]
    check_result = check_result_from_condition(
        result,
        stage=CheckStage.RESULT,
        check_id_prefix="ssh.evidence_checkpoint",
        source="evidence_checkpoints",
    )

    assert checkpoint["severity"] == "WARNING"
    assert evaluation.rollup() == "WARN"
    assert result.rule_id == "ssh.no_error_marker"
    assert check_result.status == "WARN"
    assert check_result.severity == "WARNING"
    assert check_result.check_id == "ssh.evidence_checkpoint.ssh.no_error_marker"
    assert check_result.details["priority"] == "P2"


def test_rulepack_capability_registry_matches_bmc_runtime(tmp_path):
    bmc_caps = CAPABILITY_REGISTRY["protocols"]["BMC"]
    ready_types = set()
    for rule_class in ("stage_gate", "action_completion", "content_integrity"):
        ready_types.update(bmc_caps[rule_class]["check_types"])
    ready_specs = parse_ready_specs([_bmc_ready_spec(check_type) for check_type in sorted(ready_types)])

    ready_result = asyncio.run(evaluate_ready_conditions(_FakePage(), ready_specs, protocol="BMC"))

    assert len(ready_result.results) == len(ready_types)
    assert not [r for r in ready_result.results if "unknown type" in r.details]

    artifact = tmp_path / "evidence.txt"
    artifact.write_text("System health ready\n", encoding="utf-8")
    evidence_types = set(bmc_caps["evidence_validation"]["check_types"])
    checkpoint_specs = parse_checkpoint_specs([
        _bmc_evidence_spec(check_type, artifact)
        for check_type in sorted(evidence_types)
    ])
    artifacts = ArtifactContext(
        screenshot_path=str(artifact),
        html_path="",
        txt_path=str(artifact),
        html_text="System health ready",
        txt_content="System health ready",
    )

    checkpoint_result = evaluate_evidence_checkpoints(
        checkpoint_specs,
        artifacts,
        page_text="System health ready",
    )

    assert len(checkpoint_result.results) == len(evidence_types)
    assert not [r for r in checkpoint_result.results if "unknown type" in r.details]


def test_rulepack_capability_registry_matches_ssh_runtime():
    ssh_caps = CAPABILITY_REGISTRY["protocols"]["SSH"]
    result_types = set()
    for rule_class in ("stage_gate", "action_completion", "content_integrity"):
        result_types.update(ssh_caps[rule_class]["check_types"])
    output = """
display interface brief
Interface                   PHY   Protocol Description
100GE1/0/1                  up    up       uplink
<HUAWEI>
[exit_code:0]
"""
    ctx = ResultRuleContext(
        combined_output=output,
        cmd_outputs={"cmd_0": output},
        strategy="interactive_shell",
        resolved_commands=[("cmd_0", "display interface brief")],
        command_or_url="display interface brief",
    )

    parse_failed = []
    for check_type in sorted(result_types):
        evaluation = evaluate_result_rules(
            [{"rule_id": f"capability.{check_type}", "checks": [_ssh_result_check(check_type)]}],
            ctx,
        )
        if evaluation.rule_status == "RULE_PARSE_FAILED":
            parse_failed.append((check_type, evaluation.failure_summary()))

    assert parse_failed == []

    condition_handlers = {
        "text_contains",
        "text_not_contains",
        "regex_match",
        "regex_not_match",
    }
    for protocol in ("SSH", "TELNET"):
        exposed = set(CAPABILITY_REGISTRY["protocols"][protocol]["evidence_validation"]["check_types"])
        assert exposed <= condition_handlers
        specs = parse_checkpoint_specs([
            {"type": check_type, "target": "System health ready"}
            for check_type in sorted(exposed)
        ])
        result = evaluate_evidence_checkpoints(
            specs,
            ArtifactContext(txt_content="System health ready"),
            page_text="System health ready",
        )
        assert not [r for r in result.results if "unknown type" in r.details]


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


def test_rulepack_contracts_describe_workspace_binding():
    app = create_app(ExecutorRuntimeStatusService(executor_id="rulepack-test"))
    client = TestClient(app)

    response = client.get("/executor/v1/contracts/rulepack-import")

    assert response.status_code == 200
    behavior = " ".join(response.json()["validationBehavior"])
    assert "tasks.json" in behavior
    assert "fingerprints" in behavior


def test_rulepack_api_import_rejects_task_type_mismatch(tmp_path):
    _write_tasks_json(tmp_path, {
        "task_id": "task.019",
        "task_name": "Interface brief",
        "task_type": "BMC",
        "execution_mode": "BMC_URL",
        "command_or_url": "/UI/Static/#/navigate/system",
    })
    app = create_app(ExecutorRuntimeStatusService(executor_id="rulepack-test"))
    client = TestClient(app)

    response = client.post("/executor/v1/config/rule-packs:import", json={"rulePacks": [_ssh_rule_pack()]})

    assert response.status_code == 400
    assert response.json()["accepted"] is False
    assert "RULEPACK_TASK_TYPE_MISMATCH" in json.dumps(response.json())


def test_rulepack_api_import_rejects_command_fingerprint_mismatch(tmp_path):
    _write_tasks_json(tmp_path, {
        "task_id": "task.019",
        "task_name": "Interface brief",
        "task_type": "SSH",
        "execution_mode": "SSH_CMD",
        "command_or_url": "display interface brief",
    })
    pack = _ssh_rule_pack()
    pack["applies_to"]["command_fingerprint"] = sha256_text("display vlan brief")
    app = create_app(ExecutorRuntimeStatusService(executor_id="rulepack-test"))
    client = TestClient(app)

    response = client.post("/executor/v1/config/rule-packs:import", json={"rulePacks": [pack]})

    assert response.status_code == 400
    assert response.json()["accepted"] is False
    assert "RULEPACK_FINGERPRINT_MISMATCH" in json.dumps(response.json())


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
