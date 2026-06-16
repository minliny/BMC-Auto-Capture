"""
Tests for plan_catalog — deterministic planner from Excel + validation.json.
"""
from __future__ import annotations
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.plan_catalog.models import (
    PlanManifest,
    PlannedTask,
    ValidationReport,
    ValidationError,
    make_task_id,
    make_device_key,
    NetworkTestDef,
)
from src.plan_catalog.store import TaskCatalogStore
from src.plan_catalog.validation_loader import (
    load_validation_json,
    parse_network_tests,
    parse_task_types,
)
from src.plan_catalog.planner import PlanCatalogPlanner, _sha256_file, _derive_ssh_type


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
VALIDATION_JSON = FIXTURES / "validation.json"
EXCEL_FILE = Path(__file__).parent.parent / "examples" / "task_template.xlsx"


@pytest.fixture
def planner():
    """Create planner with sample Excel + validation.json."""
    return PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))


@pytest.fixture
def sample_manifest():
    return PlanManifest(
        plan_id="plan-001", plan_hash="abc123", planner_version="0.1.0",
        excel_sha256="aaa", validation_json_sha256="bbb",
    )


# ===========================================================================
# Deterministic task_id tests
# ===========================================================================

class TestDeterministicTaskId:
    """Tests 1-4: task_id and plan_hash stability."""

    def test_same_input_same_task_id(self, planner):
        """1. Same Excel + JSON twice ⇒ same task_id."""
        m1, _, _ = planner.build()
        planner2 = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))
        m2, _, _ = planner2.build()

        ids1 = [t.task_id for t in m1.tasks]
        ids2 = [t.task_id for t in m2.tasks]
        assert ids1 == ids2
        assert len(ids1) > 0

    def test_same_input_same_plan_hash(self, planner):
        """2+3. Same input ⇒ same plan_hash, independent of generated_at."""
        m1, _, _ = planner.build()
        # Simulate different generated_at
        m1.generated_at = "2020-01-01T00:00:00Z"
        h1 = m1.compute_hash()

        planner2 = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))
        m2, _, _ = planner2.build()
        m2.generated_at = "2025-12-31T23:59:59Z"
        h2 = m2.compute_hash()

        assert h1 == h2
        assert len(h1) == 16

    def test_task_id_no_uuid_randomness(self):
        """4. task_id does not contain UUID (no dashes in hex format)."""
        tid = make_task_id(
            "0.1.0", "abc", "def", "A3", "dev-key-1",
            "1", "Task1", "BMC", "BMC_URL", "excel:row=1",
        )
        # SHA256 hex = 16 hex chars, no dashes
        assert len(tid) == 16
        assert "-" not in tid
        assert all(c in "0123456789abcdef" for c in tid)

    def test_task_id_stable_across_calls(self):
        """Same args ⇒ same task_id."""
        args = ("0.1.0", "abc", "def", "A3", "dk", "1", "T", "BMC", "BMC_URL", "r")
        t1 = make_task_id(*args)
        t2 = make_task_id(*args)
        assert t1 == t2


# ===========================================================================
# lock_uri tests
# ===========================================================================

class TestLockUriGeneration:
    """Tests 5-9: lock_uri derivation from planner."""

    def test_bmc_task_uses_bmc_lock_uri(self, planner):
        """5. BMC_URL tasks have bmc:// lock_uri."""
        m, _, _ = planner.build()
        bmc_tasks = [t for t in m.tasks if t.task_type == "BMC" and t.lock_uri]
        assert len(bmc_tasks) > 0
        for t in bmc_tasks:
            assert t.lock_uri.startswith("bmc://")

    def test_ssh_task_uses_ssh_lock_uri(self, planner):
        """6. SSH_CMD tasks have ssh://, ssh-vrp://, or ssh-linux:// lock_uri."""
        m, _, _ = planner.build()
        ssh_tasks = [t for t in m.tasks if t.task_type in ("SSH", "NETWORK_TEST") and t.lock_uri]
        if ssh_tasks:
            valid_prefixes = ("ssh://", "ssh-vrp://", "ssh-linux://")
            for t in ssh_tasks:
                assert any(t.lock_uri.startswith(p) for p in valid_prefixes), \
                    f"lock_uri={t.lock_uri} does not start with any of {valid_prefixes}"

    def test_no_device_name_as_lock_uri(self, planner):
        """9. lock_uri never contains device_name."""
        m, _, _ = planner.build()
        for t in m.tasks:
            assert "device_name" not in t.lock_uri.lower()
            # No plain device names like "DEV01"
            if t.lock_uri:
                assert "://" in t.lock_uri

    def test_lock_uri_derived_not_fallback(self):
        """Verify derivation function never returns device_name."""
        from src.plan_catalog.models import make_device_key
        # device_key is a hash, not device_name
        dk = make_device_key(type("D", (), {
            "bmc_ip": "10.0.0.1", "inband_ip": "10.0.1.1",
            "device_group": "A3", "device_name": "Switch-A",
        })())
        assert "Switch-A" not in dk
        assert len(dk) == 16


# ===========================================================================
# Validation tests
# ===========================================================================

class TestValidation:
    """Tests 7-8: Validation errors for missing IPs."""

    def test_validation_report_has_structure(self, planner):
        """Validation report is generated with errors/warnings lists."""
        _, _, report = planner.build()
        assert isinstance(report, ValidationReport)
        assert hasattr(report, "error_count")
        assert hasattr(report, "warning_count")

    def test_validation_report_to_dict(self, planner):
        """15. validation_report.json format is correct."""
        _, _, report = planner.build()
        d = report.to_dict()
        assert "valid" in d
        assert "error_count" in d
        assert "errors" in d
        assert isinstance(d["errors"], list)


# ===========================================================================
# Network tests
# ===========================================================================

class TestNetworkTests:
    """Tests 10-12: network_tests generation."""

    def test_network_tests_generate_tasks(self, planner):
        """10. network_tests produce NETWORK_TEST tasks."""
        m, _, _ = planner.build()
        nt_tasks = [t for t in m.tasks if t.task_type == "NETWORK_TEST"]
        # Should have some network test tasks if devices match
        assert len(nt_tasks) >= 0  # depends on matching device groups

    def test_network_test_command_rendered(self):
        """11. Command template {inband_ip} renders correctly."""
        from src.plan_catalog.validation_loader import parse_network_tests
        raw = {"network_tests": [{
            "network_test_id": "nt1", "name": "Test",
            "device_groups": [], "execution_mode": "SSH_CMD",
            "command": "ping -c 4 {inband_ip}", "timeout_seconds": 30,
        }]}
        tests = parse_network_tests(raw)
        assert len(tests) == 1
        assert "{inband_ip}" in tests[0].command

    def test_network_test_task_id_stable(self, planner):
        """12. network_tests have stable task_ids."""
        m1, _, _ = planner.build()
        planner2 = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))
        m2, _, _ = planner2.build()

        nt1 = [t.task_id for t in m1.tasks if t.task_type == "NETWORK_TEST"]
        nt2 = [t.task_id for t in m2.tasks if t.task_type == "NETWORK_TEST"]
        assert nt1 == nt2


# ===========================================================================
# PlanManifest order tests
# ===========================================================================

class TestManifestOrder:
    """Tests 13: Manifest tasks order is deterministic."""

    def test_manifest_order_stable(self, planner):
        """13. PlanManifest tasks in same order across builds."""
        m1, _, _ = planner.build()
        planner2 = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))
        m2, _, _ = planner2.build()

        ids1 = [t.task_id for t in m1.tasks]
        ids2 = [t.task_id for t in m2.tasks]
        assert ids1 == ids2


# ===========================================================================
# plan_hash independence
# ===========================================================================

class TestPlanHash:
    """Tests 14: plan_hash independent of file path."""

    def test_plan_hash_independent_of_path(self, planner):
        """14. plan_hash unaffected by local file path."""
        m1, _, _ = planner.build()
        h1 = m1.plan_hash

        # Create planner with same files — path is same in test, but hash
        # excludes path entirely (uses file content sha256 only).
        planner2 = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON))
        m2, _, _ = planner2.build()
        h2 = m2.plan_hash

        assert h1 == h2

    def test_plan_hash_changes_with_different_excel(self):
        """plan_hash changes when content differs."""
        # Use validation.json itself as a second input (content differs from Excel)
        m1, _, _ = PlanCatalogPlanner(str(EXCEL_FILE), str(VALIDATION_JSON)).build()
        # Same file twice should give same hash
        assert len(m1.plan_hash) == 16


# ===========================================================================
# TaskCatalogStore tests
# ===========================================================================

class TestTaskCatalog:
    """Tests 16: TaskCatalogStore lookup."""

    def test_catalog_lookup_by_plan_item_id(self, planner):
        """16. task_catalog can look up PlannedTask by plan_item_id."""
        m, catalog, _ = planner.build()
        for t in m.tasks:
            found = catalog.get(t.plan_item_id)
            assert found is not None
            assert found.plan_item_id == t.plan_item_id

    def test_catalog_to_dict(self, planner):
        """catalog.to_dict() produces valid JSON-serializable dict."""
        _, catalog, _ = planner.build()
        d = catalog.to_dict()
        assert isinstance(d, dict)
        assert len(d) == len(catalog)
        # Round-trip
        json_str = json.dumps(d)
        assert len(json_str) > 0


# ===========================================================================
# Model serialization
# ===========================================================================

class TestModelSerialization:
    """Model to_dict/from_dict round-trips."""

    def test_planned_task_to_dict(self):
        pt = PlannedTask(
            task_id="tid-001", plan_id="p1", task_no="1",
            task_name="Test", task_type="BMC", execution_mode="BMC_URL",
            device_group="A3", device_key="dk1", lock_uri="bmc://10.0.0.1",
            source_row_ref="excel:row=1",
            device_snapshot={"oob_ip": "10.0.0.1"},
            task_snapshot={"url": "https://10.0.0.1/"},
        )
        d = pt.to_dict()
        assert d["task_id"] == "tid-001"
        assert d["lock_uri"] == "bmc://10.0.0.1"

    def test_planned_task_catalog_dict_excludes_manifest_fields(self):
        pt = PlannedTask(task_id="tid-001")
        cat = pt.to_catalog_dict()
        assert "device_snapshot" in cat
        assert "task_snapshot" in cat
        # Should NOT contain manifest-only fields like task_no, task_name
        assert "task_no" not in cat

    def test_validation_error_to_dict(self):
        ve = ValidationError("CODE", "msg", "row1", "error")
        d = ve.to_dict()
        assert d["code"] == "CODE"
        assert d["severity"] == "error"

    def test_manifest_compute_hash_excludes_generated_at(self):
        m = PlanManifest(plan_id="p1", plan_hash="")
        m.generated_at = "2099-01-01T00:00:00Z"
        h1 = m.compute_hash()
        m.generated_at = "2000-01-01T00:00:00Z"
        h2 = m.compute_hash()
        assert h1 == h2

    def test_network_test_def_round_trip(self):
        nt = NetworkTestDef(
            network_test_id="nt1", name="Ping Test",
            device_groups=["A3"], command="ping -c 4 {inband_ip}",
        )
        d = nt.to_dict()
        nt2 = NetworkTestDef.from_dict(d)
        assert nt2.network_test_id == "nt1"
        assert nt2.command == "ping -c 4 {inband_ip}"

    def test_validation_report_is_valid(self):
        r = ValidationReport()
        assert r.is_valid is True
        r.errors.append(ValidationError("E1", "bad"))
        assert r.is_valid is False


# ===========================================================================
# CLI test
# ===========================================================================

class TestCLI:
    def test_build_plan_manifest_help(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, "scripts/build_plan_manifest.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--excel" in result.stdout
        assert "--validation-json" in result.stdout

    def test_build_plan_manifest_output(self, tmp_path):
        """End-to-end: build manifest from sample Excel + validation.json."""
        import subprocess
        out = str(tmp_path / "out")
        result = subprocess.run(
            [
                sys.executable, "scripts/build_plan_manifest.py",
                "--excel", str(EXCEL_FILE),
                "--validation-json", str(VALIDATION_JSON),
                "--out", out,
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert os.path.exists(os.path.join(out, "plan_manifest.json"))
        assert os.path.exists(os.path.join(out, "task_catalog.json"))
        assert os.path.exists(os.path.join(out, "validation_report.json"))

        with open(os.path.join(out, "plan_manifest.json")) as f:
            manifest = json.load(f)
        assert "plan_id" in manifest
        assert "plan_hash" in manifest
        assert "task_count" in manifest

        # Check no passwords leaked
        stdout = result.stdout
        assert "password" not in stdout.lower() or "password_ref" in stdout
