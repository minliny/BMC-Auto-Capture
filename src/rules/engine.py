"""
Legacy Rule Engine — evaluates basic and advanced rules against a RuleContext.

This engine is kept for `rules` / `rules_json` compatibility. New audit rule
authoring must use RulePack JSON; skills should not generate this format.

Basic rules define evidence collection (screenshot, save HTML, save TXT).
  Failure = execution failure.

Advanced rules are post-capture validation (assertions, extra interactions).
  Failure sets rule_status=FAIL but does NOT change execution_status.
"""


from __future__ import annotations
import logging
from dataclasses import dataclass, field

from ..models.task import Rule, RuleAction
from .registry import get as get_handler

logger = logging.getLogger("bmc_auto_capture.rules")


@dataclass
class ActionResult:
    action_type: str
    status: str  # "PASS" | "FAIL"
    message: str = ""


@dataclass
class RuleEvaluationResult:
    basic_results: list[ActionResult] = field(default_factory=list)
    advanced_results: list[ActionResult] = field(default_factory=list)

    @property
    def basic_passed(self) -> bool:
        return all(r.status == "PASS" for r in self.basic_results)

    @property
    def advanced_passed(self) -> bool:
        if not self.advanced_results:
            return True  # No advanced rules = skip
        return all(r.status == "PASS" for r in self.advanced_results)


class RuleContext:
    """Execution context passed to rule action handlers."""

    def __init__(self, page=None, ssh_session=None, device=None, task=None, output_dir: str = ""):
        self.page = page
        self.ssh_session = ssh_session
        self.device = device
        self.task = task
        self.output_dir = output_dir
        self.screenshots: list[str] = []
        self.html_file: str = ""
        self.txt_file: str = ""
        self.text_output: str = ""
        # Runtime variables extracted during execution_flow (shared across steps)
        self.variables: dict[str, str] = {}
        # Artifact paths available to checkpoints (screenshot, html, txt)
        self.artifacts: dict[str, str] = {}

    def resolve_path(self, filename: str) -> str:
        from ..utils.path_safety import resolve_under_output_root

        return resolve_under_output_root(self.output_dir, filename)

    def resolve_var(self, template: str) -> str:
        """Replace {{var.X}} placeholders with extracted variable values."""
        import re
        def _replace(m):
            key = m.group(1)
            return self.variables.get(key, m.group(0))
        return re.sub(r'\{\{var\.(\w+)\}\}', _replace, template)

    def add_screenshot(self, path: str):
        self.screenshots.append(path)


class RuleEngine:
    """Evaluate rules: basic first (blocking), advanced second (non-blocking)."""

    async def evaluate(self, rules: list[Rule], context: RuleContext) -> RuleEvaluationResult:
        result = RuleEvaluationResult()

        for rule in rules:
            if not rule.enabled:
                continue

            target = result.basic_results if rule.rule_type == "basic" else result.advanced_results

            for action in rule.actions:
                try:
                    handler_cls = get_handler(action.action_type)
                    handler = handler_cls()
                    await handler.execute(action, context)
                    target.append(ActionResult(action.action_type, "PASS"))
                except Exception as e:
                    target.append(ActionResult(action.action_type, "FAIL", str(e)))
                    logger.warning(
                        "Rule '%s' action '%s' failed: %s",
                        rule.rule_name, action.action_type, e,
                    )
                    if rule.rule_type == "basic":
                        return result  # Stop on first basic failure
                    break  # Stop this advanced rule, continue next

        return result
