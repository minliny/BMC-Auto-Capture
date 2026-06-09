#!/usr/bin/env python3
"""Submit a plan run to executor API. Defaults to fake runner."""
from __future__ import annotations
import argparse, json, sys, urllib.request, urllib.error


def _post(url: str, data: dict) -> dict | None:
    body = json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  Error [{e.code}]: {e.read().decode()[:300]}")
        return None
    except urllib.error.URLError as e:
        print(f"  Connection error: {e.reason}")
        return None


def main():
    p = argparse.ArgumentParser(description="Submit plan run to executor")
    p.add_argument("--executor-url", default="http://127.0.0.1:8765")
    p.add_argument("--plan-id", type=int, required=True)
    p.add_argument("--item-status-url", default="http://127.0.0.1:18080/api/plans/items/status")
    p.add_argument("--updater", default="downstream-system")
    p.add_argument("--runner", choices=("fake", "real"), default="fake")
    args = p.parse_args()

    base = args.executor_url.rstrip("/")
    payload = {
        "callback": {"itemStatusUrl": args.item_status_url},
        "updater": args.updater,
        "runner": args.runner,
    }
    print(f"POST {base}/executor/v1/plans/{args.plan_id}:run")
    result = _post(f"{base}/executor/v1/plans/{args.plan_id}:run", payload)
    if result:
        print(f"  runId={result.get('runId')} status={result.get('status')} items={result.get('summary',{}).get('total','?')}")


if __name__ == "__main__":
    main()
