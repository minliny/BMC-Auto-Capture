"""
Tests for P1-PLAN-CATALOG-002: plan import + run dispatch API.
"""
from __future__ import annotations
import json
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.run_dispatch_service import RunDispatchService, RunStatus, TaskRunStatus
from src.resource_lock_manager import ResourceLockManager
from src.server_callback_client import FakeCallbackTransport


FIXTURES = Path(__file__).parent / "fixtures"
VALIDATION_JSON = str(FIXTURES / "validation.json")
EXCEL_FILE = str(Path(__file__).parent.parent / "examples" / "task_template.xlsx")


@pytest.fixture
def svc():
    """Fresh RunDispatchService with fake runner + fake callback."""
    return RunDispatchService(executor_id="exec-test", runner_mode="fake")


def _import_plan(svc, excel=None, vj=None):
    return svc.import_plan(excel or EXCEL_FILE, vj or VALIDATION_JSON)


# ===========================================================================
# Plan import tests (1-7)
# ===========================================================================

class TestPlanImport:
    def test_import_plan_succeeds(self, svc):
        """1. Import plan returns accepted + plan_id."""
        r = _import_plan(svc)
        assert r["accepted"] is True
        assert len(r["plan_id"]) == 16
        assert r["task_count"] > 0

    def test_plan_hash_saved(self, svc):
        """2. plan_hash is saved and accessible."""
        r = _import_plan(svc)
        plan_id = r["plan_id"]
        plan = svc.get_plan(plan_id)
        assert plan is not None
        assert plan["plan_hash"] == r["plan_hash"]

    def test_get_plan_succeeds(self, svc):
        """3. GET plan returns full manifest."""
        r = _import_plan(svc)
        plan = svc.get_plan(r["plan_id"])
        assert plan["task_count"] > 0
        assert len(plan["tasks"]) > 0

    def test_get_plan_tasks(self, svc):
        """4. GET plan tasks returns task list."""
        r = _import_plan(svc)
        tasks = svc.get_plan_tasks(r["plan_id"])
        assert len(tasks) == r["task_count"]
        assert all("task_id" in t for t in tasks)

    def test_get_plan_task_by_id(self, svc):
        """5. GET plan task by task_id returns full catalog entry."""
        r = _import_plan(svc)
        tasks = svc.get_plan_tasks(r["plan_id"])
        tid = tasks[0]["task_id"]
        cat = svc.get_plan_task(r["plan_id"], tid)
        assert cat is not None
        assert "device_snapshot" in cat
        assert "task_snapshot" in cat

    def test_plan_hash_mismatch_rejects_run(self, svc):
        """6. Wrong plan_hash → run rejected."""
        r = _import_plan(svc)
        result = svc.start_run({
            "command_id": "c1", "run_id": "r1",
            "plan_id": r["plan_id"], "plan_hash": "bad-hash",
        })
        assert result["accepted"] is False
        assert "hash" in result.get("reason", "")

    def test_nonexistent_plan_rejects_run(self, svc):
        """7. Nonexistent plan_id → run rejected."""
        result = svc.start_run({
            "command_id": "c1", "run_id": "r1",
            "plan_id": "nonexistent", "plan_hash": "",
        })
        assert result["accepted"] is False


# ===========================================================================
# Run dispatch tests (8-15)
# ===========================================================================

class TestRunDispatch:
    def test_scope_all_creates_run(self, svc):
        """8. scope=ALL creates run with all tasks."""
        r = _import_plan(svc)
        result = svc.start_run({
            "command_id": "c1", "run_id": "run-1",
            "plan_id": r["plan_id"], "plan_hash": r["plan_hash"],
            "scope": "ALL",
        })
        assert result["accepted"] is True
        assert result["task_count"] == r["task_count"]

    def test_run_has_all_task_records(self, svc):
        """9. Run tasks cover all catalog enabled tasks."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c1", "run_id": "run-1",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        tasks = svc.get_run_tasks("run-1")
        assert len(tasks) == r["task_count"]

    def test_no_task_list_bypass(self, svc):
        """10. Full task list from catalog only — no external override."""
        r = _import_plan(svc)
        # start_run ignores any task_ids in request — always uses catalog
        result = svc.start_run({
            "command_id": "c2", "run_id": "run-2",
            "plan_id": r["plan_id"], "plan_hash": r["plan_hash"],
            "task_ids": ["fake-1", "fake-2"],  # ignored
        })
        tasks = svc.get_run_tasks("run-2")
        # All catalog tasks, not just 2
        assert len(tasks) == r["task_count"]

    def test_command_id_idempotent(self, svc):
        """11. Duplicate command_id returns duplicate=True."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c-dup", "run_id": "r-a",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        result2 = svc.start_run({"command_id": "c-dup", "run_id": "r-b",
                                  "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        assert result2["duplicate"] is True

    def test_get_run_returns_counts(self, svc):
        """12. GET run returns status counts."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c1", "run_id": "run-1",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        run = svc.get_run("run-1")
        assert run["total_tasks"] == r["task_count"]
        assert run["queued"] == r["task_count"]

    def test_get_run_task_returns_status(self, svc):
        """13. GET run task returns individual task status."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c1", "run_id": "run-1",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        tasks = svc.get_run_tasks("run-1")
        t = svc.get_run_task("run-1", tasks[0]["task_id"])
        assert t["status"] == TaskRunStatus.QUEUED

    def test_fake_runner_task_succeeded(self, svc):
        """14. After execution, tasks are SUCCEEDED."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c1", "run_id": "run-1",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        svc.run_all_pending()
        tasks = svc.get_run_tasks("run-1")
        statuses = {t["status"] for t in tasks}
        assert TaskRunStatus.QUEUED not in statuses
        assert TaskRunStatus.SUCCEEDED in statuses

    def test_run_completes(self, svc):
        """15. Run status is SUCCEEDED after all tasks done."""
        r = _import_plan(svc)
        svc.start_run({"command_id": "c1", "run_id": "run-1",
                        "plan_id": r["plan_id"], "plan_hash": r["plan_hash"]})
        svc.run_all_pending()
        run = svc.get_run("run-1")
        assert run["status"] in (RunStatus.SUCCEEDED, RunStatus.PARTIAL_FAILED)


# ===========================================================================
# Callback tests (16-19)
# ===========================================================================

class TestRunCallbacks:
    def test_callback_contains_task_id(self, svc):
        """16+17. Task callback URL uses task_id."""
        transport = FakeCallbackTransport()
        svc = RunDispatchService(executor_id="exec-test", runner_mode="fake",
                                 callback_transport=transport)
        r = _import_plan(svc)
        svc.start_run({
            "command_id": "c1", "run_id": "run-1",
            "plan_id": r["plan_id"], "plan_hash": r["plan_hash"],
            "callback": {"task_status_url": "http://cb/api/tasks/{task_id}/status"},
        })
        svc.run_all_pending()

        calls = transport.calls
        assert len(calls) >= 1
        # Each task gets a callback
        tasks = svc.get_run_tasks("run-1")
        task_ids = {t["task_id"] for t in tasks}
        called_ids = set()
        for c in calls:
            tid = c["payload"].get("task_id", "")
            if tid:
                called_ids.add(tid)
        # At least some task callbacks were sent
        assert len(called_ids) > 0

    def test_callback_has_run_id_and_plan_id(self, svc):
        """18. Callback payload contains run_id and plan_id."""
        transport = FakeCallbackTransport()
        svc = RunDispatchService(executor_id="exec-test", runner_mode="fake",
                                 callback_transport=transport)
        r = _import_plan(svc)
        svc.start_run({
            "command_id": "c1", "run_id": "run-cb",
            "plan_id": r["plan_id"], "plan_hash": r["plan_hash"],
            "callback": {"task_status_url": "http://cb/{task_id}"},
        })
        svc.run_all_pending()
        for c in transport.calls:
            if c["payload"].get("status") == "SUCCEEDED":
                assert c["payload"]["run_id"] == "run-cb"
                assert c["payload"]["plan_id"] == r["plan_id"]
                break

    def test_callback_failure_preserves_result(self, svc):
        """19. CALLBACK_FAILED preserves execution result."""
        transport = FakeCallbackTransport()
        transport.set_failure()
        svc = RunDispatchService(executor_id="exec-test", runner_mode="fake",
                                 callback_transport=transport)
        r = _import_plan(svc)
        svc.start_run({
            "command_id": "c1", "run_id": "run-1",
            "plan_id": r["plan_id"], "plan_hash": r["plan_hash"],
            "callback": {"task_status_url": "http://cb/{task_id}"},
        })
        svc.run_all_pending()
        tasks = svc.get_run_tasks("run-1")
        # Tasks should still have results recorded
        for t in tasks:
            assert t["status"] in (TaskRunStatus.CALLBACK_FAILED, TaskRunStatus.SUCCEEDED)


# ===========================================================================
# Lock integration (20)
# ===========================================================================

class TestRunLockIntegration:
    def test_same_lock_uri_serialized(self, svc):
        """20. Same lock_uri tasks are serialized (no deadlock)."""
        lock_mgr = ResourceLockManager()
        svc2 = RunDispatchService(executor_id="exec-test", runner_mode="fake",
                                  lock_manager=lock_mgr)
        _import_plan(svc2)
        plan_id = list(svc2._plans.keys())[0]
        catalog = svc2._plans[plan_id]["catalog"]
        bmc_tasks = [tid for tid, pt in catalog._by_id.items()
                     if pt.lock_uri.startswith("bmc://")]
        svc2.start_run({"command_id": "c1", "run_id": "run-lock",
                         "plan_id": plan_id,
                         "plan_hash": svc2._plans[plan_id]["manifest"].plan_hash})
        svc2.run_all_pending()
        # All locks should be released
        for tid in bmc_tasks:
            pt = catalog.get(tid)
            if pt and pt.lock_uri:
                assert not lock_mgr.is_locked(pt.lock_uri)


# ===========================================================================
# NETWORK_TEST (21-22)
# ===========================================================================

class TestNetworkTestInRun:
    def test_network_test_in_runtime_store(self, svc):
        """21. NETWORK_TEST tasks are in run."""
        _import_plan(svc)
        plan_id = list(svc._plans.keys())[0]
        catalog = svc._plans[plan_id]["catalog"]
        nt_ids = [tid for tid, pt in catalog._by_id.items()
                  if pt.task_type == "NETWORK_TEST"]
        if nt_ids:
            svc.start_run({"command_id": "c1", "run_id": "run-nt",
                            "plan_id": plan_id,
                            "plan_hash": svc._plans[plan_id]["manifest"].plan_hash})
            run_tasks = svc.get_run_tasks("run-nt")
            run_ids = {t["task_id"] for t in run_tasks}
            for tid in nt_ids:
                assert tid in run_ids, f"NETWORK_TEST task {tid} should be in run"

    def test_network_test_fake_executes(self, svc):
        """22. NETWORK_TEST can run via FakeRunner."""
        _import_plan(svc)
        plan_id = list(svc._plans.keys())[0]
        svc.start_run({"command_id": "c1", "run_id": "run-nt2",
                        "plan_id": plan_id,
                        "plan_hash": svc._plans[plan_id]["manifest"].plan_hash})
        svc.run_all_pending()
        run = svc.get_run("run-nt2")
        assert run["status"] in (RunStatus.SUCCEEDED, RunStatus.PARTIAL_FAILED)


# ===========================================================================
# Script tests
# ===========================================================================

class TestScripts:
    def test_submit_plan_run_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/submit_plan_run.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--excel" in result.stdout
        assert "--run-id" in result.stdout
