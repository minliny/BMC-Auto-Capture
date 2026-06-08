"""
Local resource lock manager — thread-safe, in-memory lock_uri management.

Design:
  - Same lock_uri is exclusive (only one owner at a time).
  - Different lock_uri can be held concurrently.
  - Reentrant: same owner re-acquiring refreshes TTL, returns True.
  - Expired locks are reclaimable after cleanup_expired().
  - Thread-safe via threading.Lock.
  - Never uses device_name as lock key.
"""

from .lock_manager import ResourceLockManager

__all__ = ["ResourceLockManager"]
