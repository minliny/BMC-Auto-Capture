"""
CLI parity tests — ensure python run.py --help and frozen exe --help agree.

In development mode (no frozen exe), tests validate that the shared parser
(build_parser from src.cli.args) produces consistent help output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent

RUNTIME_SERVER_IMPORTS = [
    "fastapi.middleware.cors",
    "fastapi.responses",
    "fastapi.exceptions",
    "multipart",
]

RUNTIME_COLLECT_SUBMODULE_PACKAGES = [
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic_core",
    "anyio",
    "multipart",
    "python_multipart",
]


def _frozen_exe() -> Path | None:
    """Return path to frozen exe if it exists."""
    candidates = [
        PROJECT_ROOT / "runtime" / "bmc-engine.exe",
        PROJECT_ROOT / "runtime" / "bmc-engine",
        PROJECT_ROOT / "dist" / "bmc-engine" / "bmc-engine.exe",
        PROJECT_ROOT / "dist" / "bmc-engine" / "bmc-engine",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _source_help() -> str:
    """Capture python run.py --help output."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), "--help"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    return result.stdout + result.stderr


def _frozen_help(exe: Path) -> str:
    """Capture frozen exe --help output."""
    import os
    # Use subprocess with minimal env to avoid browser path noise
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = ""
    result = subprocess.run(
        [str(exe), "--help"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.stdout + result.stderr


# Required flags that MUST appear in both --help outputs
REQUIRED_FLAGS = [
    "--preflight-auth",
    "--preflight-only",
    "--preflight-target",
    "--no-preflight",
    "--server",
    "--mode",
    "--max-bmc-workers",
    "--max-ssh-workers",
    "--bmc-page-timeout",
    "--ssh-command-timeout",
    "--ssh-idle-timeout",
    "--host",
    "--port",
    "--verbose",
]


# ── Test: shared parser consistency ────────────────────────

def test_build_parser_has_preflight_auth():
    """Shared parser must contain --preflight-auth."""
    from src.cli.args import build_parser
    parser = build_parser()
    for action in parser._actions:
        if "--preflight-auth" in action.option_strings:
            assert "all" in action.choices
            assert "bmc" in action.choices
            assert "ssh" in action.choices
            return
    raise AssertionError("--preflight-auth not found in shared parser")


def test_build_parser_has_all_required_flags():
    """Shared parser must contain all required flags."""
    from src.cli.args import build_parser
    parser = build_parser()
    all_flags = set()
    for action in parser._actions:
        for opt in action.option_strings:
            all_flags.add(opt)
    for flag in REQUIRED_FLAGS:
        assert flag in all_flags, f"Flag {flag} not found in shared parser"


def test_build_parser_preflight_auth_choices():
    """--preflight-auth choices must be all, bmc, ssh only (no None)."""
    from src.cli.args import build_parser
    parser = build_parser()
    for action in parser._actions:
        if "--preflight-auth" in action.option_strings:
            choices = set(action.choices or [])
            assert "all" in choices, "Missing 'all' choice"
            assert "bmc" in choices, "Missing 'bmc' choice"
            assert "ssh" in choices, "Missing 'ssh' choice"
            assert None not in choices, "None should not be a choice"
            assert "None" not in choices, "'None' should not be a choice"
            return
    raise AssertionError("--preflight-auth not found")


def test_build_parser_accepts_legacy_concurrency():
    """--concurrency remains accepted for Windows launcher compatibility."""
    from src.cli.args import build_parser
    parser = build_parser()
    args = parser.parse_args(["--excel", "x.xlsx", "--concurrency", "3"])
    assert args.concurrency == 3


def test_build_parser_accepts_bmc_artifact_profile():
    from src.cli.args import build_parser

    parser = build_parser()
    args = parser.parse_args(["--excel", "x.xlsx", "--bmc-artifact-profile", "fast"])
    assert args.bmc_artifact_profile == "fast"


def test_legacy_concurrency_implies_full_and_both_worker_pools():
    from src.cli.args import build_parser, resolve_execution_cli

    parser = build_parser()
    argv = ["--excel", "x.xlsx", "--concurrency", "3"]
    args = parser.parse_args(argv)
    mode, max_bmc, max_ssh, legacy = resolve_execution_cli(args, argv)
    assert mode == "full"
    assert max_bmc == 3
    assert max_ssh == 3
    assert legacy == 3


def test_explicit_mode_overrides_legacy_concurrency_auto_full():
    from src.cli.args import build_parser, resolve_execution_cli

    parser = build_parser()
    argv = ["--excel", "x.xlsx", "--mode=sequential", "--concurrency", "3"]
    args = parser.parse_args(argv)
    mode, max_bmc, max_ssh, legacy = resolve_execution_cli(args, argv)
    assert mode == "sequential"
    assert max_bmc == 3
    assert max_ssh == 3
    assert legacy == 3


# ── Test: run.py --help parity ─────────────────────────────

def test_run_py_help_has_preflight_auth():
    """python run.py --help must contain --preflight-auth."""
    help_text = _source_help()
    assert "--preflight-auth" in help_text, (
        "python run.py --help missing --preflight-auth"
    )


def test_run_py_help_has_all_flags():
    """python run.py --help must contain all required flags."""
    help_text = _source_help()
    missing = []
    for flag in REQUIRED_FLAGS:
        if flag not in help_text:
            missing.append(flag)
    assert not missing, f"Missing flags in python run.py --help: {missing}"


def test_run_py_help_version_string():
    """python run.py --help must show v0.2.4."""
    help_text = _source_help()
    assert "v0.2.4" in help_text or "0.2.4" in help_text, (
        "Version string missing in python run.py --help"
    )


# ── Test: frozen exe --help parity (if exe exists) ─────────

def _frozen_has_preflight_auth():
    """Return True if frozen exe --help contains --preflight-auth."""
    exe = _frozen_exe()
    if not exe:
        return None  # Skip
    help_text = _frozen_help(exe)
    return "--preflight-auth" in help_text


def test_frozen_help_has_preflight_auth():
    """runtime/bmc-engine.exe --help must contain --preflight-auth (if exe exists)."""
    exe = _frozen_exe()
    if not exe:
        pytest.skip("No frozen exe found")
    help_text = _frozen_help(exe)
    assert "--preflight-auth" in help_text, (
        f"Frozen exe {exe} --help missing --preflight-auth"
    )


def test_frozen_help_has_all_flags():
    """Frozen exe --help must contain all required flags."""
    exe = _frozen_exe()
    if not exe:
        pytest.skip("No frozen exe found")
    help_text = _frozen_help(exe)
    missing = []
    for flag in REQUIRED_FLAGS:
        if flag not in help_text:
            missing.append(flag)
    assert not missing, f"Missing flags in frozen exe --help: {missing}"


def test_frozen_preflight_auth_invalid():
    """Frozen exe --preflight-auth invalid must return non-zero."""
    exe = _frozen_exe()
    if not exe:
        pytest.skip("No frozen exe found")
    result = subprocess.run(
        [str(exe), "--excel", str(PROJECT_ROOT / "examples" / "task_template.xlsx"),
         "--preflight-auth", "invalid"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    assert result.returncode != 0, (
        "--preflight-auth invalid should return non-zero"
    )
    # Error should be "invalid choice" not "unrecognized arguments"
    assert "invalid choice" in (result.stdout + result.stderr).lower(), (
        "Error should be 'invalid choice', not 'unrecognized arguments'"
    )


# ── Test: build_info.json (if exists) ──────────────────────

def test_build_info_json_exists():
    """A packaged runtime executable must have adjacent build metadata."""
    json_path = PROJECT_ROOT / "runtime" / "build_info.json"
    runtime_exe = next(
        (
            path
            for path in (
                PROJECT_ROOT / "runtime" / "bmc-engine.exe",
                PROJECT_ROOT / "runtime" / "bmc-engine",
            )
            if path.exists()
        ),
        None,
    )
    if runtime_exe is None:
        pytest.skip("No packaged runtime executable")
    assert json_path.exists(), "build_info.json missing in runtime/"


def test_build_info_json_valid():
    """build_info.json must contain version, git_commit, build_time."""
    json_path = PROJECT_ROOT / "runtime" / "build_info.json"
    if not json_path.exists():
        pytest.skip("No build_info.json")
    import json
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data.get("version"), "version missing"
    assert data.get("git_commit"), "git_commit missing"
    assert data.get("build_time"), "build_time missing"


def test_release_workflow_collects_runtime_server_imports_when_src_excluded():
    """Runtime/app split excludes src, so API dependency submodules must be bundled."""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "--exclude-module src" in workflow
    for module in RUNTIME_SERVER_IMPORTS:
        assert f"--hidden-import {module}" in workflow
    for package in RUNTIME_COLLECT_SUBMODULE_PACKAGES:
        assert f"--collect-submodules {package}" in workflow


def test_build_spec_collects_runtime_server_imports_when_src_excluded():
    """Local PyInstaller spec must match the release workflow runtime import policy."""
    spec = (PROJECT_ROOT / "scripts" / "build.spec").read_text(encoding="utf-8")
    assert '"src"' in spec
    for module in RUNTIME_SERVER_IMPORTS:
        assert f'"{module}"' in spec
    for package in RUNTIME_COLLECT_SUBMODULE_PACKAGES:
        assert f'"{package}"' in spec


# ── Test: stale artifact detection ─────────────────────────

def test_no_stale_artifact_without_preflight_auth():
    """If frozen exe exists, it MUST have --preflight-auth. Stale detection."""
    exe = _frozen_exe()
    if not exe:
        pytest.skip("No frozen exe found")
    result = _frozen_has_preflight_auth()
    if result is False:
        raise AssertionError(
            f"STALE ARTIFACT: {exe} is missing --preflight-auth. "
            "Rebuild required."
        )
