"""
Playwright browser lifecycle manager.
Handles launch, headless/headed toggle, context creation, and automatic recycling.
Detects event loop changes (asyncio.run creating new loops) and resets accordingly.
"""

from __future__ import annotations
import asyncio
import logging
import time

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
        self._loop_id: int = 0

    @property
    def headless(self) -> bool:
        return self._headless

    @headless.setter
    def headless(self, value: bool):
        if value != self._headless:
            self._headless = value
            self._task_count = self._max_tasks + 1

    async def start(self):
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()

    async def get_context(self):
        """Return a fresh or recycled BrowserContext.
        Detects event loop changes and forces a full reset when needed."""
        current_loop = id(asyncio.get_event_loop())

        if current_loop != self._loop_id and self._playwright is not None:
            logger.debug("Event loop changed, recreating browser")
            await self._force_reset()

        if self._playwright is None:
            await self.start()
            self._loop_id = current_loop

        if self._should_recycle():
            await self._teardown_browser()

        if self._browser is None:
            logger.info("Launching browser (headless=%s)", self._headless)
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
        await self._force_reset()

    async def _force_reset(self):
        """Tear down everything. Handles closed-loop case by dropping refs."""
        try:
            await self._teardown_browser()
        except Exception:
            self._context = None
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._loop_id = 0

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
        except (RuntimeError, Exception):
            pass
        try:
            if self._browser:
                await self._browser.close()
        except (RuntimeError, Exception):
            pass
        self._context = None
        self._browser = None
        self._task_count = 0
