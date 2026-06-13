"""
Built-in rule action handlers.
"""

import asyncio
import logging

from .registry import RuleActionHandler, register

logger = logging.getLogger("bmc_auto_capture.rules")


class ScreenshotHandler(RuleActionHandler):
    action_type = "screenshot"

    async def execute(self, action, context) -> None:
        path = context.resolve_path(action.value or "screenshot.png")
        await context.page.screenshot(path=path, full_page=True)
        context.add_screenshot(path)
        logger.debug("Screenshot saved: %s", path)


class SaveHtmlHandler(RuleActionHandler):
    action_type = "save_html"

    async def execute(self, action, context) -> None:
        from ..utils.html_redaction import capture_redacted_html

        html = await capture_redacted_html(context.page)
        path = context.resolve_path(action.value or "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        context.html_file = path
        logger.debug("HTML saved: %s", path)


class SaveTxtHandler(RuleActionHandler):
    action_type = "save_txt"

    async def execute(self, action, context) -> None:
        text = action.value or getattr(context, "text_output", "")
        path = context.resolve_path("output.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        context.txt_file = path
        logger.debug("TXT saved: %s", path)


class OpenPageHandler(RuleActionHandler):
    action_type = "open_page"

    async def execute(self, action, context) -> None:
        url = action.value
        timeout = action.timeout_seconds * 1000
        await context.page.goto(url, wait_until="networkidle", timeout=timeout)


class ClickHandler(RuleActionHandler):
    action_type = "click"

    async def execute(self, action, context) -> None:
        timeout = action.timeout_seconds * 1000
        await context.page.click(action.selector, timeout=timeout)


class FillHandler(RuleActionHandler):
    action_type = "fill"

    async def execute(self, action, context) -> None:
        timeout = action.timeout_seconds * 1000
        await context.page.fill(action.selector, action.value, timeout=timeout)


class WaitForHandler(RuleActionHandler):
    action_type = "wait_for"

    async def execute(self, action, context) -> None:
        timeout = action.timeout_seconds * 1000
        await context.page.wait_for_selector(action.selector, timeout=timeout)


class WaitMillisHandler(RuleActionHandler):
    action_type = "wait_millis"

    async def execute(self, action, context) -> None:
        ms = int(action.value) if action.value else 1000
        await asyncio.sleep(ms / 1000)


class AssertTextHandler(RuleActionHandler):
    action_type = "assert_text"

    async def execute(self, action, context) -> None:
        text = await context.page.inner_text("body")
        if action.value not in text:
            raise AssertionError(f"Expected text '{action.value}' not found on page")


class AssertElementHandler(RuleActionHandler):
    action_type = "assert_element"

    async def execute(self, action, context) -> None:
        el = await context.page.query_selector(action.selector)
        if not el:
            raise AssertionError(f"Element not found: {action.selector}")
        visible = await el.is_visible()
        if not visible:
            raise AssertionError(f"Element not visible: {action.selector}")


class AssertNoTextHandler(RuleActionHandler):
    """Fail if the given text IS found on the page.
    Use for: no 'down' ports, no '告警', no '异常'.
    """
    action_type = "assert_no_text"

    async def execute(self, action, context) -> None:
        text = await context.page.inner_text("body")
        if action.value in text:
            # Find the surrounding context for debugging
            idx = text.index(action.value)
            snippet = text[max(0, idx - 40):idx + len(action.value) + 40]
            raise AssertionError(
                f"Forbidden text '{action.value}' found on page near: ...{snippet}..."
            )


class AssertNoElementHandler(RuleActionHandler):
    """Fail if the given CSS selector IS found and visible.
    Use for: no alert icons, no error badges.
    """
    action_type = "assert_no_element"

    async def execute(self, action, context) -> None:
        el = await context.page.query_selector(action.selector)
        if el and await el.is_visible():
            raise AssertionError(f"Forbidden element found: {action.selector}")


class AssertElementTextHandler(RuleActionHandler):
    """Fail if element's text does not equal the expected value."""
    action_type = "assert_element_text"

    async def execute(self, action, context) -> None:
        el = await context.page.query_selector(action.selector)
        if not el:
            raise AssertionError(f"Element not found: {action.selector}")
        text = await el.inner_text()
        if text.strip() != action.value.strip():
            raise AssertionError(
                f"Element '{action.selector}' text mismatch: expected '{action.value}', got '{text.strip()}'"
            )


class ExtractCssHandler(RuleActionHandler):
    """Extract element text via CSS selector and store in context.variables."""
    action_type = "extract_css"

    async def execute(self, action, context) -> None:
        selector = context.resolve_var(action.selector)
        el = await context.page.query_selector(selector)
        if el:
            context.variables[action.value] = (await el.inner_text()).strip()
            logger.debug("Extracted CSS %s → %s", action.value, context.variables[action.value])
        else:
            logger.warning("ExtractCss: element not found: %s", selector)


class ExtractRegexHandler(RuleActionHandler):
    """Extract first regex match from context.text_output and store in context.variables."""
    action_type = "extract_regex"

    async def execute(self, action, context) -> None:
        import re
        text = context.text_output or ""
        pattern = context.resolve_var(action.selector)
        match = re.search(pattern, text)
        if match:
            context.variables[action.value] = match.group(1)
            logger.debug("Extracted regex group(1) '%s' → %s", pattern, context.variables[action.value])
        else:
            logger.warning("ExtractRegex: no match for pattern: %s", pattern)


class ExtractTextHandler(RuleActionHandler):
    """Extract all text matching a substring from context.text_output."""
    action_type = "extract_text"

    async def execute(self, action, context) -> None:
        text = context.text_output or ""
        key = context.resolve_var(action.selector)
        idx = text.find(key)
        if idx >= 0:
            # Extract surrounding context (50 chars each side)
            start = max(0, idx - 50)
            end = min(len(text), idx + len(key) + 50)
            context.variables[action.value] = text[start:end].strip()
            logger.debug("Extracted text at index %d → %s", idx, action.value)
        else:
            logger.warning("ExtractText: substring not found: %s", key)


# Register all built-in handlers
def _register_all():
    for cls in [
        ScreenshotHandler,
        SaveHtmlHandler,
        SaveTxtHandler,
        OpenPageHandler,
        ClickHandler,
        FillHandler,
        WaitForHandler,
        WaitMillisHandler,
        AssertTextHandler,
        AssertElementHandler,
        AssertNoTextHandler,
        AssertNoElementHandler,
        AssertElementTextHandler,
        ExtractCssHandler,
        ExtractRegexHandler,
        ExtractTextHandler,
    ]:
        register(cls.action_type, cls)


_register_all()
