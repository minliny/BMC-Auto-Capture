"""
Unit tests for api_models — lock_uri derivation, model constraints, serialization.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.api_models.lock_uri import (
    derive_lock_uri,
    derive_lock_uri_from_device,
    is_valid_lock_uri,
    LockUriDerivationError,
)
from src.api_models.command import Command, CommandType, CommandStatus
from src.api_models.job import Job, JobStatus, StepResult
from src.api_models.device_snapshot import DeviceSnapshot, ResourceLockEntry
from src.api_models.executor import Executor, ExecutorStatus, ExecutorCapabilities
from src.api_models.task_snapshot import TaskSnapshot, TaskRule, TaskRuleCheck
from src.api_models.artifact import Artifact, ArtifactType, ArtifactStatus
from src.api_models.error_info import ErrorInfo
from src.api_models.resource_lock import ResourceLock, LockType
from src.models.device import Device


# ---------------------------------------------------------------------------
# lock_uri derivation tests
# ---------------------------------------------------------------------------

class TestLockUriDerivation:
    """Test lock_uri derivation for all 4 types + error cases."""

    def test_bmc_url_derives_bmc_lock_uri(self):
        uri = derive_lock_uri(oob_ip="10.0.0.1", execution_mode="BMC_URL")
        assert uri == "bmc://10.0.0.1"

    def test_bmc_actions_derives_bmc_lock_uri(self):
        uri = derive_lock_uri(oob_ip="10.0.0.1", execution_mode="BMC_ACTIONS")
        assert uri == "bmc://10.0.0.1"

    def test_ssh_cmd_default_derives_ssh_lock_uri(self):
        uri = derive_lock_uri(inband_ip="10.0.1.1", execution_mode="SSH_CMD")
        assert uri == "ssh://10.0.1.1"

    def test_ssh_cmd_linux_derives_ssh_linux_lock_uri(self):
        uri = derive_lock_uri(
            inband_ip="10.0.1.1", execution_mode="SSH_CMD", ssh_type="SSH_LINUX"
        )
        assert uri == "ssh-linux://10.0.1.1"

    def test_ssh_cmd_vrp_derives_ssh_vrp_lock_uri(self):
        uri = derive_lock_uri(
            inband_ip="10.0.1.1", execution_mode="SSH_CMD", ssh_type="SSH_VRP"
        )
        assert uri == "ssh-vrp://10.0.1.1"

    def test_explicit_lock_type_bmc(self):
        uri = derive_lock_uri(oob_ip="10.0.0.1", lock_type="BMC")
        assert uri == "bmc://10.0.0.1"

    def test_explicit_lock_type_ssh_vrp(self):
        uri = derive_lock_uri(inband_ip="10.0.1.1", lock_type="SSH_VRP")
        assert uri == "ssh-vrp://10.0.1.1"

    def test_missing_oob_ip_for_bmc_raises(self):
        with pytest.raises(LockUriDerivationError, match="oob_ip"):
            derive_lock_uri(oob_ip="", execution_mode="BMC_URL")

    def test_missing_inband_ip_for_ssh_raises(self):
        with pytest.raises(LockUriDerivationError, match="inband_ip"):
            derive_lock_uri(inband_ip="", execution_mode="SSH_CMD")

    def test_missing_oob_ip_explicit_lock_type_raises(self):
        with pytest.raises(LockUriDerivationError, match="oob_ip"):
            derive_lock_uri(oob_ip="", lock_type="BMC")

    def test_missing_inband_ip_explicit_lock_type_raises(self):
        with pytest.raises(LockUriDerivationError, match="inband_ip"):
            derive_lock_uri(inband_ip="", lock_type="SSH_VRP")

    def test_no_fallback_to_device_name(self):
        """lock_uri must never use device_name as fallback."""
        with pytest.raises(LockUriDerivationError):
            derive_lock_uri(oob_ip="", inband_ip="", execution_mode="BMC_URL")

    def test_no_fallback_to_device_name_explicit(self):
        with pytest.raises(LockUriDerivationError):
            derive_lock_uri(oob_ip="", lock_type="BMC")

    def test_normalize_strips_spaces(self):
        uri = derive_lock_uri(oob_ip=" 10.0.0.1 ", execution_mode="BMC_URL")
        assert uri == "bmc://10.0.0.1"
        assert " " not in uri

    def test_is_valid_lock_uri(self):
        assert is_valid_lock_uri("bmc://10.0.0.1")
        assert is_valid_lock_uri("ssh://10.0.1.1")
        assert is_valid_lock_uri("ssh-vrp://10.0.1.1")
        assert is_valid_lock_uri("ssh-linux://10.0.1.1")

    def test_is_valid_rejects_spaces(self):
        assert not is_valid_lock_uri("bmc:// 10.0.0.1")
        assert not is_valid_lock_uri("bmc://10.0.0.1 ")

    def test_is_valid_rejects_device_name(self):
        assert not is_valid_lock_uri("device://switch-a")
        assert not is_valid_lock_uri("Switch-A")

    def test_is_valid_rejects_empty(self):
        assert not is_valid_lock_uri("")
        assert not is_valid_lock_uri("bmc://")

    def test_derive_from_device_object(self):
        dev = Device(
            row_index=0,
            device_name="Switch-A",
            device_group="core",
            bmc_ip="10.0.0.1",
            bmc_username="admin",
            bmc_password="pw",
            inband_ip="10.0.1.1",
            inband_username="netadmin",
            inband_password="pw",
        )
        uri = derive_lock_uri_from_device(dev, execution_mode="BMC_URL")
        assert uri == "bmc://10.0.0.1"

    def test_device_lock_uri_bmc_property(self):
        dev = Device(
            row_index=0,
            device_name="Switch-A",
            device_group="core",
            bmc_ip="10.0.0.1",
            bmc_username="admin",
            bmc_password="pw",
        )
        assert dev.lock_uri_bmc == "bmc://10.0.0.1"

    def test_device_lock_uri_bmc_missing_ip_raises(self):
        dev = Device(
            row_index=0,
            device_name="Switch-A",
            device_group="core",
            bmc_ip="",
            bmc_username="admin",
            bmc_password="pw",
        )
        with pytest.raises(ValueError, match="bmc_ip"):
            _ = dev.lock_uri_bmc

    def test_device_lock_uri_ssh_property(self):
        dev = Device(
            row_index=0,
            device_name="Switch-A",
            device_group="core",
            bmc_ip="",
            bmc_username="admin",
            bmc_password="pw",
            inband_ip="10.0.1.1",
            inband_username="u",
            inband_password="p",
        )
        assert dev.ssh_type == "SSH_LINUX"
        assert dev.lock_uri_ssh == "ssh-linux://10.0.1.1"

    def test_device_lock_uri_ssh_vrp_from_group(self):
        dev = Device(
            row_index=0,
            device_name="Switch-A",
            device_group="L1",
            bmc_ip="",
            bmc_username="admin",
            bmc_password="pw",
            inband_ip="10.0.1.1",
            inband_username="u",
            inband_password="p",
        )
        assert dev.ssh_type == "SSH_VRP"
        assert dev.lock_uri_ssh == "ssh-vrp://10.0.1.1"


# ---------------------------------------------------------------------------
# Model constraint tests
# ---------------------------------------------------------------------------

class TestModelConstraints:
    """Verify API contract constraints on models."""

    def test_command_must_have_expires_at(self):
        cmd = Command(
            command_id="cmd-001",
            command_type=CommandType.ASSIGN_JOB,
            created_at="2026-06-08T10:00:00Z",
            expires_at="2026-06-08T10:05:00Z",
        )
        assert cmd.expires_at == "2026-06-08T10:05:00Z"
        assert cmd.command_id == "cmd-001"

    def test_command_is_expired_when_past_expiry(self):
        cmd = Command(
            command_id="cmd-old",
            command_type=CommandType.PING,
            expires_at="2020-01-01T00:00:00Z",
        )
        assert cmd.is_expired(now="2026-06-08T10:00:00Z")

    def test_command_not_expired_without_expires_at(self):
        cmd = Command(
            command_id="cmd-forever",
            command_type=CommandType.PING,
            expires_at="",
        )
        assert not cmd.is_expired()

    def test_job_uses_task_snapshot_not_command(self):
        """Job must have task_snapshot field, NOT a command field."""
        job = Job(
            job_id="job-001",
            task_snapshot={"task_id": "task-001", "task_name": "BMC 登录检查"},
        )
        d = job.to_dict()
        assert "task_snapshot" in d
        assert "command" not in d

    def test_job_from_dict_preserves_task_snapshot(self):
        data = {
            "job_id": "job-001",
            "task_snapshot": {"task_id": "task-001", "task_name": "BMC Login"},
        }
        job = Job.from_dict(data)
        assert job.task_snapshot["task_id"] == "task-001"
        # Round-trip
        d2 = job.to_dict()
        assert d2["task_snapshot"]["task_name"] == "BMC Login"

    def test_device_snapshot_no_plaintext_password_fields(self):
        """DeviceSnapshot must not have 'password' fields — only password_ref."""
        ds = DeviceSnapshot(
            device_id="dev-001",
            bmc_password_ref="secret://bmc/switch-a",
            ssh_password_ref="secret://ssh/switch-a",
        )
        d = ds.to_dict()
        # Should have refs
        assert d["bmc_password_ref"] == "secret://bmc/switch-a"
        assert d["ssh_password_ref"] == "secret://ssh/switch-a"
        # Must NOT have plaintext password keys
        assert "bmc_password" not in d
        assert "ssh_password" not in d
        assert "password" not in d

    def test_device_snapshot_from_dict_no_password_leak(self):
        data = {
            "device_id": "dev-001",
            "bmc_password_ref": "secret://bmc/x",
            "ssh_password_ref": "secret://ssh/x",
            # Attacker tries to inject plaintext password
            "bmc_password": "should-not-appear",
        }
        ds = DeviceSnapshot.from_dict(data)
        d = ds.to_dict()
        assert "bmc_password" not in d

    def test_resource_lock_entry_exclusive_default(self):
        rle = ResourceLockEntry(
            lock_uri="bmc://10.0.0.1",
            lock_type="BMC",
        )
        assert rle.lock_exclusive is True


# ---------------------------------------------------------------------------
# Round-trip serialization tests
# ---------------------------------------------------------------------------

class TestSerialization:
    """to_dict / from_dict round-trip for all models."""

    def test_executor_roundtrip(self):
        caps = ExecutorCapabilities(
            max_bmc_workers=4, max_ssh_workers=8,
            bmc_worker_slots_free=2, ssh_worker_slots_free=5,
            supported_protocols=["BMC", "SSH", "SSH_VRP"],
            known_lock_uris=["bmc://10.0.0.1"],
        )
        e = Executor(
            executor_id="exec-01", hostname="PC1", ip="10.0.1.100",
            version="0.2.4", status=ExecutorStatus.ONLINE, capabilities=caps,
        )
        d = e.to_dict()
        e2 = Executor.from_dict(d)
        assert e2.executor_id == "exec-01"
        assert e2.status == ExecutorStatus.ONLINE
        assert e2.capabilities.max_bmc_workers == 4
        assert e2.capabilities.known_lock_uris == ["bmc://10.0.0.1"]

    def test_device_snapshot_roundtrip(self):
        ds = DeviceSnapshot(
            device_id="dev-001", device_name="Switch-A",
            oob_ip="10.0.0.1", inband_ip="10.0.1.1",
            ssh_type="SSH_VRP",
            bmc_password_ref="secret://bmc/x",
            resource_locks=[
                ResourceLockEntry("bmc://10.0.0.1", "BMC", "oob", True),
                ResourceLockEntry("ssh-vrp://10.0.1.1", "SSH_VRP", "inband", True),
            ],
        )
        d = ds.to_dict()
        ds2 = DeviceSnapshot.from_dict(d)
        assert ds2.device_id == "dev-001"
        assert ds2.ssh_type == "SSH_VRP"
        assert len(ds2.resource_locks) == 2
        assert ds2.resource_locks[0].lock_uri == "bmc://10.0.0.1"

    def test_task_snapshot_roundtrip(self):
        result_rules = [
            {"name": "ssh output", "enabled": True, "checks": [{"type": "contains", "target": "OK"}]},
        ]
        ts = TaskSnapshot(
            task_id="task-001", task_name="BMC Login",
            task_type="BMC", execution_mode="BMC_URL",
            command_or_url="https://10.0.0.1/login",
            rules_json='[{"name":"page","checks":[{"type":"text_exists","target":"登录"}]}]',
            rules=[
                TaskRule(
                    rule_name="check_login",
                    checks=[TaskRuleCheck("text_exists", "", "登录")],
                ),
            ],
            result_rules=result_rules,
            ssh_rules=[{"name": "legacy ssh", "checks": [{"type": "min_output_lines", "expect": 1}]}],
            task_def={"stderr_fail_patterns": ["fatal"]},
        )
        d = ts.to_dict()
        ts2 = TaskSnapshot.from_dict(d)
        assert ts2.task_id == "task-001"
        assert ts2.execution_mode == "BMC_URL"
        assert ts2.rules_json
        assert len(ts2.rules) == 1
        assert ts2.rules[0].checks[0].type == "text_exists"
        assert ts2.result_rules == result_rules
        assert ts2.ssh_rules[0]["name"] == "legacy ssh"
        assert ts2.task_def["stderr_fail_patterns"] == ["fatal"]

    def test_task_snapshot_ssh_profile_roundtrip(self):
        ts = TaskSnapshot(
            task_id="task-ssh",
            task_name="Linux terminal evidence",
            task_type="SSH",
            execution_mode="SSH_CMD",
            command_or_url="uname -a",
            ssh_profile="linux",
            ssh_evidence_mode="terminal",
            ssh_transport="terminal_session",
            artifact_profile="fast",
            per_group_timeout_seconds={"A3": 900, "L1": 60},
        )
        d = ts.to_dict()
        ts2 = TaskSnapshot.from_dict(d)
        assert d["ssh_profile"] == "linux"
        assert d["ssh_evidence_mode"] == "terminal"
        assert d["per_group_timeout_seconds"] == {"A3": 900, "L1": 60}
        assert ts2.ssh_transport == "terminal_session"
        assert ts2.artifact_profile == "fast"
        assert ts2.per_group_timeout_seconds == {"A3": 900, "L1": 60}

    def test_job_roundtrip(self):
        job = Job(
            job_id="job-001", run_id="run-001",
            device_id="dev-001", task_id="task-001",
            attempt=1, status=JobStatus.QUEUED,
            resource_lock_uri="bmc://10.0.0.1",
            task_snapshot={"task_id": "task-001"},
            device_snapshot={"device_id": "dev-001"},
            step_results=[
                StepResult(step_index=0, step_name="login", status="SUCCEEDED"),
            ],
        )
        d = job.to_dict()
        job2 = Job.from_dict(d)
        assert job2.job_id == "job-001"
        assert job2.status == JobStatus.QUEUED
        assert "task_snapshot" in job2.to_dict()
        assert "command" not in job2.to_dict()
        assert len(job2.step_results) == 1

    def test_job_terminal_states(self):
        for status in [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.TIMEOUT,
                        JobStatus.CANCELED, JobStatus.LOST, JobStatus.SKIPPED]:
            job = Job(job_id="j1", status=status)
            assert job.is_terminal

    def test_job_non_terminal_states(self):
        for status in [JobStatus.QUEUED, JobStatus.DISPATCHED,
                        JobStatus.ACCEPTED, JobStatus.RUNNING]:
            job = Job(job_id="j1", status=status)
            assert not job.is_terminal

    def test_job_retryable(self):
        job = Job(job_id="j1", status=JobStatus.FAILED, attempt=1, max_attempts=3)
        assert job.is_retryable

    def test_job_not_retryable_max_attempts(self):
        job = Job(job_id="j1", status=JobStatus.FAILED, attempt=3, max_attempts=3)
        assert not job.is_retryable

    def test_command_roundtrip(self):
        cmd = Command(
            command_id="cmd-001",
            command_type=CommandType.ASSIGN_JOB,
            payload={"job_id": "job-001", "run_id": "run-001"},
            created_at="2026-06-08T10:00:00Z",
            expires_at="2026-06-08T10:05:00Z",
        )
        d = cmd.to_dict()
        cmd2 = Command.from_dict(d)
        assert cmd2.command_id == "cmd-001"
        assert cmd2.command_type == CommandType.ASSIGN_JOB
        assert cmd2.job_id == "job-001"
        assert cmd2.expires_at == "2026-06-08T10:05:00Z"

    def test_artifact_roundtrip(self):
        art = Artifact(
            artifact_id="art-001", job_id="job-001",
            artifact_type=ArtifactType.PNG_SCREENSHOT,
            checksum_sha256="abc123", size_bytes=245760,
            status=ArtifactStatus.STORED,
        )
        d = art.to_dict()
        art2 = Artifact.from_dict(d)
        assert art2.artifact_id == "art-001"
        assert art2.status == ArtifactStatus.STORED

    def test_error_info_roundtrip(self):
        ei = ErrorInfo(
            code="BMC_TIMEOUT", message="timeout", retryable=True, category="BMC",
            details={"url": "https://10.0.0.1"},
        )
        d = ei.to_dict()
        ei2 = ErrorInfo.from_dict(d)
        assert ei2.code == "BMC_TIMEOUT"
        assert ei2.retryable is True
        assert ei2.details["url"] == "https://10.0.0.1"

    def test_resource_lock_roundtrip(self):
        rl = ResourceLock(
            lock_uri="bmc://10.0.0.1", lock_type=LockType.BMC,
            holder_job_id="job-001", holder_executor_id="exec-01",
        )
        d = rl.to_dict()
        rl2 = ResourceLock.from_dict(d)
        assert rl2.lock_uri == "bmc://10.0.0.1"
        assert rl2.lock_type == LockType.BMC
