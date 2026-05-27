"""
Playwright browser lifecycle manager.
Each worker thread gets its own Playwright + browser instance (thread-local).
No cross-thread sharing — eliminates event-loop deadlocks entirely.
"""

from __future__ import annotations
import asyncio
import logging
import threading
import time

logger = logging.getLogger("bmc_auto_capture.browser")


class BrowserManager:
    """Thread-local browser pool — each worker thread owns its own browser.

    ThreadPoolExecutor reuses threads, so a thread's browser lives across
    multiple sequential BMC tasks on the same thread.  Recycling is per-thread
    based on task count and age thresholds.
    """

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
        self._tls: dict[int, _ThreadLocalBrowser] = {}
        self._tls_lock = threading.Lock()

    @property
    def headless(self) -> bool:
        return self._headless

    @headless.setter
    def headless(self, value: bool):
        self._headless = value

    async def get_context(self):
        """Return a browser context for the current thread.

        Creates a new Playwright + browser + context on first call per thread.
        Recycles the browser after max_tasks or max_age_seconds.
        """
        tid = threading.get_ident()

        with self._tls_lock:
            tb = self._tls.get(tid)

        if tb is None:
            tb = _ThreadLocalBrowser(
                headless=self._headless,
                max_tasks=self._max_tasks,
                max_age_seconds=self._max_age,
                viewport=self._viewport,
            )
            with self._tls_lock:
                self._tls[tid] = tb

        return await tb.get_context()

    def reset_thread(self):
        """Force current thread's browser to be recreated on next get_context()."""
        tid = threading.get_ident()
        with self._tls_lock:
            tb = self._tls.pop(tid, None)
        if tb is not None:
            logger.warning("Resetting browser for thread %d after failure", tid)

    async def teardown(self):
        """Close all thread-local browsers. Called from main thread on shutdown."""
        with self._tls_lock:
            tbs = list(self._tls.values())
            self._tls.clear()

        for tb in tbs:
            try:
                await tb.close()
            except Exception:
                pass


class _ThreadLocalBrowser:
    """Per-thread Playwright browser with recycling."""

    def __init__(self, headless, max_tasks, max_age_seconds, viewport):
        self._headless = headless
        self._max_tasks = max_tasks
        self._max_age = max_age_seconds
        self._viewport = viewport
        self._playwright = None
        self._browser = None
        self._context = None
        self._task_count = 0
        self._born_at: float = 0.0

    async def get_context(self):
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()

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

    async def close(self):
        await self._teardown_browser()
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _should_recycle(self) -> bool:
        if self._browser is None:
            return False
        if self._task_count >= self._max_tasks:
            logger.info("Browser recycling: reached %d tasks", self._task_count)
            return True
        if time.time() - self._born_at >= self._max_age:
            logger.info("Browser recycling: age exceeded limit")
            return True
        return False

    async def _teardown_browser(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._task_count = 0
