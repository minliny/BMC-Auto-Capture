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
            logger.warning("重置线程浏览器 %d after failure", tid)

    def close_current_thread_browser(self):
        """Close the current thread's browser on its own event loop.

        Called from a BMC worker thread BEFORE pool shutdown.
        This avoids cross-loop deadlocks that happen when the main thread
        tries to close browsers via asyncio.run().
        """
        import asyncio as asyncio_mod
        tid = threading.get_ident()
        with self._tls_lock:
            tb = self._tls.pop(tid, None)
        if tb is None:
            return
        # Get the persistent event loop for this thread
        loop = _get_thread_loop()
        if loop.is_closed():
            tb._drop_refs()  # Can't close, just drop
            return
        try:
            loop.run_until_complete(
                asyncio_mod.wait_for(tb.close(), timeout=15)
            )
        except Exception:
            pass

    async def teardown(self):
        """Drop any remaining browser refs. Called from main thread on shutdown.

        Browser cleanup should already have happened on worker threads
        via close_current_thread_browser(). This is a safety net only —
        it drops refs WITHOUT awaiting close (would deadlock on wrong loop).
        """
        with self._tls_lock:
            tbs = list(self._tls.values())
            self._tls.clear()

        for tb in tbs:
            logger.warning("浏览器在清理时仍注册 — 丢弃引用")


# Thread-local persistent event loops (shared with BMCExecutor)
_thread_loops: dict[int, "asyncio.AbstractEventLoop"] = {}
_thread_loops_lock = threading.Lock()


def _get_thread_loop() -> "asyncio.AbstractEventLoop":
    tid = threading.get_ident()
    with _thread_loops_lock:
        loop = _thread_loops.get(tid)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _thread_loops[tid] = loop
        return loop


def _cleanup_thread_loop():
    """Close and remove the event loop for the current thread."""
    tid = threading.get_ident()
    with _thread_loops_lock:
        loop = _thread_loops.pop(tid, None)
    if loop is not None and not loop.is_closed():
        try:
            loop.close()
        except Exception:
            pass


class _ThreadLocalBrowser:
    """Per-thread Playwright browser with recycling.

    Detects event-loop changes: BMCExecutor creates a new asyncio loop
    per task.  When the loop changes, old Playwright objects are invalid
    on the new loop and must be recreated.
    """

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
        self._loop_id: int = 0

    async def get_context(self):
        from playwright.async_api import async_playwright

        current_loop = id(asyncio.get_event_loop())

        # Event loop changed → old Playwright objects belong to a dead loop.
        # Drop refs WITHOUT awaiting close (would hang on wrong loop).
        if current_loop != self._loop_id and self._playwright is not None:
            logger.debug("事件循环变化, 重建浏览器 for thread %d",
                         __import__("threading").get_ident())
            self._drop_refs()

        if self._playwright is None:
            self._playwright = await async_playwright().start()
            self._loop_id = current_loop

        if self._should_recycle():
            await self._teardown_browser()

        if self._browser is None:
            logger.info("启动浏览器 (无头模式=%s)", self._headless)
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

    def _drop_refs(self):
        """Drop references without awaiting close on a potentially dead loop."""
        self._playwright = None
        self._browser = None
        self._context = None
        self._loop_id = 0

    async def close(self):
        try:
            await asyncio.wait_for(self._teardown_browser(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("浏览器清理超时, 丢弃引用")
        except Exception:
            pass
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=10)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            self._playwright = None

    def _should_recycle(self) -> bool:
        if self._browser is None:
            return False
        if self._task_count >= self._max_tasks:
            logger.info("浏览器回收: 已达 %d tasks", self._task_count)
            return True
        if time.time() - self._born_at >= self._max_age:
            logger.info("浏览器回收: 超时 exceeded limit")
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
