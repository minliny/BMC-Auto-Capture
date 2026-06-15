from __future__ import annotations

from pathlib import Path

from src.run_session import (
    RunSession,
    build_timestamped_output_root,
    strip_timestamp_suffix,
)


def test_strip_timestamp_suffix_keeps_plain_output_root():
    assert strip_timestamp_suffix("/tmp/bmc-output") == "/tmp/bmc-output"


def test_strip_timestamp_suffix_removes_previous_run_timestamp():
    assert strip_timestamp_suffix("/tmp/bmc-output/20260615_123456") == "/tmp/bmc-output"


def test_strip_timestamp_suffix_removes_windows_previous_run_timestamp():
    assert strip_timestamp_suffix(r"C:\bmc-output\20260615_123456") == r"C:\bmc-output"


def test_build_timestamped_output_root_does_not_nest_timestamps():
    root = build_timestamped_output_root("/tmp/bmc-output/20260615_123456", "20260615_130000")
    assert root == str(Path("/tmp/bmc-output") / "20260615_130000")


def test_run_session_start_records_output_root_and_start_time():
    session = RunSession.start(
        "/tmp/bmc-output/20260615_123456",
        timestamp="20260615_130000",
        started_at=123.45,
    )
    assert session.started_at == 123.45
    assert session.timestamp == "20260615_130000"
    assert session.output_root == str(Path("/tmp/bmc-output") / "20260615_130000")
