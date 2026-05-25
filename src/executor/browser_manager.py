"""
Playwright browser lifecycle manager.
Handles launch, headless/headed toggle, context creation, and automatic recycling.

Each BMC worker gets its own BrowserManager instance so a browser crash
in one worker does not affect others.
"""


from __future__ import annotations
import logging
import time
from typing import Optional

logger = logging.getLogger("bmc_auto_capture.browser")


class BrowserManager:
    """Manages a single Playwright browser instance with recycling thresholds."""

    def __init__(
        self,
        headless: bool = True,
        max_tasks: int = 50,
        max_age_seconds: int = 1800,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ):
        self._headless = headless
        self._max_tasks = max_tasks
        self._max_age = max_age_seconds
        self._viewport = {"width": viewport_width, "height": viewport_height}

        self._playwright = None
        self._browser = None
        self._context = None
        self._task_count = 0
        self._born_at: float = 0.0

    @property
    def headless(self) -> bool:
        return self._headless

    @headless.setter
    def headless(self, value: bool):
        if value != self._headless:
            self._headless = value
            # Force recycle on next get_context
            self._task_count = self._max_tasks + 1

    async def start(self):
        """Lazy init — called on first get_context()."""
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()

    async def get_context(self):
        """Return a fresh or recycled BrowserContext."""
        if self._playwright is None:
            await self.start()

        if self._should_recycle():
            await self._teardown_browser()

        if self._browser is None:
            logger.info(
                "Launching browser (headless=%s, viewport=%s)",
                self._headless,
                self._viewport,
            )
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=[
                    "--ignore-certificate-errors",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                viewport=self._viewport,
                ignore_https_errors=True,
                locale="zh-CN",
            )
            self._task_count = 0
            self._born_at = time.time()

        self._task_count += 1
        return self._context

    async def teardown(self):
        """Full teardown — call when worker shuts down."""
        await self._teardown_browser()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # ------------------------------------------------------------------
    def _should_recycle(self) -> bool:
        if self._browser is None:
            return False
        if self._task_count >= self._max_tasks:
            logger.info("Browser recycling: reached %d tasks", self._task_count)
            return True
        age = time.time() - self._born_at
        if age >= self._max_age:
            logger.info("Browser recycling: age %.0fs exceeds limit", age)
            return True
        return False

    async def _teardown_browser(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._task_count = 0
