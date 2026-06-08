"""
ResourceLockManager — thread-safe in-memory lock for lock_uri strings.
"""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _LockEntry:
    lock_uri: str
    owner_id: str
    acquired_at: float
    ttl_seconds: float | None  # None = no expiry


class ResourceLockManager:
    """Thread-safe in-memory lock manager keyed by lock_uri.

    Same lock_uri: exclusive (one owner).
    Same owner re-acquiring: allowed (reentrant), refreshes TTL.
    Different lock_uri: concurrent.
    """

    def __init__(self) -> None:
        self._locks: dict[str, _LockEntry] = {}
        self._mtx = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        lock_uri: str,
        owner_id: str,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Acquire a lock on lock_uri for owner_id.

        Returns True if acquired (or re-acquired by same owner).
        Returns False if held by a different owner and not expired.
        Reentrant: same owner re-acquiring refreshes TTL and returns True.
        """
        if not lock_uri:
            raise ValueError("lock_uri must not be empty")
        if not owner_id:
            raise ValueError("owner_id must not be empty")

        now = time.time()
        with self._mtx:
            existing = self._locks.get(lock_uri)

            if existing is not None:
                # Same owner → reentrant, refresh TTL
                if existing.owner_id == owner_id:
                    existing.acquired_at = now
                    if ttl_seconds is not None:
                        existing.ttl_seconds = ttl_seconds
                    return True

                # Different owner — check expiry
                if self._is_expired_unlocked(existing, now):
                    # Expired — take over
                    self._locks[lock_uri] = _LockEntry(
                        lock_uri=lock_uri,
                        owner_id=owner_id,
                        acquired_at=now,
                        ttl_seconds=ttl_seconds,
                    )
                    return True

                return False

            # No existing lock — acquire
            self._locks[lock_uri] = _LockEntry(
                lock_uri=lock_uri,
                owner_id=owner_id,
                acquired_at=now,
                ttl_seconds=ttl_seconds,
            )
            return True

    def release(self, lock_uri: str, owner_id: str) -> bool:
        """Release a lock held by owner_id.

        Returns True if released.
        Returns False if not held by owner_id (or not locked).
        """
        if not lock_uri or not owner_id:
            return False

        with self._mtx:
            existing = self._locks.get(lock_uri)
            if existing is None:
                return False
            if existing.owner_id != owner_id:
                return False  # Not our lock
            del self._locks[lock_uri]
            return True

    def is_locked(self, lock_uri: str) -> bool:
        """Check if lock_uri is currently locked (ignoring expiry)."""
        with self._mtx:
            return lock_uri in self._locks

    def is_locked_active(self, lock_uri: str) -> bool:
        """Check if lock_uri is locked by a non-expired holder."""
        with self._mtx:
            existing = self._locks.get(lock_uri)
            if existing is None:
                return False
            return not self._is_expired_unlocked(existing, time.time())

    def get_owner(self, lock_uri: str) -> str | None:
        """Return the current owner_id for lock_uri, or None if not locked."""
        with self._mtx:
            existing = self._locks.get(lock_uri)
            if existing is None:
                return None
            return existing.owner_id

    def snapshot(self) -> dict[str, str]:
        """Return a copy of {lock_uri: owner_id} for all active locks."""
        with self._mtx:
            return {uri: entry.owner_id for uri, entry in self._locks.items()}

    def cleanup_expired(self) -> list[str]:
        """Remove all expired locks. Returns list of freed lock_uri strings."""
        now = time.time()
        freed: list[str] = []
        with self._mtx:
            expired = [
                uri
                for uri, entry in self._locks.items()
                if self._is_expired_unlocked(entry, now)
            ]
            for uri in expired:
                del self._locks[uri]
                freed.append(uri)
        return freed

    def __len__(self) -> int:
        with self._mtx:
            return len(self._locks)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _is_expired_unlocked(entry: _LockEntry, now: float) -> bool:
        if entry.ttl_seconds is None:
            return False  # No TTL → never expires
        return (now - entry.acquired_at) >= entry.ttl_seconds
