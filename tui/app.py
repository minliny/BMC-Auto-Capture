from __future__ import annotations

"""
TUI Dashboard — Textual-based real-time terminal UI.

Displays:
- Current phase and elapsed time
- CPU / Memory gauges
- Worker pool sizes
- Live device status table
- Progress bar
- Keyboard controls
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from textual.app import App as TextualApp, ComposeResult
from textual.widgets import Header, Footer, Static, DataTable, ProgressBar
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from rich.table import Table
from rich.text import Text


# --- Reactive state shared with the execution pipeline ---
@dataclass
class TUIState:
    phase: str = "IDLE"
    total: int = 0
    completed: int = 0
    success: int = 0
    failed: int = 0
    cpu_pct: float = 0.0
    mem_pct: float = 0.0
    bmc_workers: int = 0
    ssh_workers: int = 0
    elapsed: float = 0.0
    recent: list[dict] = field(default_factory=list)  # last N plan completions
    paused: bool = False
    running: bool = False


class BMCaptureTUI(TextualApp):
    """Textual TUI for BMC Auto-Capture."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #status-bar {
        height: 3;
        background: $surface;
        padding: 0 1;
    }
    #resource-bar {
        height: 3;
        background: $surface-darken-1;
        padding: 0 1;
    }
    #main-table {
        height: 1fr;
    }
    #progress-area {
        height: 3;
        background: $surface;
        padding: 0 1;
    }
    #footer-bar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    state: TUIState = TUIState()
    _start_time: float = 0.0
    _refresh_timer: Optional[threading.Timer] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-bar")
        yield Static(id="resource-bar")
        yield DataTable(id="main-table")
        yield Static(id="progress-area")
        yield Footer()

    def on_mount(self):
        # Setup data table
        table = self.query_one(DataTable)
        table.add_columns("Device", "Task", "Status", "Duration", "Details")

        self._start_time = time.time()
        self.set_interval(1.0, self._refresh_display)

    def _refresh_display(self):
        self.state.elapsed = time.time() - self._start_time

        # Status bar
        elapsed_str = f"{self.state.elapsed:.0f}s"
        phase_str = self.state.phase
        if self.state.paused:
            phase_str += " [PAUSED]"

        status = self.query_one("#status-bar", Static)
        status.update(
            f"Phase: {phase_str}  |  "
            f"Progress: {self.state.completed}/{self.state.total}  |  "
            f"Success: {self.state.success}  Failed: {self.state.failed}  |  "
            f"Elapsed: {elapsed_str}"
        )

        # Resource bar
        resource = self.query_one("#resource-bar", Static)
        resource.update(
            f"CPU: {self.state.cpu_pct:5.1f}%  |  "
            f"MEM: {self.state.mem_pct:5.1f}%  |  "
            f"BMC Workers: {self.state.bmc_workers}  |  "
            f"SSH Workers: {self.state.ssh_workers}"
        )

        # Progress
        progress_area = self.query_one("#progress-area", Static)
        if self.state.total > 0:
            pct = self.state.completed / self.state.total * 100
            bar_len = 40
            filled = int(bar_len * self.state.completed / self.state.total)
            bar = "█" * filled + "░" * (bar_len - filled)
            progress_area.update(f"[{bar}] {pct:.1f}%")
        else:
            progress_area.update("Waiting for execution to start...")

        # Update table with recent entries
        table = self.query_one(DataTable)
        for entry in self.state.recent[-20:]:
            try:
                table.add_row(
                    entry.get("device", "")[:20],
                    entry.get("task", "")[:30],
                    entry.get("status", ""),
                    entry.get("duration", ""),
                    entry.get("details", "")[:40],
                )
            except Exception:
                pass
        self.state.recent.clear()

    def update_state(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)

    def add_result(self, device: str, task: str, status: str, duration: str, details: str = ""):
        self.state.recent.append({
            "device": device,
            "task": task,
            "status": status,
            "duration": duration,
            "details": details,
        })
        if status == "SUCCESS" or status == "EXEC_SUCCESS":
            self.state.success += 1
        elif status.startswith("EXEC_FAILED"):
            self.state.failed += 1
        self.state.completed += 1

    def on_key(self, event):
        if event.key == "p":
            self.state.paused = not self.state.paused
        elif event.key == "s":
            self.exit()
        elif event.key == "q":
            self.exit()


def run_tui(state: TUIState | None = None) -> None:
    """Launch the TUI with an optional externally-managed state."""
    app = BMCaptureTUI()
    if state:
        app.state = state
    app.run()
