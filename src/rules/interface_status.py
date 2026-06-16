"""Structured parsers for network interface status command output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_INTERFACE_BRIEF_RE = re.compile(r"\bdisplay\s+interface\s+brief\b", re.IGNORECASE)
_STATUS_SUFFIX_RE = re.compile(r"\([^)]*\)$")


@dataclass(frozen=True)
class InterfaceStatusRecord:
    interface: str
    physical: str
    protocol: str
    description: str
    raw_line: str


def is_interface_brief_command(command: str) -> bool:
    return bool(_INTERFACE_BRIEF_RE.search(command or ""))


def parse_interface_brief(output: str) -> list[InterfaceStatusRecord]:
    """Parse true interface rows from ``display interface brief`` output.

    The parser deliberately ignores command echo, prompts, headers, separators,
    legend/help text, and empty lines. A row is accepted only when it has an
    interface token followed by parseable physical and protocol status fields.
    """
    records: list[InterfaceStatusRecord] = []
    for raw_line in (output or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _ANSI_RE.sub("", raw_line).strip()
        if not line or _is_non_record_line(line):
            continue
        tokens = line.split()
        if len(tokens) < 3 or not _looks_like_interface_name(tokens[0]):
            continue
        physical, next_index = _read_status(tokens, 1)
        if not physical:
            continue
        protocol, next_index = _read_status(tokens, next_index)
        if not protocol:
            continue
        records.append(InterfaceStatusRecord(
            interface=tokens[0],
            physical=physical,
            protocol=protocol,
            description=" ".join(tokens[next_index:]),
            raw_line=line,
        ))
    return records


def status_matches(value: str, expected: str) -> bool:
    """Return True when a parsed status value matches a status token."""
    norm = normalize_status(value)
    target = normalize_status(expected)
    if not norm or not target:
        return False
    if norm == target or norm.endswith(f" {target}"):
        return True
    return target in re.split(r"[^a-z0-9]+", norm)


def normalize_status(value: str) -> str:
    lowered = (value or "").strip().lower()
    lowered = lowered.lstrip("*^!~")
    lowered = _STATUS_SUFFIX_RE.sub("", lowered).strip()
    return re.sub(r"\s+", " ", lowered)


def coerce_status_fields(raw: object, default: Iterable[str] = ("physical", "protocol")) -> list[str]:
    if raw is None or raw == "":
        values = list(default)
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Iterable):
        values = [str(part).strip() for part in raw]
    else:
        values = [str(raw).strip()]
    fields = [field for field in values if field in {"physical", "protocol"}]
    return fields or list(default)


def coerce_status_values(raw: object, default: Iterable[str] = ("down",)) -> list[str]:
    if raw is None or raw == "":
        values = list(default)
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Iterable):
        values = [str(part).strip() for part in raw]
    else:
        values = [str(raw).strip()]
    return [value for value in values if value]


def _read_status(tokens: list[str], index: int) -> tuple[str, int]:
    if index >= len(tokens):
        return "", index
    token = tokens[index]
    if token.lower() == "administratively" and index + 1 < len(tokens):
        next_token = tokens[index + 1]
        if normalize_status(next_token) == "down":
            return f"{token} {next_token}", index + 2
    if _is_status_token(token):
        return token, index + 1
    return "", index


def _is_status_token(token: str) -> bool:
    norm = normalize_status(token)
    if norm in {"up", "down"}:
        return True
    parts = re.split(r"[^a-z0-9]+", norm)
    return "up" in parts or "down" in parts


def _looks_like_interface_name(token: str) -> bool:
    lowered = token.lower()
    if lowered in {
        "interface", "phy", "protocol", "description", "display", "brief",
        "physical", "legend", "note",
    }:
        return False
    if ":" in token:
        return False
    return bool(re.search(r"[A-Za-z]", token)) and bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9/._:-]*$", token))


def _is_non_record_line(line: str) -> bool:
    lowered = line.lower()
    if is_interface_brief_command(line):
        return True
    if re.fullmatch(r"[<\[][^\r\n<>\[\]]{1,128}[>\]]", line):
        return True
    if set(line) <= {"-", "=", " "}:
        return True
    if lowered.startswith(("phy:", "protocol:", "legend", "note:", "display ")):
        return True
    if "interface" in lowered and "phy" in lowered and "protocol" in lowered:
        return True
    if lowered.startswith(("*down:", "^down:", "(l):", "(s):", "(b):")):
        return True
    return False
