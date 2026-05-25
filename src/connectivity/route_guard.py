from __future__ import annotations

"""
Route Guard — Windows VPN-aware route monitoring.

Captures route snapshots before execution and monitors for changes.
When VPN reconnects, routes are rewritten — the guard detects this and
stops dispatching new tasks.

Default: observe mode (no route modification).
"""

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("bmc_auto_capture.route_guard")


@dataclass
class RouteSnapshot:
    timestamp: float = field(default_factory=time.time)
    raw_output: str = ""
    routes: set[str] = field(default_factory=set)


class RouteGuard:
    """Windows route monitoring with snapshot-diff detection."""

    def __init__(self, check_interval: float = 30.0):
        self._interval = check_interval
        self._before: RouteSnapshot | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._routes_changed = threading.Event()

        # Callbacks
        self.on_change: callable | None = None

    @property
    def routes_changed(self) -> bool:
        return self._routes_changed.is_set()

    # ------------------------------------------------------------------
    def capture_snapshot(self) -> RouteSnapshot:
        """Capture current Windows routing table."""
        raw = ""
        try:
            # Primary: route print
            raw = subprocess.check_output(
                ["route", "print"],
                shell=True,
                text=True,
                timeout=10,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            try:
                # Fallback: Get-NetRoute (PowerShell)
                raw = subprocess.check_output(
                    ["powershell", "-Command", "Get-NetRoute | Format-Table -AutoSize"],
                    shell=True,
                    text=True,
                    timeout=10,
                    stderr=subprocess.STDOUT,
                )
            except Exception as e:
                logger.warning("Failed to capture route snapshot: %s", e)
                raw = f"ERROR: {e}"

        # Parse routes into a set for easy diffing
        routes: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("=") and not line.startswith("-"):
                # Keep lines with IP addresses (simple heuristic)
                if any(c.isdigit() for c in line):
                    routes.add(line)

        return RouteSnapshot(timestamp=time.time(), raw_output=raw, routes=routes)

    def diff(self, before: RouteSnapshot, after: RouteSnapshot) -> list[str]:
        """Return list of changed route descriptions."""
        changes: list[str] = []

        added = after.routes - before.routes
        removed = before.routes - after.routes

        for r in sorted(added):
            changes.append(f"[ADDED] {r}")
        for r in sorted(removed):
            changes.append(f"[REMOVED] {r}")

        return changes

    # ------------------------------------------------------------------
    def start(self):
        """Start background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._routes_changed.clear()
        self._before = self.capture_snapshot()
        logger.info("RouteGuard: initial snapshot captured (%d routes)", len(self._before.routes))

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="route-guard")
        self._thread.start()
        logger.info("RouteGuard: monitoring started (interval=%.1fs)", self._interval)

    def stop(self) -> RouteSnapshot | None:
        """Stop monitoring, return final snapshot."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)

        after = self.capture_snapshot()
        logger.info("RouteGuard: final snapshot captured (%d routes)", len(after.routes))

        if self._before:
            changes = self.diff(self._before, after)
            if changes:
                logger.warning("RouteGuard: %d route changes detected during execution:", len(changes))
                for c in changes[:20]:
                    logger.warning("  %s", c)
                if len(changes) > 20:
                    logger.warning("  ... and %d more changes", len(changes) - 20)
            else:
                logger.info("RouteGuard: no route changes detected")

        return after

    def _monitor_loop(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break

            try:
                current = self.capture_snapshot()
                if self._before:
                    changes = self.diff(self._before, current)
                    if changes:
                        logger.warning("RouteGuard: route change detected! %d changes", len(changes))
                        self._routes_changed.set()
                        if self.on_change:
                            self.on_change(changes)
            except Exception as e:
                logger.error("RouteGuard: monitor error: %s", e)
