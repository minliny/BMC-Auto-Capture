"""
Shared CLI argument parser — single source of truth for run.py and __main__.py.

Ensures python run.py --help and runtime\\bmc-engine.exe --help produce
identical output. No more duplicated argparse definitions.
"""

from __future__ import annotations

import argparse
import sys

from src._version import APP_DESCRIPTION


_DESCRIPTION = APP_DESCRIPTION

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
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Deprecated compatibility flag. Values >1 imply --mode full and map to both worker pools when explicit max worker flags are not set")

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
    parser.add_argument("--bmc-artifact-profile", choices=["full", "fast"], default=None,
                        help="BMC artifact profile: full saves PNG/HTML/evidence/MHTML/state JSON; fast saves PNG/HTML only")

    # --- Acceptance DOCX export ---
    parser.add_argument("--acceptance-docx", action="store_true",
                        help="根据执行结果生成验收 DOCX、证据 ZIP 和回填报告")
    parser.add_argument("--acceptance-run-output", default=None,
                        help="已有执行输出目录；不指定时使用当前执行输出目录")
    parser.add_argument("--acceptance-evidence-dirs", nargs="+", default=None,
                        help="一个或多个已执行证据目录；支持拖拽任务目录或设备分类目录")
    parser.add_argument("--acceptance-evidence-dir", action="append", default=[],
                        help="一个已执行证据目录；可重复传入")
    parser.add_argument("--acceptance-template", default=None,
                        help="验收 DOCX 模板路径；默认使用项目内置模板")
    parser.add_argument("--acceptance-output-dir", default=None,
                        help="验收文档导出目录；默认使用执行输出目录")

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


def mode_arg_was_explicit(argv: list[str] | None = None) -> bool:
    """Return True when the caller explicitly supplied --mode/-m."""
    args = sys.argv[1:] if argv is None else list(argv)
    return any(
        arg == "--mode"
        or arg.startswith("--mode=")
        or arg == "-m"
        or (arg.startswith("-m") and not arg.startswith("--"))
        for arg in args
    )


def resolve_execution_cli(args, argv: list[str] | None = None) -> tuple[str, int | None, int | None, int]:
    """Resolve deprecated --concurrency into mode and worker overrides.

    Returns (mode, max_bmc_workers, max_ssh_workers, legacy_concurrency).
    """
    legacy_concurrency = int(getattr(args, "concurrency", 0) or 0)
    mode = getattr(args, "mode", None) or "sequential"
    if legacy_concurrency > 1 and not mode_arg_was_explicit(argv):
        mode = "full"

    max_bmc_workers = getattr(args, "max_bmc_workers", None)
    max_ssh_workers = getattr(args, "max_ssh_workers", None)
    if legacy_concurrency > 1:
        if max_bmc_workers is None:
            max_bmc_workers = legacy_concurrency
        if max_ssh_workers is None:
            max_ssh_workers = legacy_concurrency

    return mode, max_bmc_workers, max_ssh_workers, legacy_concurrency
