"""
ExcelConfigStore — managed storage for Excel config files with atomic latest.json.

Writes ONLY to:
  executor_state/configs/by_hash/{timestamp}_{sha256_12}.xlsx
  executor_state/configs/latest.json

Legacy paths (.runtime/configs/latest.xlsx, .runtime/configs/lastest.xlsx)
are read-only fallbacks that trigger automatic migration on first access.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("bmc_auto_capture.excel_config_store")

# ---------------------------------------------------------------------------
# Allowed roots for local Excel import (P0-4)
# ---------------------------------------------------------------------------

DEFAULT_EXCEL_IMPORT_ALLOWED_ROOTS: list[str] = []


def _init_allowed_roots() -> list[str]:
    """Initialize the list of allowed directory roots for Excel import.

    Returns a list of normalized absolute paths that are allowed.
    """
    roots: list[str] = []
    # Project workspace root (auto-detected)
    ws = _resolve_workspace()
    ws_str = str(ws.resolve())
    roots.append(ws_str)
    # Legacy paths under workspace
    roots.append(os.path.join(ws_str, ".runtime", "configs"))
    # Any configured extra roots
    return roots


_EXCEL_ALLOWED_ROOTS: list[str] | None = None


def _get_excel_allowed_roots() -> list[str]:
    global _EXCEL_ALLOWED_ROOTS
    if _EXCEL_ALLOWED_ROOTS is None:
        _EXCEL_ALLOWED_ROOTS = _init_allowed_roots()
    return _EXCEL_ALLOWED_ROOTS


def _is_path_allowed(path: str) -> tuple[bool, str]:
    """Check if a file path is within the allowed Excel import roots.

    Returns (allowed, reason).  If not allowed, reason describes the violation.
    """
    try:
        resolved = os.path.abspath(os.path.normpath(path))
    except Exception as e:
        return False, f"EXCEL_PATH_RESOLVE_ERROR: {e}"

    for root in _get_excel_allowed_roots():
        norm_root = os.path.abspath(os.path.normpath(root))
        if resolved.startswith(norm_root + os.sep) or resolved == norm_root:
            return True, ""

    return False, f"EXCEL_PATH_NOT_ALLOWED: {path} is not in any allowed import root"

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_WORKSPACE_CANDIDATES = [
    Path.cwd(),
    Path(__file__).resolve().parent.parent.parent,  # project root
]


def _resolve_workspace() -> Path:
    """Pick the first writable candidate as workspace root."""
    for base in _WORKSPACE_CANDIDATES:
        try:
            probe = base / ".workspace_probe"
            probe.touch()
            probe.unlink()
            return base
        except (OSError, PermissionError):
            continue
    # Last resort
    return Path.cwd()


# ---------------------------------------------------------------------------
# Config store
# ---------------------------------------------------------------------------


class ExcelConfigStore:
    """Manages Excel config storage with atomic latest.json.

    Thread-safe for concurrent upload + read via instance RLock.
    All activate/read/write paths hold the same lock.

    Transaction order for activation:
      1. validate input
      2. compute sha256
      3. parse Excel (fail-fast)
      4. write by_hash (atomic temp+rename)
      5. write latest.json (atomic temp+rename)
      6. update _memory_cache
    """

    # Subdirectories under workspace root
    STATE_DIR = "executor_state"
    CONFIGS_DIR = "executor_state/configs"
    BY_HASH_DIR = "executor_state/configs/by_hash"
    LATEST_JSON = "executor_state/configs/latest.json"

    # Legacy read-only paths (will be migrated on first access)
    LEGACY_LATEST_XLSX = ".runtime/configs/latest.xlsx"
    LEGACY_LASTEST_XLSX = ".runtime/configs/lastest.xlsx"

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def __init__(self, workspace_root: str | Path | None = None):
        self._workspace = Path(workspace_root) if workspace_root else _resolve_workspace()
        self._lock = threading.RLock()  # per-instance reentrant lock
        self._memory_cache: dict[str, Any] | None = None  # consistent in-memory latest
        self._ensure_dirs()

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def by_hash_dir(self) -> Path:
        return self._workspace / self.BY_HASH_DIR

    @property
    def latest_json_path(self) -> Path:
        return self._workspace / self.LATEST_JSON

    @property
    def legacy_latest_xlsx_path(self) -> Path:
        return self._workspace / self.LEGACY_LATEST_XLSX

    @property
    def legacy_lastest_xlsx_path(self) -> Path:
        return self._workspace / self.LEGACY_LASTEST_XLSX

    def _ensure_dirs(self):
        (self._workspace / self.BY_HASH_DIR).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Activate from upload (remote dispatch)
    # ------------------------------------------------------------------

    def activate_from_upload(self, raw_bytes: bytes, filename: str) -> dict[str, Any]:
        """Accept uploaded Excel bytes, validate, store, and mark as latest.

        Transactional: all-or-nothing under self._lock.
        Parse is attempted BEFORE writing by_hash to prevent orphan files.
        """
        # Validate extension (no lock needed — pure input check)
        if not filename.lower().endswith(".xlsx"):
            return {
                "accepted": False,
                "code": "INVALID_EXCEL_FILE",
                "message": "Only .xlsx files are accepted",
            }
        if len(raw_bytes) < 100:
            return {
                "accepted": False,
                "code": "EMPTY_EXCEL_FILE",
                "message": "Excel file is empty or too small",
            }

        sha256_full = hashlib.sha256(raw_bytes).hexdigest()
        sha12 = sha256_full[:12]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored_name = f"{ts}_{sha12}.xlsx"
        stored_path = self.by_hash_dir / stored_name

        with self._lock:
            stored_existed_before = stored_path.exists()
            # 1. Parse Excel FIRST — fail-fast before writing any files
            #    Write to a temp file for parsing, then use the same bytes for commit
            import tempfile
            fd, parse_tmp = tempfile.mkstemp(suffix=".xlsx", prefix=".parse.", dir=str(self.by_hash_dir))
            try:
                os.write(fd, raw_bytes)
                os.fsync(fd)
                os.close(fd)
                try:
                    from ..plan_run_service.service import _set_latest_excel
                    parse_result = _set_latest_excel(parse_tmp)
                except Exception as e:
                    return {
                        "accepted": False,
                        "code": "INVALID_EXCEL_CONFIG",
                        "message": f"Excel parsing failed: {e}",
                    }
            finally:
                # Always clean up parse temp file
                if os.path.exists(parse_tmp):
                    try:
                        os.unlink(parse_tmp)
                    except OSError:
                        pass

            # 2. Parse succeeded — now commit to by_hash
            tmp_path = ""
            try:
                self.by_hash_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    suffix=".xlsx", prefix=f".{stored_name}.", dir=str(self.by_hash_dir))
                try:
                    os.write(fd, raw_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp_path, str(stored_path))
            except OSError as e:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return {
                    "accepted": False,
                    "code": "FILE_WRITE_ERROR",
                    "message": str(e),
                }

            # 3. Build metadata and atomically write latest.json
            meta = {
                "version": 1,
                "hasLatest": True,
                "excelHash": sha256_full,
                "storedPath": str(stored_path),
                "originalFilename": filename,
                "source": "server_upload",
                "activatedAt": datetime.now(timezone.utc).isoformat(),
                "deviceCount": parse_result.get("deviceCount", 0),
                "enabledDeviceCount": parse_result.get("enabledDeviceCount", 0),
                "taskCount": parse_result.get("taskCount", 0),
                "enabledTaskCount": parse_result.get("enabledTaskCount", 0),
            }

            try:
                self._atomic_write_latest_json(meta)
            except OSError as e:
                rollback_error = self._rollback_new_by_hash(
                    stored_path, stored_existed_before)
                message = f"latest.json commit failed: {e}"
                if rollback_error:
                    message += f"; by_hash rollback failed: {rollback_error}"
                return {
                    "accepted": False,
                    "code": "LATEST_COMMIT_FAILED",
                    "message": message,
                }
            self._memory_cache = dict(meta)

        return {
            "accepted": True,
            "deviceCount": meta["deviceCount"],
            "enabledDeviceCount": meta["enabledDeviceCount"],
            "taskCount": meta["taskCount"],
            "enabledTaskCount": meta["enabledTaskCount"],
            "filename": filename,
            "excelHash": sha256_full,
            "sha256": sha256_full,
            "storedPath": str(stored_path),
            "message": "excel config uploaded and accepted as latest",
        }

    # ------------------------------------------------------------------
    # Activate from local path (debug / local import)
    # ------------------------------------------------------------------

    def activate_from_local_path(self, path: str) -> dict[str, Any]:
        """Activate an Excel file already on the executor filesystem.

        Transactional: all-or-nothing under self._lock.
        Parse is attempted BEFORE writing by_hash to prevent orphan files.
        The file is copied into by_hash storage; the original is NOT linked.
        Legacy paths (.runtime/configs/latest.xlsx etc.) trigger migration.

        P0-4: path must be within an allowed import root.
        """
        src = Path(path).resolve()
        if not src.exists():
            return {
                "accepted": False,
                "code": "EXCEL_PATH_NOT_FOUND",
                "message": "Excel file not found",
            }
        if not src.suffix.lower() == ".xlsx":
            return {
                "accepted": False,
                "code": "INVALID_EXCEL_FILE",
                "message": "Only .xlsx files are accepted",
            }

        # P0-4: check allowed-roots containment (no lock needed)
        allowed, reason = _is_path_allowed(str(src))
        if not allowed:
            logger.warning("Excel import rejected: %s", reason)
            return {
                "accepted": False,
                "code": "EXCEL_PATH_NOT_ALLOWED",
                "message": "Excel file path is not in an allowed import directory",
            }

        raw = src.read_bytes()
        if len(raw) < 100:
            return {
                "accepted": False,
                "code": "EMPTY_EXCEL_FILE",
                "message": "Excel file is empty or too small",
            }

        sha256_full = hashlib.sha256(raw).hexdigest()
        sha12 = sha256_full[:12]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Determine source before transaction
        is_legacy = (
            str(src).endswith("latest.xlsx")
            or str(src).endswith("lastest.xlsx")
            or ".runtime" in str(src)
        )
        original_filename = src.name

        with self._lock:
            # 1. Parse Excel FIRST — fail-fast before writing any files
            import tempfile
            fd, parse_tmp = tempfile.mkstemp(suffix=".xlsx", prefix=".parse.", dir=str(self.by_hash_dir))
            try:
                os.write(fd, raw)
                os.fsync(fd)
                os.close(fd)
                try:
                    from ..plan_run_service.service import _set_latest_excel
                    parse_result = _set_latest_excel(parse_tmp)
                except Exception as e:
                    return {
                        "accepted": False,
                        "code": "INVALID_EXCEL_CONFIG",
                        "message": f"Excel parsing failed: {e}",
                    }
            finally:
                if os.path.exists(parse_tmp):
                    try:
                        os.unlink(parse_tmp)
                    except OSError:
                        pass

            # 2. Parse succeeded — now commit to by_hash
            stored_name = f"{ts}_{sha12}.xlsx"
            stored_path = self.by_hash_dir / stored_name
            stored_existed_before = stored_path.exists()
            tmp_path = ""
            try:
                self.by_hash_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    suffix=".xlsx", prefix=f".{stored_name}.", dir=str(self.by_hash_dir))
                try:
                    os.write(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp_path, str(stored_path))
            except OSError as e:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return {
                    "accepted": False,
                    "code": "FILE_WRITE_ERROR",
                    "message": str(e),
                }

            # 3. Build metadata and atomically write latest.json
            meta = {
                "version": 1,
                "hasLatest": True,
                "excelHash": sha256_full,
                "storedPath": str(stored_path),
                "originalFilename": original_filename,
                "source": "legacy_migration" if is_legacy else "local_path",
                "activatedAt": datetime.now(timezone.utc).isoformat(),
                "deviceCount": parse_result.get("deviceCount", 0),
                "enabledDeviceCount": parse_result.get("enabledDeviceCount", 0),
                "taskCount": parse_result.get("taskCount", 0),
                "enabledTaskCount": parse_result.get("enabledTaskCount", 0),
            }

            try:
                self._atomic_write_latest_json(meta)
            except OSError as e:
                rollback_error = self._rollback_new_by_hash(
                    stored_path, stored_existed_before)
                message = f"latest.json commit failed: {e}"
                if rollback_error:
                    message += f"; by_hash rollback failed: {rollback_error}"
                return {
                    "accepted": False,
                    "code": "LATEST_COMMIT_FAILED",
                    "message": message,
                }
            self._memory_cache = dict(meta)

        return {
            "accepted": True,
            "deviceCount": parse_result.get("deviceCount", 0),
            "enabledDeviceCount": parse_result.get("enabledDeviceCount", 0),
            "taskCount": parse_result.get("taskCount", 0),
            "enabledTaskCount": parse_result.get("enabledTaskCount", 0),
            "filename": original_filename,
            "excelHash": sha256_full,
            "sha256": sha256_full,
            "storedPath": str(stored_path),
            "message": "excel config accepted as latest",
        }


    # ------------------------------------------------------------------
    # Read latest
    # ------------------------------------------------------------------

    def get_latest(self) -> dict[str, Any] | None:
        """Read latest config metadata.

        Returns:
          - dict with full metadata on success
          - dict with "code": "CONFIG_CORRUPTED" when latest.json is corrupted
          - dict with "code": "LATEST_EXCEL_MISSING" when storedPath is missing
          - dict with "code": "LATEST_EXCEL_HASH_MISMATCH" when storedPath hash doesn't match
          - None if no latest config exists (no latest.json, no legacy)

        Does NOT fallback to memory cache when latest.json is damaged — returns
        the specific error code so callers can act appropriately.
        Memory cache is only returned after validating disk integrity.
        """
        with self._lock:
            # Always validate disk state, even if memory cache exists
            if not self.latest_json_path.exists():
                # No latest.json at all — try legacy migration ONCE
                legacy = self._try_migrate_legacy()
                if legacy:
                    self._memory_cache = dict(legacy)
                    return dict(legacy)
                # No latest.json and no legacy — clear stale cache
                self._memory_cache = None
                return None

            # latest.json exists — read and validate it
            try:
                raw = self.latest_json_path.read_text(encoding="utf-8")
                meta = json.loads(raw)
                if not isinstance(meta, dict) or not meta.get("hasLatest"):
                    # Malformed latest.json — DO NOT fall back to cache or legacy
                    self._memory_cache = None
                    return {"code": "CONFIG_CORRUPTED",
                            "message": "latest.json malformed or hasLatest=false"}

                # Validate storedPath exists
                stored_path = meta.get("storedPath", "")
                if not stored_path:
                    self._memory_cache = None
                    return {"code": "CONFIG_CORRUPTED",
                            "message": "latest.json missing storedPath"}
                if not os.path.isfile(stored_path):
                    self._memory_cache = None
                    return {"code": "LATEST_EXCEL_MISSING",
                            "message": "Latest Excel config file is missing"}

                # Validate hash matches
                excel_hash = meta.get("excelHash", "")
                if excel_hash:
                    try:
                        actual_sha = hashlib.sha256()
                        with open(stored_path, "rb") as f:
                            actual_sha.update(f.read())
                        actual_hash = actual_sha.hexdigest()
                        if actual_hash != excel_hash:
                            self._memory_cache = None
                            return {"code": "LATEST_EXCEL_HASH_MISMATCH",
                                    "message": "Latest Excel config hash mismatch"}
                    except OSError:
                        # Can't read file for hash — treat as missing
                        self._memory_cache = None
                        return {"code": "LATEST_EXCEL_MISSING",
                                "message": "Cannot read latest Excel config for hash validation"}

            except (json.JSONDecodeError, OSError, ValueError):
                # Malformed latest.json — DO NOT fall back to cache or legacy
                self._memory_cache = None
                return {"code": "CONFIG_CORRUPTED",
                        "message": "latest config metadata is corrupted"}

            # All validations passed — update cache and return
            self._memory_cache = dict(meta)
            return dict(meta)

    def has_latest(self) -> bool:
        meta = self.get_latest()
        return bool(meta and not meta.get("code"))

    def reset_for_test(self) -> None:
        """Remove latest.json and clear cache. Test use only.

        Thread-safe: holds self._lock.
        """
        with self._lock:
            self._memory_cache = None
            try:
                if self.latest_json_path.exists():
                    self.latest_json_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rollback_new_by_hash(
        self, stored_path: Path, existed_before: bool,
    ) -> str:
        """Remove a by_hash file created by a failed latest.json transaction."""
        if existed_before or not stored_path.exists():
            return ""
        try:
            stored_path.unlink()
            return ""
        except OSError as exc:
            logger.error(
                "Config rollback failed for by_hash file %s: %s",
                stored_path.name,
                exc,
            )
            return str(exc)

    def _atomic_write_latest_json(self, meta: dict[str, Any]) -> None:
        """Write latest.json atomically via temp + rename.

        Must be called within self._lock.
        """
        configs_dir = self._workspace / self.CONFIGS_DIR
        configs_dir.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(meta, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix=".latest.", dir=str(configs_dir))
        try:
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, str(self.latest_json_path))
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        logger.info(
            "latest.json updated: hash=%s",
            meta.get("excelHash", "")[:12],
        )

    def _try_migrate_legacy(self) -> dict[str, Any] | None:
        """Try to read and migrate legacy .runtime/configs/latest.xlsx.

        Only called when latest.json does not exist at all.
        Side effect: writes by_hash copy + latest.json.
        Thread-safe: must be called within self._lock.
        """
        lock_held = self._lock._is_owned() if hasattr(self._lock, '_is_owned') else True
        if not lock_held:
            raise RuntimeError("_try_migrate_legacy must be called within self._lock")

        legacy_paths = [
            self.legacy_latest_xlsx_path,
            self.legacy_lastest_xlsx_path,
        ]

        for lp in legacy_paths:
            if lp.exists() and lp.is_file():
                logger.info(
                    "Legacy Excel found at %s — migrating to executor_state", lp)
                try:
                    result = self.activate_from_local_path(str(lp))
                    if result.get("accepted"):
                        logger.info(
                            "Legacy migration successful: %s",
                            lp.name,
                        )
                        return self._read_latest_json()
                except Exception as e:
                    logger.warning(
                        "Legacy migration failed for %s: %s", lp.name, e)

        return None

    def _read_latest_json(self) -> dict[str, Any] | None:
        """Internal: read latest.json without triggering migration.

        Validates storedPath exists.
        """
        if not self.latest_json_path.exists():
            return None
        try:
            raw = self.latest_json_path.read_text(encoding="utf-8")
            meta = json.loads(raw)
            if isinstance(meta, dict) and meta.get("hasLatest"):
                stored_path = meta.get("storedPath", "")
                if not stored_path or not os.path.isfile(stored_path):
                    return None
                return meta
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Module-level convenience (uses default workspace)
# ---------------------------------------------------------------------------

_default_store: ExcelConfigStore | None = None
_default_store_lock = threading.Lock()


def get_default_store() -> ExcelConfigStore:
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = ExcelConfigStore()
    return _default_store
