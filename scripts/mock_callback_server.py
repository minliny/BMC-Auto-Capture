#!/usr/bin/env python3
"""
Mock callback server — simulates the user's server receiving callback.status_url POSTs.

Uses stdlib http.server. No external dependencies.

Usage:
  python3 scripts/mock_callback_server.py
  python3 scripts/mock_callback_server.py --host 127.0.0.1 --port 18080

Endpoints:
  POST /api/tasks/{external_task_id}/status  — receive callback
  GET  /callbacks                             — list all received callbacks
  GET  /health                                — health check
"""

from __future__ import annotations
import argparse
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


class CallbackStore:
    """Thread-safe store of received callbacks."""

    def __init__(self):
        self._callbacks: list[dict] = []
        self._lock = threading.Lock()

    def add(self, external_task_id: str, payload: dict):
        record = {
            "received_at": time.time(),
            "external_task_id": external_task_id,
            "payload": payload,
        }
        with self._lock:
            self._callbacks.append(record)

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._callbacks)

    def summary(self) -> dict:
        with self._lock:
            statuses = {}
            for cb in self._callbacks:
                s = cb["payload"].get("status", "UNKNOWN")
                statuses[s] = statuses.get(s, 0) + 1
            return {
                "total": len(self._callbacks),
                "by_status": statuses,
            }


# Global store shared across requests
_store = CallbackStore()


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the mock callback server."""

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len > 0 else "{}"

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        # Extract external_task_id from path: /api/tasks/{id}/status
        path_parts = parsed.path.strip("/").split("/")
        ext_id = ""
        if len(path_parts) >= 3 and path_parts[0] == "api" and path_parts[1] == "tasks":
            ext_id = path_parts[2]

        _store.add(ext_id, payload)

        status = payload.get("status", "?")
        duration = payload.get("duration_ms", "?")
        job_id = payload.get("job_id", "?")
        print(f"[CALLBACK] external_task_id={ext_id} job_id={job_id} status={status} duration_ms={duration}")

        self._respond(200, {"ok": True, "external_task_id": ext_id})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        if path == "callbacks":
            data = {
                "summary": _store.summary(),
                "callbacks": _store.list_all(),
            }
            self._respond(200, data)
        elif path == "health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Suppress default access log noise
        pass


def main():
    parser = argparse.ArgumentParser(description="Mock Callback Server for E2E testing")
    parser.add_argument("--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=18080, help="Listen port (default: 18080)")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), CallbackHandler)
    print(f"Mock Callback Server starting on http://{args.host}:{args.port}")
    print(f"  POST /api/tasks/{{external_task_id}}/status  — receive callback")
    print(f"  GET  /callbacks                              — list received callbacks")
    print(f"  GET  /health                                 — health check")
    print()
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
