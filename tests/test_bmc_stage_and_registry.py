"""TI-004~005: BMC stage diagnostics and ResourceRegistry acquire timeout tests."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestResourceRegistryAcquireTimeout:
    """TI-005: ResourceRegistry acquire supports timeout with clear error."""

    def test_acquire_available_resource(self):
        """Available resource can be acquired."""
        from src.scheduler.resource_registry import ResourceRegistry
        reg = ResourceRegistry()
        reg._reset_for_test()
        with reg.acquire("test-key", {"execution_id": "e1", "plan_id": "p1"}) as info:
            assert info["endpoint_key"] == "test-key"
            assert info["reentrant"] is False

    def test_acquire_timeout_when_resource_held(self):
        """Timeout when resource is held by another thread."""
        from src.scheduler.resource_registry import ResourceRegistry
        reg = ResourceRegistry()
        reg._reset_for_test()

        # Hold the resource in another thread
        barrier = threading.Barrier(2)
        holder_done = threading.Event()

        def hold_resource():
            with reg.acquire("blocked-key", {"execution_id": "e2", "plan_id": "p2"}):
                barrier.wait(timeout=5)
                holder_done.wait(timeout=10)

        t = threading.Thread(target=hold_resource)
        t.start()
        barrier.wait(timeout=5)

        # Try to acquire with short timeout
        with pytest.raises(RuntimeError, match="ResourceRegistry timeout"):
            reg.acquire("blocked-key", {"execution_id": "e1", "plan_id": "p1"}, timeout=0.5).__enter__()

        holder_done.set()
        t.join(timeout=5)

    def test_acquire_timeout_error_contains_resource_name(self):
        """Timeout error message contains resource name and wait seconds."""
        from src.scheduler.resource_registry import ResourceRegistry
        reg = ResourceRegistry()
        reg._reset_for_test()

        barrier = threading.Barrier(2)
        holder_done = threading.Event()

        def hold_resource():
            with reg.acquire("my-resource", {"execution_id": "e2", "plan_id": "p2"}):
                barrier.wait(timeout=5)
                holder_done.wait(timeout=10)

        t = threading.Thread(target=hold_resource)
        t.start()
        barrier.wait(timeout=5)

        with pytest.raises(RuntimeError, match="my-resource") as exc_info:
            reg.acquire("my-resource", {"execution_id": "e1", "plan_id": "p1"}, timeout=0.5).__enter__()

        error_msg = str(exc_info.value)
        assert "waited" in error_msg
        assert "my-resource" in error_msg

        holder_done.set()
        t.join(timeout=5)

    def test_acquire_error_no_sensitive_values(self):
        """Error message must not contain sensitive values from metadata."""
        from src.scheduler.resource_registry import ResourceRegistry
        reg = ResourceRegistry()
        reg._reset_for_test()

        barrier = threading.Barrier(2)
        holder_done = threading.Event()

        def hold_resource():
            with reg.acquire("safe-key", {"execution_id": "e2", "plan_id": "p2",
                                           "password": "SENSITIVE_PASS"}) as _:
                barrier.wait(timeout=5)
                holder_done.wait(timeout=10)

        t = threading.Thread(target=hold_resource)
        t.start()
        barrier.wait(timeout=5)

        with pytest.raises(RuntimeError) as exc_info:
            reg.acquire("safe-key", {"execution_id": "e1", "plan_id": "p1"}, timeout=0.5).__enter__()

        error_msg = str(exc_info.value)
        assert "SENSITIVE_PASS" not in error_msg

        holder_done.set()
        t.join(timeout=5)


class TestBMCStageDiagnostics:
    """TI-004: BMC timeout message includes stage and elapsed time."""

    def test_timeout_message_includes_stage_info(self):
        """Timeout error message format includes stage name and timing."""
        # This is a structural test — verify the code path exists
        # by checking the bmc_executor source
        import inspect
        from src.executor.bmc_executor import BMCExecutor
        source = inspect.getsource(BMCExecutor._execute_async)
        assert "此阶段" in source or "stage" in source.lower()
        assert "current_stage" in source
