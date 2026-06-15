from __future__ import annotations

import time
import logging
from types import SimpleNamespace

from src.executor.ssh_executor import SSHExecutor, VRP_PROMPT_RE


class FakeInteractiveChannel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.timeout = None

    def send(self, data):
        self.sent.append(data)

    def recv_ready(self):
        return bool(self.responses)

    def recv(self, _size):
        return self.responses.pop(0)

    def settimeout(self, timeout):
        self.timeout = timeout


class ScriptedInteractiveChannel:
    def __init__(self, banner_chunks, scripted_responses):
        self._queue = list(banner_chunks)
        self._scripted_responses = {
            key: [list(chunks) for chunks in value]
            for key, value in scripted_responses.items()
        }
        self.sent = []
        self.timeout = None
        self.closed = False
        self.pty_args = None
        self.shell_invoked = False

    def get_pty(self, **kwargs):
        self.pty_args = kwargs

    def invoke_shell(self):
        self.shell_invoked = True

    def send(self, data):
        self.sent.append(data)
        responses = self._scripted_responses.get(data)
        if responses:
            self._queue.extend(responses.pop(0))
        return len(data)

    def recv_ready(self):
        return bool(self._queue)

    def recv(self, _size):
        if not self._queue:
            return b""
        return self._queue.pop(0)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class ScriptedInteractiveTransport:
    def __init__(self, channel):
        self.channel = channel

    def is_active(self):
        return True

    def open_session(self):
        return self.channel


class ScriptedInteractiveClient:
    def __init__(self, channel):
        self.transport = ScriptedInteractiveTransport(channel)

    def get_transport(self):
        return self.transport


def test_vrp_prompt_regex_accepts_long_angle_and_bracket_prompts():
    assert VRP_PROMPT_RE.search(
        "<HRBB13-P-POD18-ROCE_Leaf-HW-AT900A3-61-ITC20261GB>"
    )
    assert VRP_PROMPT_RE.search(
        "output\r\n[HRBB13-P-POD18-ROCE_Leaf-HW-AT900A3-61-ITC20261GB]"
    )


def test_send_and_read_stops_immediately_when_prompt_arrives():
    channel = FakeInteractiveChannel([
        b"display interface transceiver\r\n",
        b"100GE1/0/1 transceiver present\r\n",
        b"<HRBB13-P-POD18-ROCE_Leaf-HW-AT900A3-61-ITC20261GB>",
    ])
    executor = SSHExecutor(command_timeout=60.0, idle_timeout=5.0)

    started = time.monotonic()
    output, meta = executor._send_and_read(
        channel,
        "display interface transceiver",
        SimpleNamespace(device_name="L1-test"),
        timeout=60.0,
    )

    assert time.monotonic() - started < 1.0
    assert "transceiver present" in output
    assert meta["prompt_detected"] is True
    assert meta["hard_timeout_hit"] is False
    assert meta["idle_timeout_hit"] is False


def test_send_and_read_handles_each_pagination_marker_once():
    channel = FakeInteractiveChannel([
        b"display interface transceiver\r\npage 1\r\n---- More ----",
        b"\r\npage 2\r\nMore\r\n",
        b"\r\npage 3\r\n<L2_SWITCH_01>",
    ])
    executor = SSHExecutor(command_timeout=60.0, idle_timeout=5.0)

    output, meta = executor._send_and_read(
        channel,
        "display interface transceiver",
        SimpleNamespace(device_name="L2-test"),
        timeout=60.0,
    )

    assert "page 3" in output
    assert channel.sent == ["display interface transceiver\n", " ", " "]
    assert meta["pagination_detected"] is True
    assert meta["pagination_count"] == 2
    assert meta["prompt_detected"] is True


def test_send_and_read_handles_warning_more_marker_and_logs_space(caplog):
    channel = FakeInteractiveChannel([
        b"display interface transceiver\r\nWarning information:\r\n  ---- More ----",
        b"\r\ntransceiver details\r\n<SwitchName>",
    ])
    executor = SSHExecutor(command_timeout=60.0, idle_timeout=5.0)
    caplog.set_level(logging.INFO, logger="bmc_auto_capture.ssh")

    output, meta = executor._send_and_read(
        channel,
        "display interface transceiver",
        SimpleNamespace(device_name="vrp-test"),
        timeout=60.0,
    )

    assert "transceiver details" in output
    assert channel.sent == ["display interface transceiver\n", " "]
    assert meta["prompt_detected"] is True
    assert meta["output_classification"] != "PROMPT_TIMEOUT"
    assert any(
        "pager prompt detected, sending space" in record.message
        for record in caplog.records
    )


def test_send_and_read_handles_control_char_more_marker_and_cleans_transcript():
    channel = FakeInteractiveChannel([
        b"display interface transceiver\r\npage 1\r\n\x1b[24;1H---- More ----\x08\x08\x08",
        b"\r\npage 2\r\n<SwitchName>",
    ])
    executor = SSHExecutor(command_timeout=60.0, idle_timeout=5.0)

    output, meta = executor._send_and_read(
        channel,
        "display interface transceiver",
        SimpleNamespace(device_name="vrp-test"),
        timeout=60.0,
    )
    transcript = executor._format_ssh_transcript([output], "interactive_shell")

    assert channel.sent == ["display interface transceiver\n", " "]
    assert meta["prompt_detected"] is True
    assert "page 1" in transcript
    assert "page 2" in transcript
    assert "More" not in transcript
    assert "\x08" not in transcript


def test_send_and_read_accepts_bracket_prompt():
    channel = FakeInteractiveChannel([
        b"display current-configuration\r\nconfiguration line\r\n[SwitchName]",
    ])
    executor = SSHExecutor(command_timeout=60.0, idle_timeout=5.0)

    output, meta = executor._send_and_read(
        channel,
        "display current-configuration",
        SimpleNamespace(device_name="vrp-test"),
        timeout=60.0,
    )

    assert "configuration line" in output
    assert meta["prompt_detected"] is True
    assert meta["output_classification"] == "OK"


def test_interactive_shell_runs_business_command_after_screen_length_success():
    channel = ScriptedInteractiveChannel(
        banner_chunks=[b"<SwitchName>"],
        scripted_responses={
            "screen-length 0 temporary\n": [[
                b"screen-length 0 temporary\r\n<SwitchName>",
            ]],
            "display version\n": [[
                b"display version\r\nVRP software version\r\n<SwitchName>",
            ]],
        },
    )
    executor = SSHExecutor(command_timeout=1.0, idle_timeout=0.02)

    all_output, has_failure, has_timeout, failure_reasons, _cmd_outputs, step_results = (
        executor._execute_interactive_shell(
            ScriptedInteractiveClient(channel),
            SimpleNamespace(device_name="vrp-test"),
            [("cmd_0", "display version")],
            {},
            SimpleNamespace(command_timeout=1.0, idle_timeout=0.02),
        )
    )

    assert channel.sent == ["screen-length 0 temporary\n", "display version\n"]
    assert "VRP software version" in "".join(all_output)
    assert has_failure is False
    assert has_timeout is False
    assert failure_reasons == []
    assert step_results[0].status == "SUCCESS"


def test_interactive_shell_screen_length_failure_does_not_fail_business_command():
    channel = ScriptedInteractiveChannel(
        banner_chunks=[b"<SwitchName>"],
        scripted_responses={
            "screen-length 0 temporary\n": [[
                b"screen-length 0 temporary\r\nError: unsupported command\r\n",
            ]],
            "display interface transceiver\n": [[
                b"display interface transceiver\r\npage 1\r\n---- More ----",
            ]],
            " ": [[
                b"\r\npage 2\r\n<SwitchName>",
            ]],
        },
    )
    executor = SSHExecutor(command_timeout=1.0, idle_timeout=0.02)

    all_output, has_failure, has_timeout, failure_reasons, _cmd_outputs, step_results = (
        executor._execute_interactive_shell(
            ScriptedInteractiveClient(channel),
            SimpleNamespace(device_name="vrp-test"),
            [("cmd_0", "display interface transceiver")],
            {},
            SimpleNamespace(command_timeout=1.0, idle_timeout=0.02),
        )
    )

    assert channel.sent == [
        "screen-length 0 temporary\n",
        "display interface transceiver\n",
        " ",
    ]
    assert "page 2" in "".join(all_output)
    assert has_failure is False
    assert has_timeout is False
    assert failure_reasons == []
    assert step_results[0].status == "SUCCESS"


def test_non_vrp_default_strategy_stays_terminal_session():
    executor = SSHExecutor()

    strategy = executor._get_ssh_strategy(
        SimpleNamespace(device_group="A3"),
        SimpleNamespace(_task_def={}),
    )

    assert strategy == "terminal_session"
