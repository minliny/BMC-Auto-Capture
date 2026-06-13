"""OP-001: Realtime output device_group tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.out import console


class TestRealtimeDeviceGroup:
    """OP-001: Realtime output includes device_group."""

    def test_start_includes_device_group(self, capsys):
        """start() output includes device_group."""
        console.start("SSH", device_group="A3", device="dev1", task="task1")
        captured = capsys.readouterr()
        assert "[A3]" in captured.out
        assert "dev1" in captured.out
        assert "task1" in captured.out

    def test_start_missing_device_group_shows_dash(self, capsys):
        """Missing device_group shows '-'."""
        console.start("SSH", device="", task="task1")
        captured = capsys.readouterr()
        assert "[-]" in captured.out

    def test_done_includes_device_group(self, capsys):
        """done() output includes device_group."""
        console.done(1, 10, "OK", device_group="RM211", device="dev1", task="task1")
        captured = capsys.readouterr()
        assert "[RM211]" in captured.out

    def test_done_missing_device_group_shows_dash(self, capsys):
        """Missing device_group in done() shows '-'."""
        console.done(1, 10, "OK", device="dev1", task="task1")
        captured = capsys.readouterr()
        assert "[-]" in captured.out

    def test_progress_event_includes_device_group(self, capsys):
        """progress_event() output includes device_group."""
        console.progress_event("BMC", device_group="L1", device="dev1", task="task1", event="CAPTURE")
        captured = capsys.readouterr()
        assert "[L1]" in captured.out

    def test_a3_l1_l2_output_format(self, capsys):
        """A3/L1/L2 groups appear correctly in output."""
        for group in ("A3", "L1", "L2"):
            console.start("SSH", device_group=group, device="dev1", task="task1")
            captured = capsys.readouterr()
            assert f"[{group}]" in captured.out
