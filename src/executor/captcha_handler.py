"""
CAPTCHA detection and handling strategies.

Since automated CAPTCHA solving is not feasible, the strategy is:
1. Detect common CAPTCHA patterns on the page
2. If detected, take a screenshot of the CAPTCHA area
3. Pause execution and prompt the operator to solve it manually
4. Resume once the operator confirms
"""


from __future__ import annotations
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("bmc_auto_capture.captcha")

# Common CAPTCHA indicators in DOM
CAPTCHA_SELECTORS = [
    'img[src*="captcha"]',
    'img[src*="Captcha"]',
    'img[src*="verify"]',
    'img[src*="code"]',
    'input[name*="captcha"]',
    'input[name*="Captcha"]',
    'input[id*="captcha"]',
    'input[id*="Captcha"]',
    'input[placeholder*="验证码"]',
    'img[src*="verification"]',
    # Generic image verification
    '#captchaImg',
    '#verificationCode',
    '.captcha',
    '.verification-code',
]


class CaptchaDetected(Exception):
    """Raised when a CAPTCHA is found on the page."""

    def __init__(self, selector: str, screenshot_path: str = ""):
        self.selector = selector
        self.screenshot_path = screenshot_path
        super().__init__(f"CAPTCHA detected at '{selector}'")


async def detect_captcha(page) -> Optional[str]:
    """Check the current page for CAPTCHA elements.

    Returns the CSS selector of the first detected CAPTCHA element,
    or None if no CAPTCHA is found.
    """
    for sel in CAPTCHA_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                logger.warning("CAPTCHA element detected: %s", sel)
                return sel
        except Exception:
            continue
    return None


async def handle_captcha(page, output_dir: str, timeout: float = 120.0) -> bool:
    """Handle CAPTCHA with operator intervention.

    1. Screenshot the page
    2. Log a clear message asking the operator to solve it
    3. Wait for the CAPTCHA element to disappear (operator solved it)
    4. Return True if solved, False on timeout

    In automated/headless mode, this will time out and the task will fail.
    """
    captcha_sel = await detect_captcha(page)
    if not captcha_sel:
        return True  # No CAPTCHA found

    import os
    ss_path = os.path.join(output_dir, "captcha_detected.png")
    await page.screenshot(path=ss_path)

    logger.warning(
        "=" * 60 + "\n"
        "CAPTCHA detected! Screenshot saved to: %s\n"
        "Please solve the CAPTCHA manually in the browser window.\n"
        "Waiting up to %.0f seconds..." + "\n" + "=" * 60,
        ss_path,
        timeout,
    )

    # Wait for the CAPTCHA element to disappear
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = await detect_captcha(page)
        if not remaining:
            logger.info("CAPTCHA solved by operator")
            return True
        await asyncio.sleep(2)

    logger.error("CAPTCHA solving timed out after %.0fs", timeout)
    return False
