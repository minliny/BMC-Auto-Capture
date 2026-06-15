"""Module entry point for ``python -m bmc_auto_capture``."""

from __future__ import annotations

from .cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
