"""
Shared CLI argument parser — single source of truth for run.py and __main__.py.

Ensures python run.py --help and runtime\\bmc-engine.exe --help produce
identical output. No more duplicated argparse definitions.
"""

from __future__ import annotations

import argparse


_DESCRIPTION = "BMC Auto-Capture v0.2.4-RC5 — BMC/SSH 自动化测试证据采集平台"

_EPILOG = """
Examples:
  bmc-engine --excel tasks.xlsx
  bmc-engine --app-dir app --excel app/examples/_test_one_per_group.xlsx --no-preflight
  python run.py --app-dir app --excel my_tasks.xlsx --output ./results
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the canonical CLI argument parser.

    Both python run.py and the frozen bmc-engine.exe use this parser,
    guaranteeing --help output parity.
    """
    parser = argparse.ArgumentParser(
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    _add_arguments(parser)
    return parser


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add all arguments to a parser.  Pure helper, no global side-effect."""
    # --- Config ---
    parser.add_argument("--excel", "-e", default=None,
                        help="Path to Excel config (.xlsx)")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to YAML config (default: config/default_config.yaml)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output root directory (overrides YAML output_root)")
    parser.add_argument("--app-dir", default=None,
                        help="App directory containing src/, config/, tasks.json")

    # --- Execution mode ---
    parser.add_argument("--mode", "-m", choices=["sequential", "full"], default="sequential",
                        help="Execution mode: sequential (one-by-one) or full (dynamic scheduler)")

    # --- Worker pools ---
    parser.add_argument("--max-bmc-workers", type=int, default=None,
                        help="Max BMC concurrent workers (overrides YAML)")
    parser.add_argument("--max-ssh-workers", type=int, default=None,
                        help="Max SSH concurrent workers (overrides YAML)")

    # --- SSH timeouts ---
    parser.add_argument("--ssh-command-timeout", type=float, default=None,
                        help="SSH single-command timeout in seconds (overrides YAML)")
    parser.add_argument("--ssh-idle-timeout", type=float, default=None,
                        help="SSH idle read timeout in seconds (overrides YAML)")

    # --- BMC timeout ---
    parser.add_argument("--bmc-page-timeout", type=float, default=None,
                        help="BMC page load/selector timeout in seconds (overrides YAML)")

    # --- Preflight ---
    parser.add_argument("--preflight-only", action="store_true",
                        help="Connectivity preflight only, no task execution")
    parser.add_argument("--preflight-target", default="all",
                        choices=["all", "bmc", "ssh"],
                        help="Preflight target: all (default), bmc, ssh")
    parser.add_argument("--preflight-auth", default=None,
                        choices=["all", "bmc", "ssh"],
                        help="Preflight credential check: all, bmc, ssh")
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip connectivity preflight entirely")

    # --- API server ---
    parser.add_argument("--server", action="store_true",
                        help="Start as API server (minimal boot, no task execution)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="API server bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="API server bind port (default: 8080)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"],
                        help="API server log level (default: info)")
    parser.add_argument("--runner", default="fake", choices=["fake", "real"],
                        help="API server runner mode (default: fake)")
    parser.add_argument("--callback-transport", default="fake", choices=["fake", "http"],
                        help="API server direct-dispatch callback transport (default: fake)")
    parser.add_argument("--executor-id", default="exec-default",
                        help="API server executor id (default: exec-default)")
    parser.add_argument("--enable-real-runner", action="store_true",
                        help="Allow API requests to execute real BMC/SSH tasks")
    parser.add_argument("--enable-debug-callback-receiver", action="store_true",
                        help="Enable built-in debug callback receiver")
    parser.add_argument("--legacy-network-boot", action="store_true",
                        help="Start legacy Network Boot API instead of Executor API")

    # --- Verbose ---
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug-level logging")
