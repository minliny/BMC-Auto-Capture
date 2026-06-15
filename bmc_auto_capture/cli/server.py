"""Public server entry-point facade."""

from __future__ import annotations

from src.cli.server import build_server_parser, server_main

__all__ = ["build_server_parser", "server_main"]
