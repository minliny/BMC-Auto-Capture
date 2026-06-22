"""
Evidence Checkpoint Engine — evaluates read-only assertions against captured evidence.

Checkpoints are evaluated AFTER artifacts (screenshot, HTML, TXT) are saved.
They do NOT block execution or artifact saving.

checkpoint_status rollup:
  - CHECK_DISABLED: no checkpoints configured
  - CHECK_FAIL: any checkpoint returned CHECK_FAIL
  - CHECK_WARN: any checkpoint returned CHECK_WARN (no FAIL)
  - CHECK_PASS: all checkpoints passed
  - CHECK_SKIP: all checkpoints returned CHECK_SKIP
"""


from __future__ import annotations
import logging
import re
import time

from ..models.checkpoint import CheckpointResult, CheckpointSpec, CheckpointEvaluationResult
from .engine import RuleContext

logger = logging.getLogger("bmc_auto_capture.checkpoints")


class CheckpointEngine:
    """Evaluate evidence checkpoints against BMC page or SSH command output."""

    # Maps check_type string → handler method name
    _HANDLERS = {
        "text_contains": "_check_text_contains",
        "text_not_contains": "_check_text_not_contains",
        "element_visible": "_check_element_visible",
        "element_not_visible": "_check_element_not_visible",
        "element_text_contains": "_check_element_text_contains",
        "regex_match": "_check_regex_match",
        "regex_not_match": "_check_regex_not_match",
        "url_contains": "_check_url_contains",
    }

    async def evaluate(
        self,
        specs: list[CheckpointSpec],
        context: RuleContext,
        evidence_ref: str = "",
    ) -> CheckpointEvaluationResult:
        """Evaluate a list of checkpoint specs against the given context.

        Args:
            specs: list of CheckpointSpec to evaluate
            context: RuleContext with page (BMC) or ssh_session/ssh_output (SSH)
            evidence_ref: path to the primary evidence artifact (screenshot/HTML/TXT)

        Returns:
            CheckpointEvaluationResult with individual and rollup statuses
        """
        result = CheckpointEvaluationResult()

        for spec in specs:
            cp_result = await self._evaluate_one(spec, context, evidence_ref)
            result.results.append(cp_result)

        return result

    async def _evaluate_one(
        self,
        spec: CheckpointSpec,
        context: RuleContext,
        evidence_ref: str,
    ) -> CheckpointResult:
        """Evaluate a single checkpoint spec."""
        handler_name = self._HANDLERS.get(spec.check_type)
        if not handler_name:
            logger.warning("Unknown checkpoint type: %s", spec.check_type)
            return self._attach_spec_metadata(CheckpointResult(
                checkpoint_name=spec.name,
                status="CHECK_SKIP",
                details=f"Unknown check_type: {spec.check_type}",
                evidence_ref=evidence_ref,
                evaluated_at=time.time(),
            ), spec)

        handler = getattr(self, handler_name)
        try:
            return self._attach_spec_metadata(await handler(spec, context, evidence_ref), spec)
        except Exception as e:
            logger.warning("Checkpoint '%s' raised: %s", spec.name, e)
            return self._attach_spec_metadata(CheckpointResult(
                checkpoint_name=spec.name,
                status="CHECK_WARN",
                details=f"Checkpoint evaluation error: {e}",
                evidence_ref=evidence_ref,
                evaluated_at=time.time(),
            ), spec)

    def _attach_spec_metadata(self, result: CheckpointResult, spec: CheckpointSpec) -> CheckpointResult:
        result.severity = str(getattr(spec, "severity", "") or "ERROR")
        result.rule_id = str(getattr(spec, "rule_id", "") or "")
        result.rule_class = str(getattr(spec, "rule_class", "") or "")
        result.priority = str(getattr(spec, "priority", "") or "")
        result.result_layer = str(getattr(spec, "result_layer", "") or "")
        result.effect_on_final = str(getattr(spec, "effect_on_final", "") or "")
        if result.status == "CHECK_FAIL" and _checkpoint_is_non_blocking(spec):
            result.status = "CHECK_WARN"
        return result

    # ------------------------------------------------------------------
    # BMC (Playwright page) checkpoint handlers
    # ------------------------------------------------------------------

    async def _check_element_visible(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Element must exist and be visible on the page."""
        selector = ctx.resolve_var(spec.selector)
        el = await ctx.page.query_selector(selector)
        if not el:
            return CheckpointResult(
                checkpoint_name=spec.name,
                status="CHECK_FAIL",
                details=f"Element not found: {selector}",
                evidence_ref=evidence_ref,
            )
        visible = await el.is_visible()
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_PASS" if visible else "CHECK_FAIL",
            details=f"Element visible={visible}: {selector}",
            evidence_ref=evidence_ref,
        )

    async def _check_element_not_visible(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Element must NOT be visible on the page."""
        selector = ctx.resolve_var(spec.selector)
        el = await ctx.page.query_selector(selector)
        visible = bool(el) and await el.is_visible()
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_FAIL" if visible else "CHECK_PASS",
            details=f"Element should not be visible: {selector}" if visible else f"Element not visible (OK): {selector}",
            evidence_ref=evidence_ref,
        )

    async def _check_element_text_contains(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Element text must contain the expected value."""
        selector = ctx.resolve_var(spec.selector)
        el = await ctx.page.query_selector(selector)
        if not el:
            return CheckpointResult(
                checkpoint_name=spec.name,
                status="CHECK_FAIL",
                details=f"Element not found: {selector}",
                evidence_ref=evidence_ref,
            )
        text = (await el.inner_text()).strip()
        matched = spec.expect in text
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_PASS" if matched else "CHECK_FAIL",
            details=f"Element text '{spec.expect}' {'found' if matched else 'NOT found'} in: {text[:80]}",
            evidence_ref=evidence_ref,
        )

    async def _check_url_contains(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Current page URL must contain the target string."""
        url = ctx.page.url
        matched = spec.target in url
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_PASS" if matched else "CHECK_FAIL",
            details=f"URL {'contains' if matched else 'does NOT contain'}: {spec.target} (actual: {url})",
            evidence_ref=evidence_ref,
        )

    # ------------------------------------------------------------------
    # BMC / SSH shared text-based checkpoint handlers
    # ------------------------------------------------------------------

    async def _get_text_content(self, spec, ctx: RuleContext) -> str:
        """Resolve the text content to check: from page body or SSH output."""
        if ctx.page is not None:
            # BMC mode: get full page text
            return (await ctx.page.inner_text("body")).strip()
        elif ctx.text_output:
            # SSH mode: use accumulated text output
            return ctx.text_output.strip()
        return ""

    async def _check_text_contains(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Text content must contain the target string."""
        text = await self._get_text_content(spec, ctx)
        resolved_target = ctx.resolve_var(spec.target)
        matched = resolved_target in text
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_PASS" if matched else "CHECK_FAIL",
            details=f"Text {'contains' if matched else 'does NOT contain'}: {resolved_target}",
            evidence_ref=evidence_ref,
        )

    async def _check_text_not_contains(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Text content must NOT contain the target string."""
        text = await self._get_text_content(spec, ctx)
        resolved_target = ctx.resolve_var(spec.target)
        matched = resolved_target in text
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_FAIL" if matched else "CHECK_PASS",
            details=f"Text should NOT contain: {resolved_target}" if matched else f"Text does NOT contain (OK): {resolved_target}",
            evidence_ref=evidence_ref,
        )

    async def _check_regex_match(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Text content must match the regex pattern."""
        text = await self._get_text_content(spec, ctx)
        resolved_target = ctx.resolve_var(spec.target)
        match = re.search(resolved_target, text)
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_PASS" if match else "CHECK_FAIL",
            details=f"Regex {'matched' if match else 'did NOT match'}: {resolved_target}" +
                    (f" → {match.group(0)!r}" if match else ""),
            evidence_ref=evidence_ref,
        )

    async def _check_regex_not_match(self, spec, ctx: RuleContext, evidence_ref) -> CheckpointResult:
        """Text content must NOT match the regex pattern."""
        text = await self._get_text_content(spec, ctx)
        resolved_target = ctx.resolve_var(spec.target)
        match = re.search(resolved_target, text)
        return CheckpointResult(
            checkpoint_name=spec.name,
            status="CHECK_FAIL" if match else "CHECK_PASS",
            details=f"Regex should NOT match: {resolved_target}" if match else f"Regex does NOT match (OK): {resolved_target}",
            evidence_ref=evidence_ref,
        )


def _checkpoint_is_non_blocking(spec: CheckpointSpec) -> bool:
    severity = str(getattr(spec, "severity", "") or "").upper()
    priority = str(getattr(spec, "priority", "") or "").upper()
    effect = str(getattr(spec, "effect_on_final", "") or "").lower()
    return (
        severity in {"WARNING", "WARN", "INFO"}
        or priority == "P2"
        or effect in {"partial", "warning", "none"}
    )
