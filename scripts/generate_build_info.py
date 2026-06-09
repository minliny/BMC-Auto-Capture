#!/usr/bin/env python3
"""
Generate runtime/build_info.json — artifact metadata.

Called during CI build (release.yml) or locally after PyInstaller build.
Output is placed at the given output directory as build_info.json.

Usage:
    python scripts/generate_build_info.py <output_dir> [--workflow <name>] [--run-id <id>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _git_branch() -> str:
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _git_tag() -> str:
    try:
        return subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "--match", "v*"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _file_sha256(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pyinstaller_version() -> str:
    try:
        import PyInstaller
        return PyInstaller.__version__
    except Exception:
        return ""


def generate_build_info(output_dir: str | Path, *,
                        workflow_name: str = "",
                        workflow_run_id: str = "",
                        entrypoint: str = "") -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Version from pyproject.toml
    version = "unknown"
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.exists():
        import re
        m = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"))
        if m:
            version = m.group(1)

    info = {
        "version": version,
        "git_commit": _git_commit(),
        "git_branch": _git_branch(),
        "git_tag": _git_tag(),
        "build_time": datetime.now(timezone.utc).isoformat(),
        "build_machine": platform.node(),
        "python_version": platform.python_version(),
        "pyinstaller_version": _pyinstaller_version(),
        "platform": sys.platform,
        "entrypoint": entrypoint or "",
        "workflow_name": workflow_name,
        "workflow_run_id": workflow_run_id,
    }

    # runtime/bmc-engine.exe sha256 if exists
    exe_candidates = [
        output_dir / "bmc-engine.exe",
        output_dir / "bmc-engine",
    ]
    for exe in exe_candidates:
        sha = _file_sha256(exe)
        if sha:
            info["artifact_sha256"] = sha
            info["artifact_name"] = exe.name
            break

    out_path = output_dir / "build_info.json"
    out_path.write_text(
        json.dumps(info, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[build_info] Written: {out_path}")
    for k, v in info.items():
        print(f"  {k}: {v}")
    return info


def main():
    parser = argparse.ArgumentParser(description="Generate build_info.json")
    parser.add_argument("output_dir", help="Output directory (usually runtime/)")
    parser.add_argument("--workflow", default="", help="CI workflow name")
    parser.add_argument("--run-id", default="", help="CI workflow run ID")
    parser.add_argument("--entrypoint", default="run.py", help="Build entrypoint")
    args = parser.parse_args()

    generate_build_info(
        args.output_dir,
        workflow_name=args.workflow,
        workflow_run_id=args.run_id,
        entrypoint=args.entrypoint,
    )


if __name__ == "__main__":
    main()