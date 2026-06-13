from __future__ import annotations

import time
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
