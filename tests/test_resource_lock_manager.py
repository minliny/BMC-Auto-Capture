"""
Unit tests for ResourceLockManager — thread safety, exclusivity, TTL, reentrancy.
"""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.resource_lock_manager import ResourceLockManager


class TestResourceLockBasics:
    """Basic lock operations."""

    def test_acquire_free_lock(self):
        mgr = ResourceLockManager()
        assert mgr.acquire("bmc://10.0.0.1", "job-001")

    def test_same_lock_uri_exclusive(self):
        mgr = ResourceLockManager()
        assert mgr.acquire("bmc://10.0.0.1", "job-001")
        assert not mgr.acquire("bmc://10.0.0.1", "job-002")

    def test_different_lock_uri_concurrent(self):
        mgr = ResourceLockManager()
        assert mgr.acquire("bmc://10.0.0.1", "job-001")
        assert mgr.acquire("bmc://10.0.0.2", "job-002")
        assert mgr.acquire("ssh://10.0.1.1", "job-003")

    def test_different_ssh_types_concurrent(self):
        mgr = ResourceLockManager()
        assert mgr.acquire("ssh://10.0.1.1", "job-001")
        assert mgr.acquire("ssh-vrp://10.0.1.1", "job-002")
        assert mgr.acquire("ssh-linux://10.0.1.1", "job-003")

    def test_release_owned_lock(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert mgr.release("bmc://10.0.0.1", "job-001")
        assert not mgr.is_locked("bmc://10.0.0.1")

    def test_release_non_owner_does_nothing(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert not mgr.release("bmc://10.0.0.1", "job-002")  # wrong owner
        assert mgr.is_locked("bmc://10.0.0.1")  # still locked
        assert mgr.get_owner("bmc://10.0.0.1") == "job-001"

    def test_release_unlocked_returns_false(self):
        mgr = ResourceLockManager()
        assert not mgr.release("bmc://10.0.0.1", "job-001")

    def test_is_locked(self):
        mgr = ResourceLockManager()
        assert not mgr.is_locked("bmc://10.0.0.1")
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert mgr.is_locked("bmc://10.0.0.1")

    def test_get_owner(self):
        mgr = ResourceLockManager()
        assert mgr.get_owner("bmc://10.0.0.1") is None
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert mgr.get_owner("bmc://10.0.0.1") == "job-001"

    def test_snapshot(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        mgr.acquire("ssh://10.0.1.1", "job-002")
        snap = mgr.snapshot()
        assert snap == {"bmc://10.0.0.1": "job-001", "ssh://10.0.1.1": "job-002"}

    def test_len(self):
        mgr = ResourceLockManager()
        assert len(mgr) == 0
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert len(mgr) == 1


class TestReentrancy:
    """Same owner re-acquiring behavior."""

    def test_same_owner_reacquire_returns_true(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert mgr.acquire("bmc://10.0.0.1", "job-001")  # reentrant

    def test_same_owner_reacquire_does_not_block_others(self):
        """Reentrant acquire by same owner should not change owner."""
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        mgr.acquire("bmc://10.0.0.1", "job-001")  # reentrant
        assert mgr.get_owner("bmc://10.0.0.1") == "job-001"
        # Other owner still blocked
        assert not mgr.acquire("bmc://10.0.0.1", "job-002")

    def test_same_owner_can_release(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        mgr.acquire("bmc://10.0.0.1", "job-001")  # reentrant refresh
        # One release should free it
        assert mgr.release("bmc://10.0.0.1", "job-001")
        assert not mgr.is_locked("bmc://10.0.0.1")

    def test_release_after_reentrant_allows_new_owner(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")
        mgr.acquire("bmc://10.0.0.1", "job-001")  # reentrant
        mgr.release("bmc://10.0.0.1", "job-001")
        assert mgr.acquire("bmc://10.0.0.1", "job-002")


class TestTTLExpiry:
    """TTL-based lock expiry and cleanup."""

    def test_ttl_not_expired(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001", ttl_seconds=10.0)
        assert mgr.is_locked_active("bmc://10.0.0.1")

    def test_ttl_expired_reclaim(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001", ttl_seconds=0.01)
        time.sleep(0.02)
        # Expired — new owner can acquire
        assert mgr.acquire("bmc://10.0.0.1", "job-002")

    def test_cleanup_expired_removes_locks(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001", ttl_seconds=0.01)
        mgr.acquire("ssh://10.0.1.1", "job-002", ttl_seconds=60.0)  # not expired
        time.sleep(0.02)
        freed = mgr.cleanup_expired()
        assert "bmc://10.0.0.1" in freed
        assert len(mgr) == 1  # only ssh:// remains

    def test_cleanup_expired_then_reacquire(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001", ttl_seconds=0.01)
        time.sleep(0.02)
        mgr.cleanup_expired()
        assert mgr.acquire("bmc://10.0.0.1", "job-002")

    def test_no_ttl_never_expires(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-001")  # no TTL
        freed = mgr.cleanup_expired()
        assert len(freed) == 0
        assert mgr.is_locked("bmc://10.0.0.1")


class TestThreadSafety:
    """Concurrent access from multiple threads."""

    def test_concurrent_acquire_different_locks(self):
        mgr = ResourceLockManager()
        results = []
        errors = []

        def acquire_lock(lock_uri, owner_id):
            try:
                ok = mgr.acquire(lock_uri, owner_id)
                results.append((lock_uri, owner_id, ok))
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(20):
            t = threading.Thread(
                target=acquire_lock,
                args=(f"bmc://10.0.0.{i}", f"job-{i:03d}"),
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(mgr) == 20
        assert all(ok for _, _, ok in results)

    def test_concurrent_same_lock_only_one_wins(self):
        mgr = ResourceLockManager()
        winners = []
        mtx = threading.Lock()

        def try_acquire(owner_id):
            ok = mgr.acquire("bmc://10.0.0.1", owner_id)
            with mtx:
                if ok:
                    winners.append(owner_id)

        threads = [threading.Thread(target=try_acquire, args=(f"job-{i:03d}",))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1
        assert mgr.is_locked("bmc://10.0.0.1")

    def test_concurrent_release_only_owner(self):
        mgr = ResourceLockManager()
        mgr.acquire("bmc://10.0.0.1", "job-owner")

        release_results = []

        def try_release(owner_id):
            ok = mgr.release("bmc://10.0.0.1", owner_id)
            release_results.append((owner_id, ok))

        threads = [threading.Thread(target=try_release, args=(f"job-{i:03d}",))
                   for i in range(10)]
        # Also add the actual owner
        threads.append(threading.Thread(target=try_release, args=("job-owner",)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only "job-owner" should succeed
        owner_releases = [ok for oid, ok in release_results if oid == "job-owner"]
        non_owner_releases = [ok for oid, ok in release_results if oid != "job-owner"]
        assert any(owner_releases)  # owner succeeded
        assert not any(non_owner_releases)  # no non-owner succeeded


class TestEdgeCases:
    """Edge cases and validation."""

    def test_acquire_empty_lock_uri_raises(self):
        mgr = ResourceLockManager()
        with pytest.raises(ValueError, match="lock_uri"):
            mgr.acquire("", "job-001")

    def test_acquire_empty_owner_raises(self):
        mgr = ResourceLockManager()
        with pytest.raises(ValueError, match="owner_id"):
            mgr.acquire("bmc://10.0.0.1", "")

    def test_release_empty_strings_returns_false(self):
        mgr = ResourceLockManager()
        assert not mgr.release("", "")
        assert not mgr.release("bmc://10.0.0.1", "")

    def test_no_device_name_as_lock_key(self):
        """Verify that device_name-like strings are NOT used as lock keys in tests."""
        mgr = ResourceLockManager()
        # We use proper lock_uri throughout
        mgr.acquire("bmc://10.0.0.1", "job-001")
        assert "Switch-A" not in str(mgr.snapshot())

    def test_multiple_bmc_locks_different_ips(self):
        mgr = ResourceLockManager()
        for i in range(100):
            assert mgr.acquire(f"bmc://10.0.0.{i}", f"job-{i:03d}")
        assert len(mgr) == 100

    def test_multiple_ssh_type_locks_same_ip(self):
        """Different SSH types on same IP should be independent locks."""
        mgr = ResourceLockManager()
        assert mgr.acquire("ssh://10.0.1.1", "job-001")
        assert mgr.acquire("ssh-vrp://10.0.1.1", "job-002")
        assert mgr.acquire("ssh-linux://10.0.1.1", "job-003")
        assert len(mgr) == 3
