"""
Console output helpers — color-coded, level-aware terminal output.
"""
from __future__ import annotations
import sys
import time

# ANSI color codes (work in PowerShell, Windows Terminal, macOS, Linux)
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_GRAY = "\033[90m"


def _color(code: str, text: str) -> str:
    """Wrap text in ANSI color if stdout supports it."""
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


def info(msg: str):
    print(_color(_BLUE, f"  • {msg}"), flush=True)


def ok(msg: str):
    print(_color(_GREEN, f"  ✓ {msg}"), flush=True)


def warn(msg: str):
    print(_color(_YELLOW, f"  ⚠ {msg}"), flush=True)


def error(msg: str):
    print(_color(_RED, f"  ✗ {msg}"), flush=True)


def start(protocol: str, device: str, task: str):
    print(_color(_CYAN, f"  ▶ [{protocol}] {device}  {task}"), flush=True)


def done(idx: int, total: int, status: str, device: str, task: str, reason: str = ""):
    """Print task completion. status: OK/FAIL/SKIP/ERR"""
    if status == "OK":
        icon = _color(_GREEN, "OK")
    elif status in ("FAIL", "ERR"):
        icon = _color(_RED, status)
    else:
        icon = _color(_YELLOW, status)
    r = f"  [{reason[:50]}]" if reason else ""
    print(f"  [{idx:>4}/{total}] {icon}  {device}  {task}{r}", flush=True)


def heartbeat(dispatched: int, done: int, pending: int,
              bmc_run: int, ssh_run: int, ready: int):
    print(_color(_DIM,
        f"  ── 心跳: 已派={dispatched} 完成={done} 待处理={pending} "
        f"BMC运行={bmc_run} SSH运行={ssh_run} 就绪={ready} ──"
    ), flush=True)


def progress(current: int, total: int, label: str = ""):
    bar_w = 30
    pct = current / max(total, 1)
    filled = int(bar_w * pct)
    bar = "█" * filled + "░" * (bar_w - filled)
    print(f"\r  [{bar}] {current}/{total} {label}  ", end="", flush=True)
    if current >= total:
        print()
