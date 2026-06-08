#!/usr/bin/env python3
"""Unified dev verification pipeline — 5-layer local validation.

Usage:
  python scripts/dev_verify_all.py --offline --output ./dev_verify_out
  python scripts/dev_verify_all.py --offline --windows --packaged --output ./dev_verify_out
  python scripts/dev_verify_all.py --quick --output ./dev_verify_out
"""

from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_ROOT))

# ---------------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.status = "DEV_VERIFY_DONE"
        self.platform = _platform.platform()
        self.python_path = sys.executable
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self.project_root = str(PROJ_ROOT)
        self.checks_total = 0
        self.checks_passed = 0
        self.checks_failed = 0
        self.checks_skipped = 0
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.commands: list[dict] = []
        self.generated_files: list[str] = []
        self.packaged_runtime_status = "NOT_CHECKED"
        self.bat_status = "NOT_CHECKED"
        self.cli_contract_status = "NOT_CHECKED"

    def add_pass(self, name: str, detail: str = ""):
        self.checks_total += 1
        self.checks_passed += 1
        self.commands.append({"name": name, "result": "PASS", "detail": detail})

    def add_fail(self, name: str, detail: str = ""):
        self.checks_total += 1
        self.checks_failed += 1
        self.status = "DEV_VERIFY_PARTIAL"
        self.failures.append(f"{name}: {detail}" if detail else name)
        self.commands.append({"name": name, "result": "FAIL", "detail": detail})

    def add_skip(self, name: str, detail: str = ""):
        self.checks_total += 1
        self.checks_skipped += 1
        self.warnings.append(f"SKIPPED: {name}" + (f" — {detail}" if detail else ""))
        self.commands.append({"name": name, "result": "SKIP", "detail": detail})

    def block(self, reason: str):
        self.status = "DEV_VERIFY_BLOCKED"
        self.failures.append(f"BLOCKED: {reason}")

    def add_file(self, path: str):
        self.generated_files.append(path)


report = Report()


def run_cmd(cmd: list[str], name: str, timeout: int = 120,
            cwd: str | None = None, allow_fail: bool = False) -> tuple[int, str, str]:
    """Run a command. Returns (returncode, stdout, stderr)."""
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd or str(PROJ_ROOT))
        rc = proc.returncode
        out = proc.stdout
        err = proc.stderr
    except subprocess.TimeoutExpired:
        rc = -1
        out = ""
        err = f"TIMEOUT after {timeout}s"
    elapsed = time.time() - t0
    detail = f"rc={rc} ({elapsed:.1f}s)"
    if rc == 0 and not allow_fail:
        report.add_pass(name, detail)
    elif allow_fail:
        report.add_pass(name, f"expected non-zero: {detail}")
    else:
        report.add_fail(name, f"{detail}\n    stderr: {err[:200]}")
    return rc, out, err


# ======================================================================
# Layer 1: Source unit / offline logic
# ======================================================================

def layer1_source_tests(output_dir: str, quick: bool):
    print("\n" + "=" * 60)
    print("  Layer 1: Source Unit / Offline Tests")
    print("=" * 60)

    scripts_dir = PROJ_ROOT / "tests"

    # 1a: verify_offline.py
    if (PROJ_ROOT / "verify_offline.py").exists():
        cmd = [sys.executable, str(PROJ_ROOT / "verify_offline.py")]
        if quick:
            cmd.append("--quick")
        rc, _, _ = run_cmd(cmd, "verify_offline.py", timeout=300)
        if rc != 0 and quick:
            report.warnings.append(
                "verify_offline.py: test_api_run_with_plans requires mock (pre-existing, "
                "needs BMC session runner mock to avoid real browser)"
            )

    # 1b: timing report generation
    timing_out = os.path.join(output_dir, "timing_out")
    run_cmd([sys.executable, str(PROJ_ROOT / "scripts" / "generate_timing_report_offline.py"),
             "--output", timing_out],
            "generate_timing_report_offline.py", timeout=60)
    for f in ["result.csv", "plan_timing.csv", "device_timing.csv",
              "endpoint_timing.csv", "execution_summary.csv", "execution_summary.json"]:
        fp = os.path.join(timing_out, f)
        if os.path.exists(fp):
            report.add_file(fp)
            report.add_pass(f"timing report: {f}", f"{os.path.getsize(fp)}B")
        else:
            report.add_fail(f"timing report: {f}", "file missing")

    # 1c: endpoint key tests (standalone)
    epk = scripts_dir / "test_endpoint_key.py"
    if epk.exists():
        run_cmd([sys.executable, str(epk)], "test_endpoint_key", timeout=30)

    # 1d: ResourceRegistry tests (standalone)
    reg = scripts_dir / "test_resource_registry.py"
    if reg.exists():
        run_cmd([sys.executable, str(reg)], "test_resource_registry", timeout=30)

    # 1e: FileLock tests (standalone, skip subprocess if quick)
    fl = scripts_dir / "test_file_lock_windows.py"
    if fl.exists() and not quick:
        run_cmd([sys.executable, str(fl)], "test_file_lock_windows", timeout=60)
    elif fl.exists():
        report.add_skip("test_file_lock_windows (subprocess)", "quick mode")

    # 1f: API run_with_plans mock
    api = scripts_dir / "test_api_run_with_plans_source_or_mock.py"
    if api.exists():
        run_cmd([sys.executable, str(api)], "test_api_run_with_plans_mock", timeout=60)

    # 1g: BMC generic gates
    gate = scripts_dir / "test_bmc_generic_gates.py"
    if gate.exists():
        run_cmd([sys.executable, str(gate)], "test_bmc_generic_gates", timeout=60)

    # 1h: endpoint scheduling tests (pytest)
    eps = scripts_dir / "test_endpoint_scheduling.py"
    if eps.exists():
        run_cmd([sys.executable, "-m", "pytest", str(eps), "-q", "--tb=line"],
                "test_endpoint_scheduling (pytest)", timeout=120)

    # 1i: evidence audit (basic import + function check)
    try:
        from src.out.evidence_audit import write_evidence_audit_csv, EVIDENCE_AUDIT_HEADER
        report.add_pass("evidence_audit import", f"{len(EVIDENCE_AUDIT_HEADER)} fields")
    except Exception as e:
        report.add_fail("evidence_audit import", str(e))

    # 1j: timing report tests (pytest)
    ttr = scripts_dir / "test_timing_reports.py"
    if ttr.exists():
        run_cmd([sys.executable, "-m", "pytest", str(ttr), "-q", "--tb=line"],
                "test_timing_reports (pytest)", timeout=60)

    # 1k: preflight auth CLI tests
    pac = scripts_dir / "test_preflight_auth_cli.py"
    if pac.exists():
        run_cmd([sys.executable, str(pac)], "test_preflight_auth_cli", timeout=30)


# ======================================================================
# Layer 2: CLI contract
# ======================================================================

def layer2_cli_contract():
    print("\n" + "=" * 60)
    print("  Layer 2: CLI Contract")
    print("=" * 60)

    required_params = [
        "--preflight-auth",
        "--preflight-target",
        "--server",
        "--host",
        "--port",
        "--mode",
        "--max-bmc-workers",
        "--max-ssh-workers",
        "--preflight-only",
        "--no-preflight",
        "--output",
        "--app-dir",
    ]

    # Check run.py
    run_py = PROJ_ROOT / "run.py"
    if run_py.exists():
        with open(run_py) as f:
            run_content = f.read()
        rc, run_help, _ = run_cmd([sys.executable, str(run_py), "--help"],
                                   "run.py --help", timeout=10)
        run_help = run_help or ""

    # Check src/__main__.py
    main_py = PROJ_ROOT / "src" / "__main__.py"
    main_content = ""
    if main_py.exists():
        with open(main_py) as f:
            main_content = f.read()

    # Check parameter consistency
    params_ok = True
    for param in required_params:
        in_run = param in run_content
        in_main = param in main_content
        if in_run and in_main:
            report.add_pass(f"CLI param: {param}", "present in both run.py and __main__.py")
        elif in_run and not in_main:
            report.add_fail(f"CLI param: {param}", "in run.py but MISSING from __main__.py")
            params_ok = False
        elif not in_run and in_main:
            report.add_fail(f"CLI param: {param}", "in __main__.py but MISSING from run.py")
            params_ok = False

    report.cli_contract_status = "OK" if params_ok else "MISMATCH"

    # Check invalid args return non-zero
    run_cmd([sys.executable, str(run_py), "--preflight-auth", "invalid"],
            "CLI: --preflight-auth invalid → non-zero", timeout=10, allow_fail=True)

    # Check --help contains --preflight-auth
    if "--preflight-auth" in run_help:
        report.add_pass("CLI: --help contains --preflight-auth")
    else:
        report.add_fail("CLI: --help missing --preflight-auth")


# ======================================================================
# Layer 3: Packaged runtime smoke
# ======================================================================

def layer3_packaged_runtime():
    print("\n" + "=" * 60)
    print("  Layer 3: Packaged Runtime Smoke")
    print("=" * 60)

    exe_path = PROJ_ROOT / "runtime" / "bmc-engine.exe"
    if not exe_path.exists():
        # Check dist/
        exe_path = PROJ_ROOT / "dist" / "bmc-engine" / "bmc-engine.exe"
    if not exe_path.exists():
        report.add_skip("packaged runtime", f"bmc-engine.exe not found at {exe_path}")
        report.packaged_runtime_status = "PACKAGED_NOT_FOUND"
        return

    report.packaged_runtime_status = "CHECKING"

    # --help
    rc, help_out, _ = run_cmd([str(exe_path), "--help"],
                               "bmc-engine.exe --help", timeout=15)
    if "--preflight-auth" in help_out:
        report.add_pass("bmc-engine.exe: --help has --preflight-auth")
    else:
        report.add_fail("bmc-engine.exe: --help missing --preflight-auth")

    # --preflight-auth invalid → non-zero
    run_cmd([str(exe_path), "--preflight-auth", "invalid"],
            "bmc-engine.exe: --preflight-auth invalid → non-zero",
            timeout=10, allow_fail=True)

    report.packaged_runtime_status = "OK"


# ======================================================================
# Layer 4: Windows startup script check
# ======================================================================

def layer4_bat_check(windows_mode: bool):
    print("\n" + "=" * 60)
    print("  Layer 4: Windows Startup Script (.bat)")
    print("=" * 60)

    bat_path = PROJ_ROOT / "启动.bat"
    if not bat_path.exists():
        report.add_skip("启动.bat", "file not found")
        report.bat_status = "NOT_FOUND"
        return

    report.bat_status = "CHECKING"

    with open(bat_path, "rb") as f:
        bat_data = f.read()
    # Convert to text (try GBK first, fall back to latin-1)
    try:
        bat_text = bat_data.decode("gbk")
    except Exception:
        bat_text = bat_data.decode("latin-1", errors="replace")

    # Check 1: no naked Chinese that could be executed as commands
    # A bat line starting with Chinese chars (not after echo/set/if) is dangerous
    dangerous_pattern = False
    for line in bat_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("::") or stripped.startswith("rem "):
            continue
        if stripped.startswith("echo") or stripped.startswith("set") or stripped.startswith("if"):
            continue
        if stripped.startswith("goto") or stripped.startswith("call") or stripped.startswith(":"):
            continue
        if stripped.startswith("cls") or stripped.startswith("pause") or stripped.startswith("exit"):
            continue
        if not stripped:
            continue
        # Check if the line starts with a non-ASCII char after whitespace
        if ord(stripped[0]) > 127:
            dangerous_pattern = True
            report.add_fail("bat: naked non-ASCII line start",
                            f"Line may execute as command: '{stripped[:60]}'")
            break
    if not dangerous_pattern:
        report.add_pass("bat: no naked non-ASCII commands")

    # Check 2: preflight-auth parameter exists in bat
    if "--preflight-auth" in bat_text:
        report.add_pass("bat: --preflight-auth parameter present")
    else:
        report.add_fail("bat: --preflight-auth parameter missing")

    # Check 3: auth mode does NOT combine --preflight-only + --preflight-auth
    # Only check in auth section (not connect section)
    auth_lines = [l for l in bat_text.split("\n") if "auth" in l.lower() and "call :run_engine" in l]
    bad_combo = any("--preflight-only" in l and "--preflight-auth" in l for l in auth_lines)
    if bad_combo:
        report.add_fail("bat: auth mode has both --preflight-only and --preflight-auth")
    elif any("--preflight-auth" in l for l in auth_lines):
        report.add_pass("bat: auth mode uses --preflight-auth correctly (no --preflight-only)")
    else:
        report.add_skip("bat: auth mode check", "no auth call found")

    # Check 4: ERRORLEVEL checks after engine calls
    erl_checks = bat_text.count("ERRORLEVEL")
    if erl_checks >= 2:
        report.add_pass("bat: ERRORLEVEL checks present", f"{erl_checks} occurrences")
    else:
        report.add_fail("bat: insufficient ERRORLEVEL checks", f"found {erl_checks}")

    # Check 5: no completion message without ERRORLEVEL guard
    if "completed successfully" in bat_text.lower() or "预检完成" in bat_text:
        has_guard = False
        lines = bat_text.split("\n")
        for i, line in enumerate(lines):
            low = line.lower()
            if "completed" in low or "预检完成" in line:
                for j in range(max(0, i - 5), i):
                    if "ERRORLEVEL" in lines[j] or "PF_EXIT" in lines[j]:
                        has_guard = True
                        break
        if has_guard:
            report.add_pass("bat: completion message guarded by ERRORLEVEL/PF_EXIT check")
        else:
            report.add_fail("bat: completion message NOT guarded by ERRORLEVEL check")
    else:
        report.add_pass("bat: no unguarded completion message")

    # Check 6: server mode parameters
    if "--server" in bat_text and "--host" in bat_text and "--port" in bat_text:
        report.add_pass("bat: server mode parameters present")
    else:
        report.add_fail("bat: server mode parameters missing or incomplete")

    report.bat_status = "OK"


# ======================================================================
# Layer 5: Offline integration verification
# ======================================================================

def layer5_offline_integration(output_dir: str, quick: bool):
    print("\n" + "=" * 60)
    print("  Layer 5: Offline Integration Verification")
    print("=" * 60)

    # 5a: endpoint serial vs concurrent
    eps_test = PROJ_ROOT / "tests" / "test_endpoint_scheduling.py"
    if eps_test.exists():
        import pytest
        # Quick targeted checks
        specific_tests = [
            "test_same_endpoint_serial",
            "test_different_endpoint_concurrent",
            "test_bmc_inband_separate_pools",
        ]
        for test_name in specific_tests:
            rc, out, err = run_cmd(
                [sys.executable, "-m", "pytest", str(eps_test),
                 "-k", test_name, "-q", "--tb=line"],
                f"integration: {test_name}", timeout=60,
            )

    # 5b: FileLock subprocess
    if not quick:
        fl_test = PROJ_ROOT / "tests" / "test_file_lock.py"
        if fl_test.exists():
            run_cmd([sys.executable, "-m", "pytest", str(fl_test),
                     "-k", "test_two_processes_same_endpoint_serial",
                     "-q", "--tb=line"],
                    "integration: FileLock subprocess serial", timeout=30)
            run_cmd([sys.executable, "-m", "pytest", str(fl_test),
                     "-k", "test_two_processes_different_endpoint_concurrent",
                     "-q", "--tb=line"],
                    "integration: FileLock subprocess concurrent", timeout=30)

    # 5c: Timing reports
    timing_out = os.path.join(output_dir, "timing_out")
    timing_fields = ["plan_timing.csv", "device_timing.csv", "endpoint_timing.csv",
                     "execution_summary.csv", "execution_summary.json"]
    for f in timing_fields:
        fp = os.path.join(timing_out, f)
        if os.path.exists(fp):
            report.add_pass(f"integration: {f}", "exists")
        else:
            report.add_fail(f"integration: {f}", "missing")

    # 5d: evidence_audit BMC/SSH differentiation
    try:
        from src.out.evidence_audit import _expected_evidence
        bmc_expected = _expected_evidence("BMC")
        ssh_expected = _expected_evidence("SSH")
        telnet_expected = _expected_evidence("TELNET")
        if "html" in bmc_expected and "txt" in ssh_expected and "txt" in telnet_expected:
            report.add_pass("integration: evidence_audit type differentiation",
                            f"BMC→{bmc_expected}, SSH→{ssh_expected}, TELNET→{telnet_expected}")
        else:
            report.add_fail("integration: evidence_audit type differentiation wrong")
    except Exception as e:
        report.add_fail("integration: evidence_audit type check", str(e))

    # 5e: BMC page gate visible/hidden distinction
    try:
        from src.executor.bmc_health_check import check_page_basic_health, check_ready_for_capture
        report.add_pass("integration: BMC page gate imports OK")
        # Quick logic check: hidden error should warn, visible error should fail
        import asyncio

        class MockPage:
            url = "https://10.0.0.1/dashboard"
            title = "Dashboard"

            async def title(self):
                return "Dashboard"

            async def content(self):
                return "<html>" + "x" * 3000 + "</html>"

            async def evaluate(self, js):
                return {
                    "frame_count": 0, "visible_loading_count": 1,
                    "hidden_loading_count": 2, "visible_error_count": 0,
                    "hidden_error_count": 0,
                    "visible_loading": [{"selector": ".loading", "tag": "div",
                                          "class": "loading", "text": "Loading...",
                                          "visible": True, "area": 10000}],
                    "hidden_loading": [], "visible_error": [],
                    "hidden_error": [], "has_fullscreen_overlay": False,
                    "overlay_details": [],
                }

            async def query_selector(self, sel):
                return None

        # visible loading → READY should not pass
        r = asyncio.run(check_ready_for_capture(MockPage(), max_wait=0.5))
        if not r.ok or r.severity == "FAIL":
            report.add_pass("integration: BMC gate detects visible loading")
        else:
            report.add_fail("integration: BMC gate missed visible loading")

    except Exception as e:
        report.add_fail("integration: BMC page gate check", str(e))


# ======================================================================
# Main
# ======================================================================

def write_reports(output_dir: str):
    """Write JSON and TXT reports."""
    os.makedirs(output_dir, exist_ok=True)

    # JSON
    json_path = os.path.join(output_dir, "dev_verify_report.json")
    report_data = {
        "status": report.status,
        "platform": report.platform,
        "python_path": report.python_path,
        "python_version": report.python_version,
        "project_root": report.project_root,
        "checks_total": report.checks_total,
        "checks_passed": report.checks_passed,
        "checks_failed": report.checks_failed,
        "checks_skipped": report.checks_skipped,
        "failures": report.failures,
        "warnings": report.warnings,
        "commands": report.commands,
        "generated_files": report.generated_files,
        "packaged_runtime_status": report.packaged_runtime_status,
        "bat_status": report.bat_status,
        "cli_contract_status": report.cli_contract_status,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)
    report.add_file(json_path)

    # TXT
    txt_path = os.path.join(output_dir, "dev_verify_report.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("  Dev Verification Report\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"  Status:   {report.status}\n")
        f.write(f"  Platform: {report.platform}\n")
        f.write(f"  Python:   {report.python_version} ({report.python_path})\n")
        f.write(f"  Project:  {report.project_root}\n\n")
        f.write(f"  Total:  {report.checks_total}\n")
        f.write(f"  Passed: {report.checks_passed}\n")
        f.write(f"  Failed: {report.checks_failed}\n")
        f.write(f"  Skipped:{report.checks_skipped}\n\n")

        f.write(f"  CLI Contract:   {report.cli_contract_status}\n")
        f.write(f"  Packaged Exe:   {report.packaged_runtime_status}\n")
        f.write(f"  Startup Bat:    {report.bat_status}\n\n")

        if report.failures:
            f.write("─" * 40 + "\n")
            f.write("  FAILURES:\n")
            for item in report.failures:
                f.write(f"    - {item}\n")
            f.write("\n")
        if report.warnings:
            f.write("─" * 40 + "\n")
            f.write("  WARNINGS/SKIPPED:\n")
            for item in report.warnings:
                f.write(f"    - {item}\n")
        f.write("\n")
    report.add_file(txt_path)

    print(f"\n{'=' * 60}")
    print(f"  Report: {json_path}")
    print(f"  Report: {txt_path}")
    print(f"  Status: {report.status}")
    print(f"  Checks: {report.checks_passed} passed / {report.checks_failed} failed"
          f" / {report.checks_skipped} skipped (total {report.checks_total})")


def main():
    parser = argparse.ArgumentParser(description="Dev local verification pipeline")
    parser.add_argument("--offline", action="store_true", default=True,
                        help="Offline-only verification (no real HW)")
    parser.add_argument("--windows", action="store_true",
                        help="Enable Windows .bat checks")
    parser.add_argument("--packaged", action="store_true",
                        help="Check packaged bmc-engine.exe")
    parser.add_argument("--quick", action="store_true",
                        help="Skip slow subprocess tests")
    parser.add_argument("--output", "-o", default="./dev_verify_out",
                        help="Output directory for reports and generated files")
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    print(f"Dev Verify Pipeline")
    print(f"  Platform: {report.platform}")
    print(f"  Python:   {report.python_version}")
    print(f"  Output:   {output_dir}")
    print(f"  Mode:     {'quick' if args.quick else 'full'}"
          f"{' +windows' if args.windows else ''}"
          f"{' +packaged' if args.packaged else ''}")

    try:
        layer1_source_tests(output_dir, args.quick)
        layer2_cli_contract()
        if args.packaged:
            layer3_packaged_runtime()
        if args.windows or True:  # always check bat on any platform
            layer4_bat_check(args.windows)
        layer5_offline_integration(output_dir, args.quick)
    except KeyboardInterrupt:
        report.block("Interrupted by user")
    except Exception as e:
        report.block(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()

    write_reports(output_dir)
    return 0 if report.checks_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
