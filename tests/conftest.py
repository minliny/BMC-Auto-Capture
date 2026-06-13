from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_persistent_executor_state(tmp_path, monkeypatch):
    """Keep every test away from the repository's persistent executor state."""
    import src.excel_config_store as config_store_module
    from src.plan_run_service.service import _excel_store, _store_lock

    project_root = Path(__file__).resolve().parent.parent
    isolated_store = config_store_module.ExcelConfigStore(tmp_path / "executor_state")
    monkeypatch.setattr(config_store_module, "_default_store", isolated_store)
    monkeypatch.setattr(config_store_module, "_WORKSPACE_CANDIDATES", [tmp_path])
    monkeypatch.setattr(
        config_store_module,
        "_EXCEL_ALLOWED_ROOTS",
        [str(project_root), str(tmp_path)],
    )
    with _store_lock:
        _excel_store.clear()

    yield

    with _store_lock:
        _excel_store.clear()
