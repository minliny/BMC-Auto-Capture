"""
Tests for P1-1: ConfigStore concurrency lock / transaction safety.

Covers:
  - Concurrent activate_from_upload A/B 50x → latest.json always consistent
  - activate_from_upload + concurrent get_latest → no JSONDecodeError
  - activate_from_local_path concurrency
  - Legacy migration concurrency (idempotent)
  - Parse failure → latest unchanged
  - storedPath missing → get_latest returns None
  - hash mismatch detection
  - Concurrent A/B → final latest points to A or B, not mixed
"""
from __future__ import annotations
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from src.excel_config_store import ExcelConfigStore


def _hash_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_parse_result() -> dict:
    """Simulated _set_latest_excel return value.

    Bypasses real Excel parsing (which requires openpyxl and a real .xlsx).
    """
    return {
        "accepted": True,
        "sha256": "a" * 64,
        "path": "/fake/path.xlsx",
        "devices": [],
        "tasks": [],
        "deviceCount": 5,
        "enabledDeviceCount": 4,
        "taskCount": 10,
        "enabledTaskCount": 8,
    }


@pytest.fixture(autouse=True)
def mock_excel_parse(monkeypatch):
    """Mock _set_latest_excel to bypass real Excel parsing."""
    from src.plan_run_service import service as prs_module

    def fake_set_latest_excel(path):
        result = _fake_parse_result()
        result["path"] = path
        result["sha256"] = hashlib.sha256(open(path, "rb").read()).hexdigest()
        # Also update module-level excel_store
        from src.plan_run_service.service import _excel_store, _store_lock
        with _store_lock:
            _excel_store["latest"] = dict(result)
        return result

    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_set_latest_excel)


# ===========================================================================
# Helper: store with a temporary workspace
# ===========================================================================



def _patch_excel_parse(monkeypatch):
    """Monkeypatch _set_latest_excel to bypass real Excel parsing."""
    from src.plan_run_service import service as prs_module

    def fake_set_latest_excel(path):
        result = _fake_parse_result()
        result["path"] = path
        result["sha256"] = hashlib.sha256(open(path, "rb").read()).hexdigest()
        return result

    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_set_latest_excel)


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> ExcelConfigStore:
    """ExcelConfigStore scoped to a temp directory."""
    _patch_excel_parse(monkeypatch)
    store = ExcelConfigStore(workspace_root=str(tmp_path))
    return store


def _fake_xlsx_bytes(content: str = "test-workbook-content") -> bytes:
    """Return bytes > 100 that pass the min-size check."""
    return content.encode("utf-8") * 10


# ===========================================================================
# Basic transaction integrity
# ===========================================================================


def test_single_activate_creates_latest_json(tmp_store: ExcelConfigStore):
    """One activate_from_upload creates a valid latest.json."""
    raw = _fake_xlsx_bytes()
    result = tmp_store.activate_from_upload(raw, "test.xlsx")
    assert result["accepted"] is True

    meta = tmp_store.get_latest()
    assert meta is not None
    assert meta["excelHash"] == _hash_of(raw)
    assert meta["hasLatest"] is True
    assert os.path.isfile(meta["storedPath"])


def test_single_activate_local_path(tmp_store: ExcelConfigStore, monkeypatch):
    """One activate_from_local_path creates a valid latest.json."""
    # Bypass allowed-roots check for test isolation
    monkeypatch.setattr("src.excel_config_store._is_path_allowed",
                        lambda p: (True, ""))
    ws = tmp_store.workspace
    test_file = ws / "test_config.xlsx"
    test_file.write_bytes(_fake_xlsx_bytes())

    result = tmp_store.activate_from_local_path(str(test_file))
    assert result["accepted"] is True

    meta = tmp_store.get_latest()
    assert meta is not None
    assert os.path.isfile(meta["storedPath"])


# ===========================================================================
# Concurrent activate (A/B) — 50 rounds
# ===========================================================================


def test_concurrent_activate_50_rounds(tmp_path: Path, monkeypatch):
    """50 concurrent activate_from_upload A/B → latest.json always valid.

    _patch_excel_parse(monkeypatch)
    Verifies:
      - No JSONDecodeError from concurrent readers
      - latest.json always points to A or B, never mixed
      - storedPath always exists
    """
    from src.plan_run_service import service as prs_module
    def fake_parse(path):
        import hashlib
        return {"accepted": True, "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "path": path, "deviceCount": 5, "enabledDeviceCount": 4,
                "taskCount": 10, "enabledTaskCount": 8}
    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_parse)
    store = ExcelConfigStore(workspace_root=str(tmp_path))

    raw_a = _fake_xlsx_bytes("content-A-AAAA")
    raw_b = _fake_xlsx_bytes("content-B-BBBB")
    hash_a = _hash_of(raw_a)
    hash_b = _hash_of(raw_b)

    errors: list[str] = []
    results_lock = threading.Lock()

    def activate(which: str, raw: bytes):
        for _ in range(25):
            r = store.activate_from_upload(raw, f"{which}.xlsx")
            if not r.get("accepted"):
                with results_lock:
                    errors.append(f"{which} not accepted: {r}")
            time.sleep(0.001)

    t1 = threading.Thread(target=activate, args=("A", raw_a), daemon=True)
    t2 = threading.Thread(target=activate, args=("B", raw_b), daemon=True)
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)

    assert not errors, f"Activation errors: {errors}"

    # After all activations, latest.json must be valid
    meta = store.get_latest()
    assert meta is not None, "get_latest returned None after activations"

    # Must point to A or B, nothing else
    assert meta["excelHash"] in (hash_a, hash_b), \
        f"latest hash {meta['excelHash'][:12]}... not A or B"
    assert os.path.isfile(meta["storedPath"]), \
        f"storedPath {meta['storedPath']} does not exist"

    # Verify stored file contents match the hash
    with open(meta["storedPath"], "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()
    assert actual_hash == meta["excelHash"], \
        f"Hash mismatch: stored file hash {actual_hash[:12]} != metadata {meta['excelHash'][:12]}"


def test_concurrent_activate_and_read(tmp_path: Path, monkeypatch):
    """activate_from_upload + concurrent get_latest → no JSONDecodeError."""
    _patch_excel_parse(monkeypatch)
    from src.plan_run_service import service as prs_module
    def fake_parse(path):
        import hashlib
        return {"accepted": True, "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "path": path, "deviceCount": 5, "enabledDeviceCount": 4,
                "taskCount": 10, "enabledTaskCount": 8}
    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_parse)
    store = ExcelConfigStore(workspace_root=str(tmp_path))
    raw = _fake_xlsx_bytes("TEST-CONTENT-DATA")
    errors: list[str] = []
    results_lock = threading.Lock()

    def repeated_activate():
        for _ in range(20):
            r = store.activate_from_upload(raw, "test.xlsx")
            if not r.get("accepted"):
                with results_lock:
                    errors.append(f"activate failed: {r}")
            time.sleep(0.002)

    def repeated_read():
        for _ in range(100):
            try:
                meta = store.get_latest()
                if meta is not None:
                    _ = meta["excelHash"]
            except (json.JSONDecodeError, ValueError) as e:
                with results_lock:
                    errors.append(f"read error: {e}")
            time.sleep(0.001)

    threads = []
    for _ in range(3):
        t = threading.Thread(target=repeated_read, daemon=True)
        threads.append(t)
    t_act = threading.Thread(target=repeated_activate, daemon=True)
    threads.append(t_act)

    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert not errors, f"Errors during concurrent activate/read: {errors}"
    meta = store.get_latest()
    assert meta is not None
    assert os.path.isfile(meta["storedPath"])


# ===========================================================================
# Parse failure → latest unchanged
# ===========================================================================


def test_parse_failure_does_not_change_latest(monkeypatch, tmp_store: ExcelConfigStore):
    """When Excel parsing fails, latest.json must be unchanged."""
    _patch_excel_parse(monkeypatch)
    from src.plan_run_service import service as prs_module

    def failing_parse(path):
        raise ValueError("SIMULATED_PARSE_FAILURE")

    # First establish a valid latest
    raw_valid = _fake_xlsx_bytes("valid-content-data")
    r1 = tmp_store.activate_from_upload(raw_valid, "valid.xlsx")
    assert r1["accepted"] is True
    hash_before = _hash_of(raw_valid)

    # Now monkey-patch to fail on next parse
    monkeypatch.setattr(prs_module, "_set_latest_excel", failing_parse)

    # Upload again — should fail
    raw2 = _fake_xlsx_bytes("new-content-data-file")
    r2 = tmp_store.activate_from_upload(raw2, "fail.xlsx")
    assert r2["accepted"] is False

    # latest must still point to the original
    meta = tmp_store.get_latest()
    assert meta is not None
    assert meta["excelHash"] == hash_before


def test_parse_failure_invalid_extension(tmp_store: ExcelConfigStore):
    """Invalid file extension must not change latest."""
    r = tmp_store.activate_from_upload(b"x" * 200, "test.txt")
    assert r["accepted"] is False


# ===========================================================================
# storedPath missing → get_latest returns None
# ===========================================================================


def test_stored_path_missing_after_activation(tmp_path: Path, monkeypatch):
    """If storedPath is deleted after activation, get_latest returns None."""
    _patch_excel_parse(monkeypatch)
    from src.plan_run_service import service as prs_module
    def fake_parse(path):
        import hashlib
        return {"accepted": True, "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "path": path, "deviceCount": 5, "enabledDeviceCount": 4,
                "taskCount": 10, "enabledTaskCount": 8}
    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_parse)
    store = ExcelConfigStore(workspace_root=str(tmp_path))
    raw = _fake_xlsx_bytes()

    r = store.activate_from_upload(raw, "test.xlsx")
    assert r["accepted"] is True

    meta = store.get_latest()
    assert meta is not None

    # Delete the stored file
    os.remove(meta["storedPath"])

    # Now get_latest should return error dict (LATEST_EXCEL_MISSING)
    meta2 = store.get_latest()
    assert meta2 is not None, "get_latest should return error dict"
    assert meta2.get("code") == "LATEST_EXCEL_MISSING", \
        f"Expected LATEST_EXCEL_MISSING, got {meta2}"


# ===========================================================================
# Legacy migration concurrency
# ===========================================================================


def test_legacy_migration_concurrent(tmp_path: Path, monkeypatch):
    """Concurrent get_latest calls when latest.json absent → migration idempotent."""
    _patch_excel_parse(monkeypatch)
    monkeypatch.setattr("src.excel_config_store._is_path_allowed",
                        lambda p: (True, ""))
    # Create legacy file
    legacy_dir = tmp_path / ".runtime" / "configs"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_xlsx = legacy_dir / "latest.xlsx"
    raw = _fake_xlsx_bytes("legacy-content-data")
    legacy_xlsx.write_bytes(raw)

    store = ExcelConfigStore(workspace_root=str(tmp_path))
    errors: list[str] = []
    results_lock = threading.Lock()

    def concurrent_get():
        for _ in range(10):
            try:
                m = store.get_latest()
                if m is not None:
                    _ = m["excelHash"]
            except Exception as e:
                with results_lock:
                    errors.append(str(e))

    threads = [threading.Thread(target=concurrent_get, daemon=True) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors, f"Legacy migration errors: {errors}"
    meta = store.get_latest()
    assert meta is not None, "Legacy migration should have produced a latest"
    assert os.path.isfile(meta["storedPath"])


# ===========================================================================
# Concurrent A/B — final state check
# ===========================================================================


def test_concurrent_activate_a_b_final_not_mixed(tmp_path: Path, monkeypatch):
    """Concurrent A/B activation: final latest.json points cleanly to A or B.

    _patch_excel_parse(monkeypatch)
    No hash/path mixing: the excelHash in latest.json must match the actual
    file at storedPath.
    """
    from src.plan_run_service import service as prs_module
    def fake_parse(path):
        import hashlib
        return {"accepted": True, "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "path": path, "deviceCount": 5, "enabledDeviceCount": 4,
                "taskCount": 10, "enabledTaskCount": 8}
    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_parse)
    store = ExcelConfigStore(workspace_root=str(tmp_path))

    raw_a = _fake_xlsx_bytes("content-A-VERSION")
    raw_b = _fake_xlsx_bytes("content-B-VERSION")
    hash_a = _hash_of(raw_a)
    hash_b = _hash_of(raw_b)

    def activate_a():
        for _ in range(15):
            store.activate_from_upload(raw_a, "A.xlsx")
            time.sleep(0.002)

    def activate_b():
        for _ in range(15):
            store.activate_from_upload(raw_b, "B.xlsx")
            time.sleep(0.002)

    t_a = threading.Thread(target=activate_a, daemon=True)
    t_b = threading.Thread(target=activate_b, daemon=True)
    t_a.start()
    t_b.start()
    t_a.join(20)
    t_b.join(20)

    meta = store.get_latest()
    assert meta is not None

    # Final hash must be A or B
    assert meta["excelHash"] in (hash_a, hash_b), \
        f"Final hash {meta['excelHash'][:12]} is neither A nor B"

    # storedPath file content must match excelHash
    stored = meta["storedPath"]
    assert os.path.isfile(stored)
    with open(stored, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    assert actual == meta["excelHash"], \
        f"stored file hash {actual[:12]} != metadata {meta['excelHash'][:12]}"


# ===========================================================================
# Read latest.json directly (no memory cache) — must still be valid
# ===========================================================================


def test_latest_json_on_disk_matches_memory(tmp_path: Path, monkeypatch):
    """After activation, latest.json on disk must be valid JSON."""
    _patch_excel_parse(monkeypatch)
    from src.plan_run_service import service as prs_module
    def fake_parse(path):
        import hashlib
        return {"accepted": True, "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                "path": path, "deviceCount": 5, "enabledDeviceCount": 4,
                "taskCount": 10, "enabledTaskCount": 8}
    monkeypatch.setattr(prs_module, "_set_latest_excel", fake_parse)
    store = ExcelConfigStore(workspace_root=str(tmp_path))
    raw = _fake_xlsx_bytes("disk-check-content-data")
    store.activate_from_upload(raw, "test.xlsx")

    # Read latest.json directly from disk
    lj = store.latest_json_path
    assert lj.exists()
    raw_json = lj.read_text(encoding="utf-8")
    parsed = json.loads(raw_json)
    assert isinstance(parsed, dict)
    assert parsed.get("hasLatest") is True
    assert os.path.isfile(parsed["storedPath"])


def test_callback_outbox_multi_instance_concurrency(tmp_path: Path):
    """Instances targeting one file must not lose append/update operations."""
    from src.callback_outbox import CallbackOutbox, CallbackOutboxItem, SENT

    outbox_dir = tmp_path / "plans" / "plan-1"
    first = CallbackOutbox("plan-1", outbox_dir=str(outbox_dir))
    second = CallbackOutbox("plan-1", outbox_dir=str(outbox_dir))
    created: list[CallbackOutboxItem] = []
    created_lock = threading.Lock()

    def append_many(outbox: CallbackOutbox, prefix: str):
        local = []
        for index in range(50):
            item = CallbackOutboxItem(
                plan_id="plan-1",
                device_name=f"{prefix}-{index}",
                task_name="task",
                status="SUCCESS",
            )
            outbox.append(item)
            local.append(item)
        with created_lock:
            created.extend(local)

    threads = [
        threading.Thread(target=append_many, args=(first, "A")),
        threading.Thread(target=append_many, args=(second, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert len(first._read_all()) == 100

    update_threads = [
        threading.Thread(
            target=(first if index % 2 == 0 else second).mark_sent,
            args=(item.outbox_id,),
        )
        for index, item in enumerate(created)
    ]
    for thread in update_threads:
        thread.start()
    for thread in update_threads:
        thread.join(10)
        assert not thread.is_alive()

    final_items = first._read_all()
    assert len(final_items) == 100
    assert all(item.delivery_status == SENT for item in final_items)
