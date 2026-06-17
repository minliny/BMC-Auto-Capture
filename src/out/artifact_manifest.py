"""Artifact manifest helpers for local evidence replay."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from ..utils.path_safety import is_safe_path_component, safe_join_under_root


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: str, *, root_dir: str = "") -> dict[str, Any]:
    raw_path = str(path or "")
    meta: dict[str, Any] = {
        "path": os.path.abspath(raw_path) if raw_path else "",
        "filename": os.path.basename(raw_path) if raw_path else "",
        "exists": False,
    }

    if not raw_path:
        return meta

    abs_path = os.path.abspath(raw_path)
    if root_dir:
        try:
            meta["relative_path"] = os.path.relpath(abs_path, os.path.abspath(root_dir))
        except ValueError:
            pass

    if not os.path.isfile(abs_path):
        return meta

    meta.update({
        "exists": True,
        "size_bytes": os.path.getsize(abs_path),
        "sha256": sha256_file(abs_path),
    })
    return meta


def build_artifact_manifest(
    artifacts: dict[str, str],
    *,
    metadata: dict[str, Any] | None = None,
    root_dir: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "artifact_manifest.v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "artifacts": {
            name: file_metadata(path, root_dir=root_dir)
            for name, path in artifacts.items()
        },
    }


def write_artifact_manifest(
    output_dir: str,
    filename: str,
    *,
    artifacts: dict[str, str],
    metadata: dict[str, Any] | None = None,
    root_dir: str = "",
) -> str:
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe artifact manifest filename: {filename!r}")

    os.makedirs(output_dir, exist_ok=True)
    manifest_path = safe_join_under_root(output_dir, filename)
    payload = build_artifact_manifest(artifacts, metadata=metadata, root_dir=root_dir)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    return manifest_path


def merge_runtime_context(existing_json: str, updates: dict[str, Any]) -> str:
    context: dict[str, Any] = {}
    if existing_json:
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, dict):
                context.update(parsed)
            else:
                context["_previous_runtime_context"] = parsed
        except Exception:
            context["_previous_runtime_context"] = existing_json

    context.update({
        key: value
        for key, value in updates.items()
        if value not in ("", None)
    })
    return json.dumps(context, ensure_ascii=False)
