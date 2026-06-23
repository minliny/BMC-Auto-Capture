from __future__ import annotations

import time
from typing import Any, Callable


TERMINAL_PLAN_STATUSES = {"COMPLETED", "FAILED"}


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
    message: str = "condition was not met",
) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise AssertionError(f"{message}; last_value={last_value!r}")


def wait_for_service_plan(svc: Any, plan_id: int | str, *, timeout: float = 5.0) -> dict[str, Any]:
    if hasattr(svc, "run_by_plan_id"):
        svc.run_by_plan_id(plan_id)

    def _done() -> dict[str, Any] | None:
        run = svc.get_plan(plan_id)
        if run and run.get("status") in TERMINAL_PLAN_STATUSES:
            return run
        return None

    return wait_until(_done, timeout=timeout, message=f"plan {plan_id} did not finish")


def wait_for_client_plan(
    client: Any,
    plan_id: int | str,
    *,
    excel_hash: str = "",
    timeout: float = 5.0,
) -> dict[str, Any]:
    query = f"?excelHash={excel_hash}" if excel_hash else ""

    def _done() -> dict[str, Any] | None:
        resp = client.get(f"/executor/v1/plans/{plan_id}{query}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") in TERMINAL_PLAN_STATUSES:
            return data
        return None

    return wait_until(_done, timeout=timeout, message=f"client plan {plan_id} did not finish")
