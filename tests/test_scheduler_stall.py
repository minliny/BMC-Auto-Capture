"""
Reproduce the 16-tasks-then-stall scenario with fake data.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler


def make_device(idx: int, group: str, has_bmc: bool = True, has_ssh: bool = True) -> Device:
    return Device(
        row_index=idx,
        device_name=f"DEV-{group}-{idx:02d}",
        device_group=group,
        bmc_ip=f"10.0.{idx}.1" if has_bmc else "",
        bmc_username="admin",
        bmc_password="pass",
        inband_ip=f"10.0.{idx}.2" if has_ssh else "",
        inband_username="user",
        inband_password="pass",
        enabled=True,
    )


def make_bmc_task(name: str, seq: int, group: str = "") -> Task:
    return Task(
        row_index=seq,
        sequence=seq,
        task_name=name,
        task_type="BMC",
        execution_mode="BMC_URL",
        match_group=group,
        command_or_url="/UI/Static/#/test",
        output_dir_template="{device_name}/{task_name}",
        image_name_template="{device_name}_{task_name}_{timestamp}",
        timeout_seconds=30,
        enabled=True,
    )


def make_ssh_task(name: str, seq: int, group: str = "") -> Task:
    return Task(
        row_index=seq,
        sequence=seq,
        task_name=name,
        task_type="SSH",
        execution_mode="SSH_CMD",
        match_group=group,
        command_or_url="show version",
        output_dir_template="{device_name}/{task_name}",
        image_name_template="{device_name}_{task_name}_{timestamp}",
        timeout_seconds=30,
        enabled=True,
    )


def test_28_devices_128_plans():
    """Simulate 28 devices × 4-5 tasks = 128 plans."""
    tasks = [
        make_bmc_task("RAID配置测试", 1),
        make_ssh_task("L1交换网络端口查询测试", 2),
        make_bmc_task("RM211管理iBMC IP查询", 3),
        make_bmc_task("BMC信息查询", 4),
        make_ssh_task("SSH系统状态", 5),
    ]
    enabled_tasks = [t for t in tasks if t.enabled]

    devices = []
    for i in range(28):
        group = f"GROUP-{i % 5}"
        devices.append(make_device(i, group, has_bmc=True, has_ssh=True))
    enabled_devices = [d for d in devices if d.enabled]

    # Generate plans
    from src.scheduler.plan_generator import generate_plans
    plans = generate_plans(enabled_devices, enabled_tasks)

    print(f"Devices: {len(enabled_devices)}, Tasks: {len(enabled_tasks)}, Plans: {len(plans)}")
    print(f"Unique devices in plans: {len({p.device.device_name for p in plans})}")

    # Monkey-patch executor to return fake success instantly
    original_execute = DynamicScheduler._execute_plan

    exec_count = [0]

    def fake_execute(self, plan):
        exec_count[0] += 1
        from src.models.execution_result import ExecutionResult
        return ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            task_name=plan.task.task_name,
            execution_status="EXEC_SUCCESS",
            started_at=time.time(),
            ended_at=time.time(),
        )

    DynamicScheduler._execute_plan = fake_execute

    try:
        config = AppConfig()
        config.base_bmc_workers = 2
        config.max_bmc_workers = 2
        config.base_ssh_workers = 4
        config.max_ssh_workers = 4
        config.output_root = "/tmp/bmc_test_output"

        scheduler = DynamicScheduler(config)

        t0 = time.time()
        results = scheduler.run(plans)
        elapsed = time.time() - t0

        print(f"\nResults: {len(results)} completed, {exec_count[0]} executed")
        print(f"Elapsed: {elapsed:.1f}s")

        # Verify
        total = len(plans)
        completed = len(results)
        if completed == total:
            print(f"PASS: All {total} plans completed")
        else:
            print(f"FAIL: Only {completed}/{total} plans completed (missing {total - completed})")

        # Check device queue cleanup
        remaining = sum(len(q) for q in scheduler._endpoint_queues.values())
        running = len(scheduler._bmc_pool._active_futures) + len(scheduler._ssh_pool._active_futures)
        locked = len(scheduler._bmc_pool._running_resources) + len(scheduler._ssh_pool._running_resources)
        print(f"Cleanup: remaining_in_queues={remaining}, running_futures={running}, locked_devices={locked}")

        if remaining == 0 and running == 0 and locked == 0:
            print("PASS: Clean shutdown")
        else:
            print(f"FAIL: Dirty shutdown (remaining={remaining}, running={running}, locked={locked})")

        if elapsed < 5:
            print("PASS: Fast completion (<5s)")
        else:
            print(f"WARN: Slow completion ({elapsed:.1f}s)")

    finally:
        DynamicScheduler._execute_plan = original_execute


if __name__ == "__main__":
    test_28_devices_128_plans()
