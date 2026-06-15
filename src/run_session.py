"""Runtime session state helpers for App execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time


_TIMESTAMP_SUFFIX_RE = re.compile(r"^(?P<base>.+)[\\/]\d{8}_\d{6}$")


def strip_timestamp_suffix(output_root: str | Path) -> str:
    """Return the configured output base without a trailing run timestamp."""
    base_root = str(output_root)
    match = _TIMESTAMP_SUFFIX_RE.match(base_root)
    if match:
        return match.group("base")
    return base_root


def build_timestamped_output_root(output_root: str | Path, timestamp: str) -> str:
    """Build the per-run output directory under the stable output base."""
    return str(Path(strip_timestamp_suffix(output_root)) / timestamp)


@dataclass(frozen=True)
class RunSession:
    """State that belongs to one App run."""

    started_at: float
    timestamp: str
    output_root: str

    @classmethod
    def start(
        cls,
        configured_output_root: str | Path,
        *,
        timestamp: str | None = None,
        started_at: float | None = None,
    ) -> "RunSession":
        run_ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
        run_started_at = time.time() if started_at is None else started_at
        return cls(
            started_at=run_started_at,
            timestamp=run_ts,
            output_root=build_timestamped_output_root(configured_output_root, run_ts),
        )
