from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.out.artifact_manifest import (
    build_artifact_manifest,
    file_metadata,
    merge_runtime_context,
    write_artifact_manifest,
)


def test_file_metadata_records_size_hash_and_relative_path(tmp_path: Path):
    artifact = tmp_path / "evidence.txt"
    artifact.write_text("line-1\nline-2\n", encoding="utf-8")

    meta = file_metadata(str(artifact), root_dir=str(tmp_path))

    assert meta["exists"] is True
    assert meta["filename"] == "evidence.txt"
    assert meta["relative_path"] == "evidence.txt"
    assert meta["size_bytes"] == artifact.stat().st_size
    assert meta["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_write_artifact_manifest_writes_replayable_payload(tmp_path: Path):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"png")

    manifest_path = write_artifact_manifest(
        str(tmp_path),
        "screen.metadata.json",
        artifacts={"screenshot": str(screenshot), "missing": ""},
        metadata={"protocol": "BMC", "execution_status": "EXEC_SUCCESS"},
        root_dir=str(tmp_path),
    )

    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert payload["schema_version"] == "artifact_manifest.v1"
    assert payload["metadata"]["protocol"] == "BMC"
    assert payload["artifacts"]["screenshot"]["exists"] is True
    assert len(payload["artifacts"]["screenshot"]["sha256"]) == 64
    assert payload["artifacts"]["missing"]["exists"] is False


def test_artifact_manifest_rejects_unsafe_filename(tmp_path: Path):
    with pytest.raises(ValueError):
        write_artifact_manifest(str(tmp_path), "../escape.json", artifacts={}, root_dir=str(tmp_path))


def test_merge_runtime_context_preserves_existing_json_object():
    merged = merge_runtime_context('{"ssh_strategy":"interactive_shell"}', {"artifact_manifest_path": "/tmp/a.json"})

    payload = json.loads(merged)
    assert payload["ssh_strategy"] == "interactive_shell"
    assert payload["artifact_manifest_path"] == "/tmp/a.json"


def test_build_artifact_manifest_handles_missing_artifacts(tmp_path: Path):
    payload = build_artifact_manifest(
        {"state_json": str(tmp_path / "missing.state.json")},
        metadata={"protocol": "BMC"},
        root_dir=str(tmp_path),
    )

    assert payload["artifacts"]["state_json"]["exists"] is False
