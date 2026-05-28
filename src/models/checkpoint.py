"""
Checkpoint models — evidence checkpoint specifications and results.
"""


from __future__ import annotations
from dataclasses import dataclass, field
import time


@dataclass
class CheckpointResult:
    """Result of a single evidence checkpoint evaluation."""
    checkpoint_name: str        # e.g. "url_correct", "logical_drive_visible"
    status: str                 # "CHECK_PASS" | "CHECK_FAIL" | "CHECK_WARN" | "CHECK_SKIP"
    details: str = ""           # 判定依据描述
    evidence_ref: str = ""      # 关联的截图/HTML/TXT 路径
    evaluated_at: float = 0.0   # 时间戳

    def __post_init__(self):
        if self.evaluated_at == 0.0:
            self.evaluated_at = time.time()


@dataclass
class CheckpointSpec:
    """Specification for a single evidence checkpoint.

    Checkpoints are read-only assertions evaluated against captured
    evidence (page, HTML, SSH output) AFTER artifacts are saved.
    They do NOT block execution or artifact saving.
    """
    name: str                   # checkpoint 唯一标识
    check_type: str             # "text_contains" | "text_not_contains" | "element_visible" |
                                # "element_not_visible" | "regex_match" | "regex_not_match" |
                                # "url_contains" | "element_text_contains"
    # BMC element checks:
    selector: str = ""          # CSS selector for element-based checks
    # SSH output checks:
    ssh_cmd_ref: str = ""       # e.g. "cmd:show_interfaces" — references command output by name
    # Common:
    target: str = ""            # 检查的文本内容或正则 pattern
    expect: str = ""            # 期望值（部分类型使用）
    severity: str = "ERROR"     # "ERROR" | "WARNING" | "INFO" — affects final_verdict weighting

    @classmethod
    def from_dict(cls, d: dict) -> "CheckpointSpec":
        return cls(
            name=str(d.get("name", "")),
            check_type=str(d.get("type", "")),
            selector=str(d.get("selector", "")),
            ssh_cmd_ref=str(d.get("ssh_cmd_ref", "")),
            target=str(d.get("target", "")),
            expect=str(d.get("expect", "")),
            severity=str(d.get("severity", "ERROR")),
        )


@dataclass
class CheckpointEvaluationResult:
    """Aggregated results from a checkpoint evaluation session."""
    results: list[CheckpointResult] = field(default_factory=list)

    @property
    def statuses(self) -> list[str]:
        return [r.status for r in self.results]

    @property
    def has_fail(self) -> bool:
        return "CHECK_FAIL" in self.statuses

    @property
    def has_warn(self) -> bool:
        return "CHECK_WARN" in self.statuses

    @property
    def all_pass(self) -> bool:
        return bool(self.results) and all(s == "CHECK_PASS" for s in self.statuses)

    @property
    def all_skip(self) -> bool:
        return bool(self.results) and all(s == "CHECK_SKIP" for s in self.statuses)

    def rollup_status(self) -> str:
        """Aggregate individual checkpoint results into a single status."""
        if not self.results:
            return "CHECK_DISABLED"
        if self.has_fail:
            return "CHECK_FAIL"
        if self.has_warn:
            return "CHECK_WARN"
        if self.all_pass:
            return "CHECK_PASS"
        if self.all_skip:
            return "CHECK_SKIP"
        return "CHECK_DISABLED"

    def summary(self) -> str:
        """Human-readable one-line summary."""
        counts = {}
        for s in self.statuses:
            counts[s] = counts.get(s, 0) + 1
        parts = [f"{v}×{k.replace('CHECK_','')}" for k, v in sorted(counts.items())]
        return ", ".join(parts) if parts else "CHECK_DISABLED"