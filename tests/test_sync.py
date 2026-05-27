"""Test dispatch logic WITHOUT threads - pure sequential."""
from __future__ import annotations
import sys, time
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.scheduler.worker_pool import WorkerPool
from src.scheduler.plan_generator import generate_plans

# Simulate EXACT dispatch/completion logic manually
devices = [
    Device(0, "D0", "G1", "10.0.0.1", "a", "p", "10.0.0.101", "u", "p", True, ()),
    Device(1, "D1", "G1", "10.0.1.1", "a", "p", "10.0.1.101", "u", "p", True, ()),
]
tasks = [
    Task(0, 0, "BMC_T0", "BMC", "BMC_URL", "", (), "/test", timeout_seconds=5, enabled=True),
    Task(1, 1, "SSH_T0", "SSH", "SSH_CMD", "", (), "show ver", timeout_seconds=5, enabled=True),
]
plans = generate_plans(devices, tasks)
print(f"Plans: {len(plans)}")

# Build device queues (same as DynamicScheduler._build_device_queues)
device_queues: dict[str, deque] = {}
for plan in plans:
    did = plan.device_id
    if did not in device_queues:
        device_queues[did] = deque()
    device_queues[did].append(plan)

ready_devices: deque[str] = deque()
for did in device_queues:
    ready_devices.append(did)

print(f"Devices: {list(device_queues.keys())}")
print(f"Ready: {list(ready_devices)}")
for did, q in device_queues.items():
    print(f"  {did}: {[p.task.task_name for p in q]}")

# Simulate dispatch loop
results = []
bmc_pool = WorkerPool("bmc", 1, 1)
ssh_pool = WorkerPool("ssh", 1, 1)
bmc_pool.start()
ssh_pool.start()

dispatched = 0
_completed = 0

def execute_plan(plan):
    global dispatched
    dispatched += 1
    print(f"  EXEC #{dispatched}: {plan.device.device_name} {plan.task.task_name} [{plan.protocol}]")
    return ExecutionResult(
        plan_id=plan.plan_id, device_name=plan.device.device_name,
        task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
        started_at=time.time(), ended_at=time.time(),
    )

def on_complete(result, plan, did):
    global _completed
    _completed += 1
    results.append(result)
    print(f"  DONE #{_completed}: {did} {plan.task.task_name}")
    q = device_queues.get(did)
    if q:
        print(f"    queue_remaining: {len(q)} tasks")
        ready_devices.append(did)

# Dispatch loop (simulates main loop)
iteration = 0
while any(q for q in device_queues.values()):
    iteration += 1
    print(f"\n--- Iteration {iteration} ---")
    print(f"  Ready devices: {list(ready_devices)}")

    made_progress = False
    for pool, protocol in [(bmc_pool, "BMC"), (ssh_pool, "SSH")]:
        while pool.has_idle() and ready_devices:
            did = ready_devices.popleft()
            q = device_queues.get(did)
            if not q:
                print(f"  {protocol}: {did} has empty queue, skip")
                continue

            plan = q[0]
            if plan.protocol != protocol:
                ready_devices.append(did)
                print(f"  {protocol}: {did} head is {plan.protocol}, skip")
                continue

            # DISPATCH (pop from queue before execution)
            plan = q.popleft()
            print(f"  START [{protocol}] {did} {plan.task.task_name}")

            # EXECUTE (simulate immediate completion to test logic)
            result = execute_plan(plan)

            # ON DONE
            on_complete(result, plan, did)
            made_progress = True

    if not made_progress:
        print("  NO PROGRESS - checking remaining queues:")
        for did, q in device_queues.items():
            if q:
                head = q[0]
                print(f"    {did}: {head.task.task_name} [{head.protocol}] - why not dispatched?")
        break

print(f"\nFinal: {len(results)}/{len(plans)} results")
remaining = sum(len(q) for q in device_queues.values())
print(f"Remaining in queues: {remaining}")
print("PASS" if len(results) == len(plans) and remaining == 0 else "FAIL")
