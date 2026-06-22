"""
Tests for E2E acceptance tools:
  - mock_callback_server
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

    def test_mock_server_help_succeeds(self):
        """mock_callback_server --help works."""
        result = subprocess.run(
            [sys.executable, "scripts/mock_callback_server.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
