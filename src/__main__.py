"""
CLI entry point: python -m bmc_auto_capture --excel <path> [--config <path>]
"""


from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path

from .models.app_config import AppConfig
from .app import App


def main():
    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture v2.0 — 自动化测试证据采集平台",
    )
    parser.add_argument(
        "--excel", "-e",
        required=True,
        help="Path to Excel V2 configuration file (.xlsx)",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to YAML config file (default: config/default_config.yaml)",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["sequential", "full"],
        default="sequential",
        help="Execution mode (default: sequential)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Load config
    config_path = args.config or Path(__file__).parent.parent / "config" / "default_config.yaml"
    if Path(config_path).exists():
        config = AppConfig.from_yaml(config_path)
    else:
        config = AppConfig()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate Excel exists
    excel_path = Path(args.excel)
    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    # Run
    app = App(config)
    results = app.run(str(excel_path))

    # Exit code
    if not results:
        sys.exit(1)
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS", "EXEC_SKIPPED_PRECHECK_FAILED"))
    if failed > 0:
        sys.exit(1 if failed > len(results) * 0.5 else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
