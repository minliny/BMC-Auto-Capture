"""
Cross-process file lock for endpoint_key serialization.

Supports Unix (fcntl.flock) and Windows (msvcrt.locking).
Provides a context manager that maps endpoint_key to safe filesystem paths.

Usage:
    lock = FileLock()
    with lock.acquire("BMC:10.0.0.1:443", timeout=30):
        # exclusive access to this endpoint across processes
        ...
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("bmc_auto_capture.file_lock")


def _safe_filename(endpoint_key: str) -> str:
    """Convert endpoint_key to a safe filesystem name."""
    # endpoint_key looks like "BMC:10.0.0.1:443" or "INBAND:192.168.1.1:22"
    # Replace colons with underscores, keep alphanumeric + dots
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', endpoint_key)
    # Avoid path traversal
    safe = safe.replace('..', '__')
    return safe


class FileLock:
    """Cross-process lock using OS-level file locking.

    On Unix: fcntl.flock (advisory, process-safe)
    On Windows: msvcrt.locking (mandatory)
    """

    def __init__(self, lock_dir: str | None = None):
        if lock_dir:
            self._lock_dir = Path(lock_dir)
        else:
            self._lock_dir = Path(tempfile.gettempdir()) / "bmc_auto_capture_locks"
        self._lock_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, endpoint_key: str, timeout: float | None = 60):
        """Return a context manager that holds the file lock.

        Blocks up to `timeout` seconds.  None = wait forever.
        """
        return _FileLockContext(self, endpoint_key, timeout)

    def try_acquire(self, endpoint_key: str) -> "_FileLockContext | None":
        """Non-blocking acquire. Returns None if lock is held."""
        ctx = _FileLockContext(self, endpoint_key, timeout=0.0)
        if ctx._acquired:
            return ctx
        ctx._close()
        return None

    def is_locked(self, endpoint_key: str) -> bool:
        """Check if another process holds the lock (best-effort)."""
        lock_path = self._lock_path(endpoint_key)
        if not lock_path.exists():
            return False
        # Try to acquire non-blocking and immediately release
        f = None
        try:
            f = open(lock_path, 'w')
            if _try_lock_file(f):
                _unlock_file(f)
                return False
            return True
        except Exception:
            return False
        finally:
            if f:
                try:
                    f.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _lock_path(self, endpoint_key: str) -> Path:
        return self._lock_dir / f"{_safe_filename(endpoint_key)}.lock"

    def _lock_info_path(self, endpoint_key: str) -> Path:
        return self._lock_dir / f"{_safe_filename(endpoint_key)}.info"


class _FileLockContext:
    """Context manager for a single file lock acquisition."""

    def __init__(self, parent: FileLock, endpoint_key: str, timeout: float | None):
        self._parent = parent
        self._endpoint_key = endpoint_key
        self._lock_path = parent._lock_path(endpoint_key)
        self._info_path = parent._lock_info_path(endpoint_key)
        self._file = None
        self._acquired = False
        self.wait_seconds: float = 0.0

        _t0 = time.time()
        self._acquired = self._do_acquire(timeout)
        self.wait_seconds = round(time.time() - _t0, 3)

    def __enter__(self):
        if not self._acquired:
            raise RuntimeError(
                f"FileLock: could not acquire {self._endpoint_key} "
                f"(path={self._lock_path})"
            )
        return self

    def __exit__(self, *exc):
        self._close()

    def _do_acquire(self, timeout: float | None) -> bool:
        """Attempt to acquire the file lock. Returns True on success."""
        deadline = None if timeout is None else time.time() + timeout

        while True:
            try:
                # Open (or create) the lock file
                self._file = open(self._lock_path, 'w')

                if _try_lock_file(self._file):
                    # Write metadata for diagnostics
                    self._write_info()
                    logger.debug(
                        "[FileLock] acquired %s (path=%s, wait=%.2fs)",
                        self._endpoint_key, self._lock_path, self.wait_seconds,
                    )
                    return True

                # Lock held — close and retry
                self._file.close()
                self._file = None

            except Exception as e:
                logger.warning(
                    "[FileLock] error acquiring %s: %s", self._endpoint_key, e,
                )
                if self._file:
                    try:
                        self._file.close()
                    except Exception:
                        pass
                    self._file = None

            if deadline is not None and time.time() >= deadline:
                logger.warning(
                    "[FileLock] timeout acquiring %s after %.2fs",
                    self._endpoint_key, self.wait_seconds,
                )
                return False

            time.sleep(0.1)

    def _close(self):
        if self._acquired and self._file:
            _unlock_file(self._file)
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._acquired = False
            # Clean up lock file
            try:
                if self._lock_path.exists():
                    self._lock_path.unlink()
            except Exception:
                pass
            try:
                if self._info_path.exists():
                    self._info_path.unlink()
            except Exception:
                pass
            logger.debug(
                "[FileLock] released %s (path=%s)",
                self._endpoint_key, self._lock_path,
            )

    def _write_info(self):
        """Write holder metadata for diagnostics."""
        try:
            info = f"pid={os.getpid()}\nendpoint={self._endpoint_key}\n"
            with open(self._info_path, 'w') as f:
                f.write(info)
        except Exception:
            pass


# ------------------------------------------------------------------
# Platform-specific lock primitives
# ------------------------------------------------------------------

def _try_lock_file(f) -> bool:
    """Try to acquire an exclusive non-blocking lock on the open file.

    Returns True if acquired, False if already held.
    """
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, IOError):
        return False
    except ImportError:
        # Windows — try msvcrt
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, IOError):
            return False
        except ImportError:
            # Fallback: no OS-level lock available
            logger.warning("No file lock available (not Unix, not Windows)")
            return True  # Best-effort: pretend we got it


def _unlock_file(f) -> None:
    """Release the lock on the file."""
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except ImportError:
        try:
            import msvcrt
            # Seek to beginning before unlocking
            try:
                f.seek(0)
            except Exception:
                pass
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            pass
    except Exception:
        pass
