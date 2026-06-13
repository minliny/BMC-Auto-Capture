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
            self._items.append({"received_at": time.time(), "type": "item", "payload": dict(payload)})

    def add_summary(self, payload: dict):
        with self._lock:
            self._items.append({"received_at": time.time(), "type": "summary", "payload": dict(payload)})

    def list_all(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def summary(self) -> dict:
        with self._lock:
            s, f = 0, 0
            for it in self._items:
                if it.get("type") != "item":
                    continue
                st = it["payload"].get("status", "")
                if st == "SUCCESS": s += 1
                elif st == "FAILED": f += 1
            item_total = sum(1 for it in self._items if it.get("type") == "item")
            summary_total = sum(1 for it in self._items if it.get("type") == "summary")
            return {"total": item_total, "SUCCESS": s, "FAILED": f, "summaryCallbacks": summary_total}


_store = Store()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl).decode("utf-8", errors="replace") if cl > 0 else "{}"
        payload = json.loads(body)
        count = 0
        if isinstance(payload.get("items"), list):
            for item in payload["items"]:
                _store.add(item)
                count += 1
                print(
                    f"[CALLBACK] planId={item.get('planId', '?')} "
                    f"group={item.get('deviceGroup', '?')} device={item.get('deviceName', '?')} "
                    f"task={item.get('taskName', '?')} status={item.get('status', '?')}"
                )
        elif "summary" in payload and not payload.get("taskName"):
            _store.add_summary({"planId": payload.get("planId"), "summary": payload.get("summary", {})})
            count = 1
            print(f"[SUMMARY] planId={payload.get('planId', '?')}")
        else:
            _store.add(payload)
            count = 1
            pid = payload.get("planId", "?")
            dg = payload.get("deviceGroup", "?")
            dn = payload.get("deviceName", "?")
            tn = payload.get("taskName", "?")
            st = payload.get("status", "?")
            print(f"[CALLBACK] planId={pid} group={dg} device={dn} task={tn} status={st}")
        self._respond(200, {
            "code": 0,
            "message": "success",
            "data": {"total": count, "success": count, "failed": 0, "errors": []},
        })

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
