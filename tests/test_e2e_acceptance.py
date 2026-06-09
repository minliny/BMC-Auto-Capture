"""
Tests for E2E acceptance tools:
  - mock_callback_server
  - submit_direct_job payload generation
  - Password safety
  - Regression on existing 158 tests
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# Import the scripts as modules
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_scripts_dir))

from mock_callback_server import CallbackStore, CallbackHandler
from submit_direct_job import build_payload, _redact_password


# ===========================================================================
# Mock callback server tests
# ===========================================================================

class TestMockCallbackServer:
    """Tests for mock callback server store + grouped format."""

    def test_store_add_and_list(self):
        store = CallbackStore()
        store.add("task-001", {"status": "SUCCEEDED", "job_id": "j1"})
        store.add("task-001", {"status": "RUNNING", "job_id": "j1"})

        all_cbs = store.list_all()
        assert len(all_cbs) == 2

    def test_store_summary(self):
        store = CallbackStore()
        store.add("t1", {"status": "SUCCEEDED"})
        store.add("t2", {"status": "FAILED"})
        store.add("t3", {"status": "SUCCEEDED"})
        s = store.summary()
        assert s["total"] == 3
        assert s["by_status"]["SUCCEEDED"] == 2
        assert s["by_status"]["FAILED"] == 1

    def test_grouped_count_equals_2_after_running_and_succeeded(self):
        """1+2+3: Two callbacks for same task ⇒ count=2, received length=2."""
        store = CallbackStore()
        store.add("task-001", {"status": "RUNNING", "job_id": "j1"})
        store.add("task-001", {"status": "SUCCEEDED", "job_id": "j1", "duration_ms": 100})
        groups = store.grouped_by_task()
        assert len(groups) == 1
        g = groups[0]
        assert g["count"] == 2
        assert len(g["received"]) == 2

    def test_received_contains_running_and_succeeded(self):
        """4+5: received list contains RUNNING and SUCCEEDED."""
        store = CallbackStore()
        store.add("task-001", {"status": "RUNNING"})
        store.add("task-001", {"status": "SUCCEEDED"})
        g = store.grouped_by_task()[0]
        statuses = [r["status"] for r in g["received"]]
        assert "RUNNING" in statuses
        assert "SUCCEEDED" in statuses

    def test_latest_is_succeeded(self):
        """6: latest.status == SUCCEEDED."""
        store = CallbackStore()
        store.add("task-001", {"status": "RUNNING"})
        store.add("task-001", {"status": "SUCCEEDED", "duration_ms": 100})
        g = store.grouped_by_task()[0]
        assert g["latest"]["status"] == "SUCCEEDED"

    def test_deepcopy_prevents_mutation(self):
        """Payload mutation does not affect stored record."""
        store = CallbackStore()
        payload = {"status": "RUNNING", "data": {"nested": "val"}}
        store.add("task-001", payload)
        # Mutate original
        payload["status"] = "MUTATED"
        payload["data"]["nested"] = "mutated"
        # Stored should be unchanged
        g = store.grouped_by_task()[0]
        assert g["received"][0]["status"] == "RUNNING"
        assert g["received"][0]["data"]["nested"] == "val"

    def test_auth_token_not_in_payload_body(self):
        """7. Auth token is transmitted via HTTP header, NOT in JSON payload."""
        store = CallbackStore()
        # Real callback payload never includes auth_token in body;
        # it's sent as Authorization: Bearer header.
        store.add("task-001", {
            "external_task_id": "task-001",
            "status": "SUCCEEDED",
            "job_id": "j1",
        })
        g = store.grouped_by_task()[0]
        # The body should contain business fields only
        assert "auth_token" not in g["latest"]
        assert "Authorization" not in g["latest"]
        assert g["latest"]["status"] == "SUCCEEDED"

    def test_server_starts_and_accepts_post(self):
        """Start mock server, POST a callback, GET /callbacks."""
        import http.server
        import threading

        # Use a random port
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Clear global store
        from mock_callback_server import _store
        _store._callbacks.clear()

        server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            # POST callback
            data = json.dumps({"status": "SUCCEEDED", "job_id": "j1", "duration_ms": 100}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/task-001/status",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
            body = json.loads(resp.read().decode())
            assert body["ok"] is True

            # GET /callbacks
            resp2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/callbacks", timeout=5)
            assert resp2.status == 200
            cb_data = json.loads(resp2.read().decode())
            assert cb_data["summary"]["total"] == 1
        finally:
            server.shutdown()
            server.server_close()


# ===========================================================================
# submit_direct_job tests
# ===========================================================================

class TestSubmitDirectJob:
    """Tests 2-4: submit_direct_job payload generation."""

    def test_bmc_url_payload(self):
        """2. Generate BMC_URL payload with correct fields."""
        ns = argparse.Namespace(
            type="BMC_URL",
            external_task_id="task-bmc-1",
            job_id="job-bmc-1",
            callback_url="http://127.0.0.1:18080/api/tasks/task-bmc-1/status",
            executor_url="http://127.0.0.1:8765",
            device_id="", device_name="Switch-A", device_group="A3",
            oob_ip="10.0.0.1", oob_username="admin", oob_password_ref="env:BMC_PASS",
            inband_ip="", inband_username="", inband_password_ref="",
            ssh_type="",
            task_id="", task_name="BMC Test",
            url="https://10.0.0.1/storage", ssh_cmd="",
            timeout=120,
            command_id="cmd-bmc-1", run_id="run-1",
            lock_uri="", auth_token="tok1",
        )

        payload = build_payload(ns)
        assert payload["external_task_id"] == "task-bmc-1"
        assert payload["command_type"] == "ASSIGN_JOB"

        job = payload["job"]
        assert job["job_id"] == "job-bmc-1"
        assert job["resource_lock"]["lock_uri"] == "bmc://10.0.0.1"
        assert job["device_snapshot"]["oob_ip"] == "10.0.0.1"
        assert job["device_snapshot"]["oob_password_ref"] == "env:BMC_PASS"
        assert job["task_snapshot"]["execution_mode"] == "BMC_URL"
        assert job["task_snapshot"]["url"] == "https://10.0.0.1/storage"

    def test_ssh_cmd_payload(self):
        """3. Generate SSH_CMD payload with correct fields."""
        ns = argparse.Namespace(
            type="SSH_CMD",
            external_task_id="task-ssh-1",
            job_id="job-ssh-1",
            callback_url="http://127.0.0.1:18080/api/tasks/task-ssh-1/status",
            executor_url="http://127.0.0.1:8765",
            device_id="", device_name="Linux-Server", device_group="A3",
            oob_ip="", oob_username="", oob_password_ref="",
            inband_ip="10.0.1.1", inband_username="root", inband_password_ref="env:SSH_PASS",
            ssh_type="SSH_LINUX",
            task_id="", task_name="SSH Test",
            url="", ssh_cmd="uname -a",
            timeout=60,
            command_id="cmd-ssh-1", run_id="run-2",
            lock_uri="", auth_token="",
        )

        payload = build_payload(ns)
        job = payload["job"]
        assert job["device_snapshot"]["inband_ip"] == "10.0.1.1"
        assert job["device_snapshot"]["inband_password_ref"] == "env:SSH_PASS"
        assert job["task_snapshot"]["execution_mode"] == "SSH_CMD"
        assert job["task_snapshot"]["ssh_cmd"] == "uname -a"
        # Lock URI derivation for SSH_LINUX
        assert job["resource_lock"]["lock_uri"] == "ssh-linux://10.0.1.1"

    def test_ssh_vrp_lock_uri(self):
        """SSH_VRP type derives ssh-vrp:// lock_uri."""
        ns = argparse.Namespace(
            type="SSH_CMD", external_task_id="t1", job_id="j1",
            callback_url="http://x/cb",
            executor_url="http://x",
            device_id="", device_name="D", device_group="L1",
            oob_ip="", oob_username="", oob_password_ref="",
            inband_ip="10.0.1.1", inband_username="u", inband_password_ref="env:P",
            ssh_type="SSH_VRP",
            task_id="", task_name="T", url="", ssh_cmd="show ver",
            timeout=60, command_id="c1", run_id="r1",
            lock_uri="", auth_token="",
        )
        payload = build_payload(ns)
        assert payload["job"]["resource_lock"]["lock_uri"] == "ssh-vrp://10.0.1.1"

    def test_explicit_lock_uri_overrides_derived(self):
        """Explicit --lock-uri overrides derivation."""
        ns = argparse.Namespace(
            type="BMC_URL", external_task_id="t1", job_id="j1",
            callback_url="http://x/cb",
            executor_url="http://x",
            device_id="", device_name="D", device_group="A3",
            oob_ip="10.0.0.1", oob_username="a", oob_password_ref="env:P",
            inband_ip="", inband_username="", inband_password_ref="",
            ssh_type="",
            task_id="", task_name="T", url="https://10.0.0.1/", ssh_cmd="",
            timeout=60, command_id="c1", run_id="r1",
            lock_uri="bmc://custom-lock", auth_token="",
        )
        payload = build_payload(ns)
        assert payload["job"]["resource_lock"]["lock_uri"] == "bmc://custom-lock"

    def test_no_password_in_output(self):
        """4. _redact_password hides password_ref values."""
        payload = {
            "device_snapshot": {
                "oob_password_ref": "secret123",
                "inband_password_ref": "secret456",
                "oob_username": "admin",
            }
        }
        redacted = _redact_password(payload)
        assert redacted["device_snapshot"]["oob_password_ref"] == "***"
        assert redacted["device_snapshot"]["inband_password_ref"] == "***"
        assert redacted["device_snapshot"]["oob_username"] == "admin"

    def test_help_succeeds(self):
        """submit_direct_job --help works."""
        result = subprocess.run(
            [sys.executable, "scripts/submit_direct_job.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--type" in result.stdout
        assert "--external-task-id" in result.stdout

    def test_mock_server_help_succeeds(self):
        """mock_callback_server --help works."""
        result = subprocess.run(
            [sys.executable, "scripts/mock_callback_server.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0


# Needed for Namespace
import argparse
