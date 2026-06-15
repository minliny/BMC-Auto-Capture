"""TI-001~003: SSH output protection and A3 marker diagnostics tests."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.executor.ssh_executor import SSHExecutor, HCCN_MARKER_RE
from src.out.screenshot import clean_output_for_png


class FakeChannel:
    """Fake paramiko channel for testing SSH output reading."""

    def __init__(self, chunks=None, prompt_after=None):
        self._chunks = list(chunks or [])
        self._prompt_after = prompt_after
        self._idx = 0
        self._closed = False
        self._timeout = 60
        self._exit_status = 0

    def recv_ready(self):
        return self._idx < len(self._chunks)

    def recv(self, size):
        if self._idx >= len(self._chunks):
            return b""
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return self._idx >= len(self._chunks)

    def settimeout(self, t):
        self._timeout = t

    def close(self):
        self._closed = True

    def send(self, data):
        return len(data)


class TestReadTerminalUntilIdle:
    """TI-001: terminal_session output protection."""

    def test_fast_output_then_idle_exits_normally(self):
        """Quick output then idle: normal exit with idle_timeout_hit."""
        executor = SSHExecutor()
        chunks = [b"output line 1\n", b"output line 2\n"]
        channel = FakeChannel(chunks=chunks)
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=5, idle_timeout=0.1,
        )
        assert "output line 1" in output
        assert meta["idle_timeout_hit"] is True
        assert meta["output_truncated"] is False
        assert meta["bytes_read"] > 0

    def test_continuous_output_triggers_max_bytes(self):
        """Continuous output triggers max_output_bytes limit."""
        executor = SSHExecutor()
        # Generate enough chunks to exceed 1MB
        big_chunks = [b"A" * 100000 for _ in range(20)]
        channel = FakeChannel(chunks=big_chunks)
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=5, idle_timeout=0.1, max_output_bytes=500000,
        )
        assert meta["output_truncated"] is True
        assert meta["timeout_reason"] == "max_output_bytes_reached"

    def test_no_output_triggers_no_output_timeout(self):
        """No output at all triggers no_output_timeout."""
        executor = SSHExecutor()
        channel = FakeChannel(chunks=[])
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=1, idle_timeout=0.3,
        )
        assert meta["timeout_reason"] == "no_output_timeout"
        assert meta["bytes_read"] == 0

    def test_meta_includes_last_non_empty_line(self):
        """Meta includes last_non_empty_line."""
        executor = SSHExecutor()
        chunks = [b"line1\n", b"line2\n", b"\n", b"line3\n"]
        channel = FakeChannel(chunks=chunks)
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=5, idle_timeout=0.1,
        )
        assert meta["last_non_empty_line"] == "line3"

    def test_sentinel_stops_without_idle_wait(self):
        """Linux terminal sessions stop as soon as the internal sentinel appears."""
        executor = SSHExecutor()
        sentinel = "__BMC_AUTO_CAPTURE_DONE_TEST__"
        chunks = [
            b"uname -a\n",
            b"Linux test-host\n",
            f"{sentinel}:0\n".encode("utf-8"),
            b"this should not be read\n",
        ]
        channel = FakeChannel(chunks=chunks)
        output, meta = executor._read_terminal_until_idle(
            channel,
            timeout=5,
            idle_timeout=5,
            stop_pattern=executor._terminal_sentinel_pattern(sentinel),
        )
        assert "Linux test-host" in output
        assert "this should not be read" not in output
        assert meta["timeout_reason"] == "sentinel_detected"
        assert meta["sentinel_detected"] is True
        assert executor._extract_terminal_sentinel_exit_code(output, sentinel) == 0

    def test_terminal_sentinel_is_stripped_from_transcript(self):
        executor = SSHExecutor()
        sentinel = "__BMC_AUTO_CAPTURE_DONE_TEST__"
        output = (
            "uname -a\n"
            "Linux test-host\n"
            f"printf '\\n{sentinel}:%s\\n' \"$?\"\n"
            f"{sentinel}:0\n"
            "$ "
        )
        stripped = executor._strip_terminal_sentinel(output, sentinel)
        assert sentinel not in stripped
        assert "Linux test-host" in stripped


class TestTranscriptFormatting:
    """SSH evidence should stay terminal-like without artificial section headers."""

    def test_terminal_transcript_has_no_command_output_headers(self):
        executor = SSHExecutor()

        transcript = executor._format_ssh_transcript(
            ["$ uname -a\nLinux test-host\n$ "],
            "terminal_session",
        )

        assert "Linux test-host" in transcript
        assert "=== COMMAND" not in transcript
        assert "=== OUTPUT" not in transcript

    def test_exec_command_transcript_has_no_command_output_headers(self):
        executor = SSHExecutor()

        transcript = executor._format_ssh_transcript(
            ["first output\n", "second output\n"],
            "exec_command",
        )

        assert "first output" in transcript
        assert "second output" in transcript
        assert "=== COMMAND" not in transcript
        assert "=== OUTPUT" not in transcript

    def test_terminal_transcript_preserves_raw_more_markers(self):
        executor = SSHExecutor()

        transcript = executor._format_ssh_transcript(
            [
                "display this\r\n"
                "page 1\r\n"
                "---- More ----\r\n"
                "page 2\r\n"
                "===more===\r\n"
                "<SwitchName>"
            ],
            "interactive_shell",
        )

        assert "---- More ----" in transcript
        assert "===more===" in transcript
        assert "[TRUNCATED:" not in transcript
        assert "完整请看" not in transcript

    def test_png_normalization_preserves_raw_more_markers(self):
        text = clean_output_for_png(
            "display this\r\n"
            "page 1\r\n"
            "---- More ----\r\n"
            "page 2\r\n"
            "===more===\r\n"
        )

        assert "---- More ----" in text
        assert "===more===" in text


class TestReadChannel:
    """TI-002: _read_channel large output protection."""

    def test_read_channel_with_output_limit(self):
        """_read_channel respects max_output_bytes."""
        executor = SSHExecutor()
        big_chunks = [b"B" * 100000 for _ in range(20)]
        channel = FakeChannel(chunks=big_chunks)
        stdout = MagicMock()
        stdout.read.return_value = b""
        stdin = MagicMock()
        device = MagicMock()
        device.device_name = "test-device"

        out, err, events, timed_out, more = executor._read_channel(
            channel, stdout, stdin, device,
            cmd_deadline=time.time() + 5,
            idle_timeout=0.1,
            max_output_bytes=500000,
        )
        total_bytes = sum(len(c) for c in out)
        assert total_bytes <= 600000  # some margin for chunk boundaries


class TestHccnMarker:
    """TI-003: A3 hccn_tool marker diagnostics."""

    def test_hccn_marker_regex(self):
        """HCCN marker regex matches ===========> N pattern."""
        text = "some output\n==============> 7\nmore output"
        matches = HCCN_MARKER_RE.findall(text)
        assert matches == ["7"]

    def test_hccn_marker_multiple(self):
        """Multiple markers are all found, last one returned."""
        text = "==============> 0\n==============> 5\n==============> 7\n"
        matches = HCCN_MARKER_RE.findall(text)
        assert matches == ["0", "5", "7"]

    def test_terminal_session_captures_last_marker(self):
        """terminal_session meta includes last_marker for hccn output."""
        executor = SSHExecutor()
        chunks = [
            b"==============> 0\n",
            b"==============> 5\n",
            b"==============> 7\n",
        ]
        channel = FakeChannel(chunks=chunks)
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=5, idle_timeout=0.1,
        )
        assert meta["last_marker"] == 7

    def test_terminal_session_marker_at_7_then_timeout(self):
        """When timeout after marker 7, diagnostics include last_marker=7."""
        executor = SSHExecutor()
        chunks = [
            b"==============> 0\n",
            b"==============> 5\n",
            b"==============> 7\n",
        ]
        channel = FakeChannel(chunks=chunks)
        output, meta = executor._read_terminal_until_idle(
            channel, timeout=5, idle_timeout=0.1,
        )
        # After chunks exhausted, idle_timeout will trigger
        assert meta["last_marker"] == 7
        assert meta["idle_timeout_hit"] is True
