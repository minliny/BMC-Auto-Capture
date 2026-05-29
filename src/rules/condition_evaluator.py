"""
Condition evaluator — separate framework for capture_ready_conditions
and evidence_checkpoints.

capture_ready_conditions: check LIVE Playwright page BEFORE final_capture.
  Answers: "Is the page ready to screenshot?"

evidence_checkpoints: check SAVED artifacts AFTER final_capture.
  Answers: "Does the evidence satisfy the test case?"

These are two different evaluators with different contexts, not one merged engine.
"""

from __future__ import annotations
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bmc_auto_capture.conditions")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ConditionResult:
    """Result of a single condition evaluation."""
    condition_type: str       # e.g. "url_contains", "text_contains"
    status: str               # "PASS" | "FAIL" | "WARN" | "SKIP"
    target: str = ""          # the expected value / selector / pattern
    actual: str = ""          # snippet of what was found
    details: str = ""

    @property
    def is_pass(self) -> bool:
        return self.status == "PASS"


@dataclass
class ReadyConditionSpec:
    """Specification for a single capture_ready_condition.

    These check the live Playwright page BEFORE final_capture.
    """
    condition_type: str       # url_contains | selector_visible | text_contains | ...
    target: str = ""          # URL fragment / CSS selector / text / ...
    values: tuple = ()        # list of candidate values (text_contains_any)
    timeout_ms: int = 5000

    @classmethod
    def from_dict(cls, d: dict) -> "ReadyConditionSpec":
        vals = d.get("values", [])
        if isinstance(vals, list):
            vals = tuple(vals)
        return cls(
            condition_type=str(d.get("type", "")),
            target=str(d.get("target", d.get("selector", ""))),
            values=vals,
            timeout_ms=int(d.get("timeout_ms", d.get("timeout", 5000))),
        )


@dataclass
class CheckpointConditionSpec:
    """Specification for a single evidence_checkpoint.

    These check SAVED artifacts AFTER final_capture.
    """
    condition_type: str       # text_contains | regex_match | html_contains | ...
    name: str = ""            # human-readable checkpoint name
    target: str = ""          # expected text / regex pattern / artifact key
    values: tuple = ()        # list of candidate values (text_contains_any, not_contains_any)
    severity: str = "ERROR"   # ERROR | WARNING | INFO

    @classmethod
    def from_dict(cls, d: dict) -> "CheckpointConditionSpec":
        vals = d.get("values", [])
        if isinstance(vals, list):
            vals = tuple(vals)
        return cls(
            condition_type=str(d.get("type", "")),
            name=str(d.get("name", "")),
            target=str(d.get("target", "")),
            values=vals,
            severity=str(d.get("severity", "ERROR")),
        )


@dataclass
class ConditionEvaluationResult:
    """Aggregated results from evaluating a list of conditions."""
    results: list[ConditionResult] = field(default_factory=list)
    stage: str = ""  # "ready" | "checkpoint"

    def rollup(self) -> str:
        """Aggregate: FAIL > WARN > PASS > SKIP."""
        statuses = {r.status for r in self.results}
        if "FAIL" in statuses:
            return "FAIL"
        if "WARN" in statuses:
            return "WARN"
        if "PASS" in statuses:
            return "PASS"
        return "SKIP"

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        parts = [f"{v}x{'' if k == 'PASS' else k}" for k, v in sorted(counts.items())]
        return ", ".join(parts) if parts else "no conditions"

    def detail_lines(self) -> list[str]:
        lines = []
        for r in self.results:
            lines.append(
                f"[{r.status}] {r.condition_type}: {r.target}"
                + (f" — {r.actual[:60]}" if r.actual else "")
                + (f" ({r.details})" if r.details else "")
            )
        return lines


# ---------------------------------------------------------------------------
# ArtifactContext — holds saved evidence for checkpoint evaluation
# ---------------------------------------------------------------------------

@dataclass
class ArtifactContext:
    """Collected artifacts available for evidence_checkpoint evaluation."""
    screenshot_path: str = ""
    html_path: str = ""
    txt_path: str = ""
    html_text: str = ""       # pre-extracted inner_text("body")
    txt_content: str = ""     # raw TXT file content (SSH output)

    @classmethod
    def from_execution_result(cls, result) -> "ArtifactContext":
        """Build from an ExecutionResult (BMC page still available for text)."""
        return cls(
            screenshot_path=result.screenshots[-1] if result.screenshots else "",
            html_path=getattr(result, "html_file", ""),
            txt_path=getattr(result, "txt_file", ""),
        )

    def load_html_text(self) -> str:
        """Load HTML content from file. Always re-reads (no cache in evaluator)."""
        if self.html_path and os.path.exists(self.html_path):
            try:
                with open(self.html_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return self.html_text or ""

    def load_txt_content(self) -> str:
        """Load TXT content from file. Always re-reads."""
        if self.txt_path and os.path.exists(self.txt_path):
            try:
                with open(self.txt_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return self.txt_content or ""


# ---------------------------------------------------------------------------
# Ready condition evaluator (live Playwright page)
# ---------------------------------------------------------------------------

async def evaluate_ready_conditions(
    page,
    specs: list[ReadyConditionSpec],
    protocol: str = "BMC",
) -> ConditionEvaluationResult:
    """Evaluate capture_ready_conditions against a live Playwright page.

    - BMC tasks with empty specs: defaults to page_alive + not_login_page.
    - SSH/TELNET tasks with empty specs: skip (READY_SKIP), no page checks.
    """
    if not specs:
        if protocol.upper() in ("SSH", "TELNET"):
            return ConditionEvaluationResult(
                stage="ready",
                results=[ConditionResult("ready_skip", "SKIP", "", "",
                          "SSH/TELNET: no capture_ready_conditions configured")],
            )
        # BMC defaults
        specs = [
            ReadyConditionSpec("page_alive"),
            ReadyConditionSpec("not_login_page"),
        ]

    eval_result = ConditionEvaluationResult(stage="ready")
    for spec in specs:
        cr = await _eval_one_ready(page, spec)
        eval_result.results.append(cr)
    return eval_result


async def _eval_one_ready(page, spec: ReadyConditionSpec) -> ConditionResult:
    """Evaluate a single ready condition against a live page."""
    ct = spec.condition_type
    target = spec.target
    timeout = spec.timeout_ms

    try:
        if ct == "page_alive":
            if page is None:
                return ConditionResult(ct, "FAIL", target, "", "page is None")
            try:
                if page.is_closed():
                    return ConditionResult(ct, "FAIL", target, "", "page is closed")
            except Exception:
                pass
            return ConditionResult(ct, "PASS", target, "page alive")

        elif ct == "not_login_page":
            url = page.url
            if "/login" in url.lower():
                return ConditionResult(ct, "FAIL", target, url, "page on login URL")
            # Also check for login form elements
            login_indicators = [
                'input[name="username"]', 'input[name="password"]',
                '#login-container', '#btLogin',
            ]
            for sel in login_indicators:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return ConditionResult(ct, "FAIL", target, url,
                                               f"login element visible: {sel}")
                except Exception:
                    continue
            return ConditionResult(ct, "PASS", target, url)

        elif ct == "url_contains":
            url = page.url
            ok = target in url
            return ConditionResult(
                ct, "PASS" if ok else "FAIL", target,
                url[:120], "" if ok else f"'{target}' not in URL"
            )

        elif ct == "url_not_contains":
            url = page.url
            ok = target not in url
            return ConditionResult(
                ct, "PASS" if ok else "FAIL", target,
                url[:120], "" if ok else f"'{target}' found in URL"
            )

        elif ct == "selector_visible":
            el = await page.query_selector(target)
            visible = bool(el and await el.is_visible())
            return ConditionResult(
                ct, "PASS" if visible else "FAIL", target,
                "visible" if visible else "not found/visible"
            )

        elif ct == "selector_enabled":
            el = await page.query_selector(target)
            if not el:
                return ConditionResult(ct, "FAIL", target, "not found")
            enabled = await el.is_enabled()
            return ConditionResult(
                ct, "PASS" if enabled else "FAIL", target,
                "enabled" if enabled else "disabled"
            )

        elif ct == "text_contains":
            text = await page.inner_text("body")
            ok = target in text
            snippet = _snippet(text, target, 40) if not ok else ""
            return ConditionResult(
                ct, "PASS" if ok else "FAIL", target,
                snippet, "" if ok else f"'{target}' not found in page text"
            )

        elif ct == "text_contains_any":
            text = await page.inner_text("body")
            candidates = _resolve_candidates(spec)
            if not candidates:
                return ConditionResult(ct, "SKIP", target, "", "no candidates configured")
            for cand in candidates:
                if cand in text:
                    return ConditionResult(ct, "PASS", target, cand, f"matched '{cand}'")
            return ConditionResult(
                ct, "FAIL", target, _snippet(text, candidates[0], 40),
                f"none of {candidates} found"
            )

        else:
            return ConditionResult(ct, "SKIP", target, "", f"unknown type: {ct}")

    except Exception as e:
        logger.warning("Ready condition '%s' error: %s", ct, e)
        return ConditionResult(ct, "WARN", target, "", f"eval error: {e}")


# ---------------------------------------------------------------------------
# Evidence checkpoint evaluator (saved artifacts)
# ---------------------------------------------------------------------------

def evaluate_evidence_checkpoints(
    specs: list[CheckpointConditionSpec],
    artifacts: ArtifactContext,
    page_text: str = "",
) -> ConditionEvaluationResult:
    """Evaluate evidence_checkpoints against saved artifacts.

    Args:
        specs: list of checkpoint specs
        artifacts: ArtifactContext with screenshot/html/txt paths
        page_text: pre-extracted page inner_text (from BMC) or SSH output
    """
    if not specs:
        eval_result = ConditionEvaluationResult(stage="checkpoint")
        return eval_result

    # Ensure artifact text is loaded
    html_text = artifacts.load_html_text()
    txt_content = artifacts.load_txt_content() or page_text

    eval_result = ConditionEvaluationResult(stage="checkpoint")
    for spec in specs:
        cr = _eval_one_checkpoint(spec, artifacts, html_text, txt_content, page_text)
        eval_result.results.append(cr)
    return eval_result


def _eval_one_checkpoint(
    spec: CheckpointConditionSpec,
    artifacts: ArtifactContext,
    html_text: str,
    txt_content: str,
    page_text: str,
) -> ConditionResult:
    """Evaluate a single evidence checkpoint."""
    ct = spec.condition_type
    target = spec.target
    severity = spec.severity
    name = spec.name

    try:
        if ct == "file_exists":
            path = _resolve_artifact_path(target, artifacts)
            ok = bool(path and os.path.exists(path))
            return ConditionResult(ct, "PASS" if ok else "FAIL", path,
                                   "exists" if ok else f"not found (key={target})",
                                   f"[{name}]" if name else "")

        elif ct == "html_contains":
            if not html_text:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no HTML loaded")
            ok = target in html_text
            snippet = _snippet(html_text, target, 40) if not ok else ""
            status = "FAIL" if (not ok and severity == "ERROR") else \
                     "WARN" if (not ok and severity == "WARNING") else \
                     "PASS" if ok else "SKIP"
            return ConditionResult(ct, status, target, snippet,
                                   f"[{name}]" if name else "")

        elif ct == "txt_contains":
            content = txt_content or page_text
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no TXT content")
            ok = target in content
            status = "FAIL" if (not ok and severity == "ERROR") else \
                     "WARN" if (not ok and severity == "WARNING") else \
                     "PASS" if ok else "SKIP"
            return ConditionResult(ct, status, target, "", f"[{name}]" if name else "")

        elif ct == "text_contains":
            content = page_text or txt_content
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no text content")
            ok = target in content
            status = "FAIL" if (not ok and severity == "ERROR") else \
                     "WARN" if (not ok and severity == "WARNING") else "PASS"
            return ConditionResult(ct, status, target, "", f"[{name}]" if name else "")

        elif ct == "text_contains_any":
            content = page_text or txt_content
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no text content")
            candidates = _resolve_candidates(spec)
            if not candidates:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no candidates configured")
            for cand in candidates:
                if cand in content:
                    return ConditionResult(ct, "PASS", target, cand,
                                           f"[{name}] matched '{cand}'")
            status = "FAIL" if severity == "ERROR" else "WARN"
            return ConditionResult(ct, status, target, "",
                                   f"[{name}] none of {candidates} found")

        elif ct == "text_not_contains":
            content = page_text or txt_content
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no text content")
            ok = target not in content
            status = "FAIL" if (not ok and severity == "ERROR") else \
                     "WARN" if (not ok and severity == "WARNING") else "PASS"
            return ConditionResult(ct, status, target, "",
                                   f"[{name}]" if name else "")

        elif ct == "not_contains_any":
            content = page_text or txt_content
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no text content")
            candidates = _resolve_candidates(spec)
            if not candidates:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no candidates configured")
            found = [c for c in candidates if c in content]
            if found:
                status = "FAIL" if severity == "ERROR" else "WARN"
                return ConditionResult(ct, status, target, ", ".join(found),
                                       f"[{name}] forbidden: {found}")
            return ConditionResult(ct, "PASS", target, "", f"[{name}]")

        elif ct == "regex_match":
            content = page_text or txt_content
            if not content:
                return ConditionResult(ct, "SKIP", target, "", f"[{name}] no text content")
            match = re.search(target, content)
            ok = bool(match)
            status = "FAIL" if (not ok and severity == "ERROR") else \
                     "WARN" if (not ok and severity == "WARNING") else "PASS"
            return ConditionResult(ct, status, target,
                                   match.group(0)[:60] if match else "",
                                   f"[{name}]" if name else "")

        else:
            return ConditionResult(ct, "SKIP", target, "",
                                   f"[{name}] unknown type: {ct}")

    except Exception as e:
        logger.warning("Checkpoint '%s' error: %s", name or ct, e)
        return ConditionResult(ct, "WARN", target, "",
                               f"[{name}] eval error: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_candidates(spec) -> list[str]:
    """Resolve candidate values from spec: prefer 'values' array, fallback to target '||' split."""
    vals = getattr(spec, "values", ())
    if vals:
        return [str(v).strip() for v in vals if str(v).strip()]
    target = getattr(spec, "target", "")
    if target:
        return [s.strip() for s in target.split("||") if s.strip()]
    return []


def _resolve_artifact_path(target: str, artifacts: ArtifactContext) -> str:
    """Resolve artifact key to actual file path.

    Supported keys: screenshot, html_file, txt_file, log_file.
    If target is empty, falls back to first available artifact.
    If target is a literal path, returns as-is if it exists.
    """
    key_map = {
        "screenshot": artifacts.screenshot_path,
        "html_file": artifacts.html_path,
        "txt_file": artifacts.txt_path,
        "log_file": getattr(artifacts, "log_path", ""),
    }
    # Known key → resolve directly
    if target in key_map:
        return key_map[target] or ""
    # Empty target → fallback to any available artifact
    if not target:
        for path in [artifacts.screenshot_path, artifacts.html_path, artifacts.txt_path]:
            if path:
                return path
        return ""
    # Literal path
    if os.path.exists(target):
        return target
    return ""


def _snippet(text: str, keyword: str, context: int = 40) -> str:
    """Extract a snippet around keyword from text."""
    if not text or not keyword:
        return text[:80] if text else ""
    idx = text.find(keyword)
    if idx < 0:
        return text[:80]
    start = max(0, idx - context)
    end = min(len(text), idx + len(keyword) + context)
    return text[start:end]


def parse_ready_specs(raw) -> list[ReadyConditionSpec]:
    """Parse capture_ready_conditions from tasks.json dict or list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [ReadyConditionSpec.from_dict(r) for r in raw if isinstance(r, dict)]
    return []


def parse_checkpoint_specs(raw) -> list[CheckpointConditionSpec]:
    """Parse evidence_checkpoints from tasks.json dict or list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [CheckpointConditionSpec.from_dict(c) for c in raw if isinstance(c, dict)]
    return []
