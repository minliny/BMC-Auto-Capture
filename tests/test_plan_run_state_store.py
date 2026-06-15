from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.plan_run_service.state_store import PlanRunStateStore, safe_state_id


def test_state_store_persists_run_and_plan_alias(tmp_path):
    store = PlanRunStateStore(tmp_path)
    state = {
        "planId": "plan-1",
        "runId": "run-1",
        "status": "COMPLETED",
        "items": [],
    }

    store.persist_run_state("run-1", "plan-1", state)

    run_path = tmp_path / "executor_state" / "runs" / "run-1.json"
    latest_path = tmp_path / "executor_state" / "plans" / "plan-1" / "latest_run.json"
    assert json.loads(run_path.read_text(encoding="utf-8")) == state
    assert json.loads(latest_path.read_text(encoding="utf-8")) == state
    assert not list((tmp_path / "executor_state").rglob("*.tmp"))


def test_state_store_loads_only_json_dict_states(tmp_path):
    store = PlanRunStateStore(tmp_path)
    store.persist_run_state("run-1", "plan-1", {"runId": "run-1"})
    runs_dir = tmp_path / "executor_state" / "runs"
    (runs_dir / "not-dict.json").write_text("[]", encoding="utf-8")
    (runs_dir / "broken.json").write_text("{broken", encoding="utf-8")

    states = store.load_run_states()

    assert states == [("run-1.json", {"runId": "run-1"})]


def test_state_store_make_plan_output_root_uses_safe_timestamp(tmp_path):
    store = PlanRunStateStore(tmp_path)

    output_root = Path(store.make_plan_output_root("plan-1", run_ts="20260615_120000"))

    assert output_root == tmp_path / "executor_state" / "outputs" / "plan-1" / "20260615_120000"
    assert output_root.is_dir()


@pytest.mark.parametrize("unsafe", ["", "../x", "/x", r"C:\x", "a/b"])
def test_safe_state_id_rejects_path_traversal(unsafe):
    with pytest.raises(ValueError):
        safe_state_id(unsafe)


def test_safe_state_id_accepts_plain_ids():
    assert safe_state_id(" plan-1 ") == "plan-1"
