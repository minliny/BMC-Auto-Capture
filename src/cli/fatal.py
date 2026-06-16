"""Terminal-facing fatal error guard for CLI entry points."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from typing import Any, TextIO

from ..utils.sensitive import redact_sensitive_text


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _system_exit_code(code: Any) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def _return_code(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 0


def print_terminal_fault(
    exc: BaseException,
    *,
    stream: TextIO | None = None,
    show_traceback: bool | None = None,
) -> None:
    """Print a concise, stable fatal error that is visible in terminal launchers."""
    stream = stream or sys.stderr
    show_traceback = _truthy_env("BMC_SHOW_TRACEBACK") if show_traceback is None else show_traceback
    message = redact_sensitive_text(str(exc) or repr(exc))
    print("", file=stream)
    print("[FATAL] 执行端发生未处理异常，任务已停止。", file=stream)
    print(f"  类型: {exc.__class__.__name__}", file=stream)
    print(f"  原因: {message}", file=stream)
    print("  处理: 请保留本窗口输出，并检查本次输出目录中的日志/结果文件。", file=stream)
    if show_traceback:
        print("", file=stream)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=stream)


def run_with_terminal_fault_guard(
    entrypoint: Callable[[], Any],
    *,
    stream: TextIO | None = None,
    show_traceback: bool | None = None,
) -> int:
    """Run a CLI entrypoint and turn uncaught failures into visible terminal text."""
    stream = stream or sys.stderr
    try:
        return _return_code(entrypoint())
    except SystemExit as exc:
        code = _system_exit_code(exc.code)
        if exc.code not in (None, 0) and not isinstance(exc.code, int):
            print(redact_sensitive_text(str(exc.code)), file=stream)
        return code
    except KeyboardInterrupt:
        print("", file=stream)
        print("[INTERRUPTED] 用户中断，执行已停止。", file=stream)
        return 130
    except Exception as exc:
        print_terminal_fault(exc, stream=stream, show_traceback=show_traceback)
        return 1
