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
        html = await context.page.content()
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
    ]:
        register(cls.action_type, cls)


_register_all()
