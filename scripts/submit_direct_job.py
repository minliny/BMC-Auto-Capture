#!/usr/bin/env python3
"""
Submit a direct-dispatch job to a running Executor API server.

Usage:
  python3 scripts/submit_direct_job.py --type BMC_URL --external-task-id t1 --job-id j1 \
    --callback-url http://127.0.0.1:18080/api/tasks/t1/status \
    --oob-ip 10.0.0.1 --oob-username admin --oob-password-ref env:BMC_PASS \
    --url "https://10.0.0.1/"

  python3 scripts/submit_direct_job.py --type SSH_CMD --external-task-id t2 --job-id j2 \
    --callback-url http://127.0.0.1:18080/api/tasks/t2/status \
    --inband-ip 10.0.1.1 --inband-username root --inband-password-ref env:SSH_PASS \
    --ssh-cmd "show version"

Never prints passwords or tokens.
"""

from __future__ import annotations
import argparse
import json
import sys
import urllib.request
import urllib.error
import uuid


def build_payload(args: argparse.Namespace) -> dict:
    """Build the job dispatch JSON payload from CLI args."""
    task_type = args.type.upper()

    # Device snapshot
    device_snapshot: dict = {
        "device_id": args.device_id or f"dev-{uuid.uuid4().hex[:8]}",
        "device_name": args.device_name or "CLI-Device",
        "device_group": args.device_group or "A3",
        "oob_ip": args.oob_ip or "",
        "oob_username": args.oob_username or "",
        "oob_password_ref": args.oob_password_ref or "",
        "inband_ip": args.inband_ip or "",
        "inband_username": args.inband_username or "",
        "inband_password_ref": args.inband_password_ref or "",
    }

    # Task snapshot
    task_snapshot: dict = {
        "task_id": args.task_id or args.external_task_id,
        "task_name": args.task_name or f"CLI Task {args.external_task_id}",
        "task_type": task_type,
        "execution_mode": task_type if task_type in ("BMC_URL", "SSH_CMD") else "BMC_URL",
        "timeout_seconds": args.timeout,
        "retry_count": 0,
    }

    if task_type == "BMC_URL":
        task_snapshot["url"] = args.url or "https://{oob_ip}/"
    elif task_type == "SSH_CMD":
        task_snapshot["ssh_cmd"] = args.ssh_cmd or "echo ok"

    # Lock URI
    lock_uri = args.lock_uri or ""
    if not lock_uri:
        if task_type == "BMC_URL":
            lock_uri = f"bmc://{args.oob_ip}" if args.oob_ip else ""
        elif task_type == "SSH_CMD":
            inband = args.inband_ip
            ssh_type = args.ssh_type or ""
            if ssh_type == "SSH_VRP":
                lock_uri = f"ssh-vrp://{inband}" if inband else ""
            elif ssh_type == "SSH_LINUX":
                lock_uri = f"ssh-linux://{inband}" if inband else ""
            else:
                lock_uri = f"ssh://{inband}" if inband else ""

    return {
        "command_id": args.command_id or f"cmd-{uuid.uuid4().hex[:12]}",
        "command_type": "ASSIGN_JOB",
        "external_task_id": args.external_task_id,
        "callback": {
            "status_url": args.callback_url,
            "artifact_url": "",
            "auth_token": args.auth_token or "",
        },
        "job": {
            "job_id": args.job_id,
            "run_id": args.run_id or f"run-{uuid.uuid4().hex[:8]}",
            "attempt": 1,
            "resource_lock": {
                "lock_uri": lock_uri,
                "lock_exclusive": True,
            },
            "device_snapshot": device_snapshot,
            "task_snapshot": task_snapshot,
        },
    }


def _redact_password(data: dict) -> dict:
    """Deep copy with password_ref values redacted for display."""
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _redact_password(v)
        elif "password" in k.lower() and isinstance(v, str):
            out[k] = "***" if v else ""
        else:
            out[k] = v
    return out


def main():
    parser = argparse.ArgumentParser(description="Submit a direct-dispatch job to Executor API")

    # Required
    parser.add_argument("--type", required=True, choices=("BMC_URL", "SSH_CMD"),
                        help="Task type")
    parser.add_argument("--external-task-id", required=True,
                        help="Server-side task ID (returned in callback)")
    parser.add_argument("--job-id", required=True,
                        help="Local job ID")
    parser.add_argument("--callback-url", required=True,
                        help="Server callback URL for status updates")

    # Executor
    parser.add_argument("--executor-url", default="http://127.0.0.1:8080",
                        help="Executor API base URL (default: http://127.0.0.1:8080)")

    # Device
    parser.add_argument("--device-id", default="", help="Device ID")
    parser.add_argument("--device-name", default="", help="Device name")
    parser.add_argument("--device-group", default="A3", help="Device group")
    parser.add_argument("--oob-ip", default="", help="OOB/BMC IP")
    parser.add_argument("--oob-username", default="", help="BMC username")
    parser.add_argument("--oob-password-ref", default="", help="BMC password ref (e.g. env:BMC_PASS)")
    parser.add_argument("--inband-ip", default="", help="In-band/SSH IP")
    parser.add_argument("--inband-username", default="", help="SSH username")
    parser.add_argument("--inband-password-ref", default="", help="SSH password ref (e.g. env:SSH_PASS)")
    parser.add_argument("--ssh-type", default="", choices=("", "SSH", "SSH_VRP", "SSH_LINUX"),
                        help="SSH sub-type for lock_uri derivation")

    # Task
    parser.add_argument("--task-id", default="", help="Task definition ID")
    parser.add_argument("--task-name", default="", help="Task display name")
    parser.add_argument("--url", default="", help="BMC target URL")
    parser.add_argument("--ssh-cmd", default="", help="SSH command to execute")
    parser.add_argument("--timeout", type=int, default=60, help="Task timeout in seconds")

    # Misc
    parser.add_argument("--command-id", default="", help="Idempotency command_id (auto-generated)")
    parser.add_argument("--run-id", default="", help="Run ID")
    parser.add_argument("--lock-uri", default="", help="Explicit resource lock URI (auto-derived)")
    parser.add_argument("--auth-token", default="", help="Callback auth token")

    args = parser.parse_args()

    payload = build_payload(args)

    url = args.executor_url.rstrip("/") + "/executor/v1/jobs"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    # Display (password redacted)
    safe = json.dumps(_redact_password(payload), ensure_ascii=False, indent=2)
    print(f"POST {url}")
    print(safe)
    print()

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"Response [{resp.status}]:")
            print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error [{e.code}]: {body}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}")
        print(f"Is the executor API server running at {args.executor_url}?")
        sys.exit(1)


if __name__ == "__main__":
    main()
