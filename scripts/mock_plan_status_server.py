#!/usr/bin/env python3
"""Mock plan item status server — receives per-device-per-task callbacks."""
from __future__ import annotations
import argparse, json, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


class Store:
    def __init__(self):
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def add(self, payload: dict):
        with self._lock:
            self._items.append({"received_at": time.time(), "payload": dict(payload)})

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def summary(self) -> dict:
        with self._lock:
            s, f = 0, 0
            for it in self._items:
                st = it["payload"].get("status", "")
                if st == "SUCCESS": s += 1
                elif st == "FAILED": f += 1
            return {"total": len(self._items), "SUCCESS": s, "FAILED": f}


_store = Store()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl).decode("utf-8", errors="replace") if cl > 0 else "{}"
        payload = json.loads(body)
        _store.add(payload)
        pid = payload.get("planId", "?")
        dn = payload.get("deviceName", "?")
        tn = payload.get("taskName", "?")
        st = payload.get("status", "?")
        print(f"[CALLBACK] planId={pid} device={dn} task={tn} status={st}")
        self._respond(200, {"ok": True})

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        if path == "plan-item-statuses":
            self._respond(200, {"summary": _store.summary(), "items": _store.list_all()})
        elif path == "health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args): pass


def main():
    p = argparse.ArgumentParser(description="Mock Plan Item Status Server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    args = p.parse_args()
    srv = HTTPServer((args.host, args.port), Handler)
    print(f"Mock Plan Status Server: http://{args.host}:{args.port}")
    print(f"  POST /api/plans/items/status")
    print(f"  GET  /plan-item-statuses")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()
