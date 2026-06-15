"""
BMC Generic Page Lifecycle Gate — 5-stage universal page health validation.

Gate sequence: OPENED → AUTHENTICATED → PAGE_BASIC_HEALTH → READY_FOR_CAPTURE → SCREENSHOT_VALIDATED

Distinguishes visible vs hidden loading/error elements.
Hidden templates (Angular/Vue ng-hide, display:none) do NOT fail the gate.
Only visible overlays or missing page content trigger FAIL.

Task-specific rules (RAID/CPU/fan content) are NOT implemented here — out of scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bmc_auto_capture.gate")


# ======================================================================
# Data structures
# ======================================================================

@dataclass
class BMCPageGateResult:
    """Result from a single gate check."""
    ok: bool
    gate: str  # OPENED | AUTHENTICATED | PAGE_BASIC_HEALTH | READY_FOR_CAPTURE | SCREENSHOT_VALIDATED
    reason: str = ""
    severity: str = "PASS"  # PASS | WARN | FAIL
    stage: str = ""
    task_name: str = ""
    endpoint_key: str = ""
    url: str = ""
    title: str = ""
    matched_selector: str = ""
    matched_text: str = ""
    is_visible: bool = False
    html_length: int = 0
    visible_loading_count: int = 0
    hidden_loading_count: int = 0
    visible_error_count: int = 0
    hidden_error_count: int = 0
    screenshot_path: str = ""
    evidence_html_path: str = ""
    state_json_path: str = ""
    dom_stability_samples: list[int] = field(default_factory=list)
    popup_state: str = ""
    frame_count: int = 0
    checked_at: float = 0.0
    debug: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "gate": self.gate, "reason": self.reason,
            "severity": self.severity, "stage": self.stage,
            "task_name": self.task_name, "endpoint_key": self.endpoint_key,
            "url": self.url, "title": self.title,
            "matched_selector": self.matched_selector,
            "matched_text": self.matched_text,
            "is_visible": self.is_visible,
            "html_length": self.html_length,
            "visible_loading_count": self.visible_loading_count,
            "hidden_loading_count": self.hidden_loading_count,
            "visible_error_count": self.visible_error_count,
            "hidden_error_count": self.hidden_error_count,
            "screenshot_path": self.screenshot_path,
            "evidence_html_path": self.evidence_html_path,
            "state_json_path": self.state_json_path,
            "dom_stability_samples": self.dom_stability_samples,
            "popup_state": self.popup_state,
            "frame_count": self.frame_count,
            "checked_at": self.checked_at,
            "debug": self.debug,
        }


@dataclass
class PageState:
    """Snapshot of page state for gate evaluation."""
    url: str = ""
    title: str = ""
    html: str = ""
    html_length: int = 0
    frame_count: int = 0

    # Counts from JS evaluation
    visible_loading_count: int = 0
    hidden_loading_count: int = 0
    visible_error_count: int = 0
    hidden_error_count: int = 0

    # Detailed element info
    visible_loading_els: list[dict] = field(default_factory=list)
    hidden_loading_els: list[dict] = field(default_factory=list)
    visible_error_els: list[dict] = field(default_factory=list)
    hidden_error_els: list[dict] = field(default_factory=list)

    # Popup/overlay state
    popup_state: str = ""  # none | welcome | password_expired | account_conflict | session_expired
    has_fullscreen_overlay: bool = False
    overlay_details: list[dict] = field(default_factory=list)


# ======================================================================
# Keyword sets (unified)
# ======================================================================

ACCOUNT_CONFLICT_KEYWORDS = [
    "账号已在别处登录", "账户已在其他地方登录", "已在其他地方登录",
    "account already logged in", "already logged in elsewhere",
    "session conflict", "会话冲突", "该用户已登录", "用户已在线",
    "您已被迫下线", "您的账号在另一台设备登录", "someone else has logged in",
]

SESSION_EXPIRED_KEYWORDS = [
    "session expired", "会话已过期", "登录已过期", "login expired",
    "token invalid", "unauthorized", "未授权",
    "please login", "请重新登录", "重新登录", "re-login",
    "登录超时", "会话超时", "login timeout", "session timeout",
]

PASSWORD_EXPIRED_KEYWORDS = [
    "password expired", "密码已过期", "密码过期", "password expiring",
    "修改密码", "change password", "您的账号存在安全风险",
]

LOGIN_PAGE_SELECTORS = [
    'input[name="username"]', 'input[id="username"]', 'input[name="account"]',
    'input[type="password"]', '#login-container', '#login-input', '#btLogin',
    '.login-form', '.login_box',
]

LOGIN_PAGE_URL_PATTERNS = ['/login', '/Login', 'login.jsp', 'login.php']

LOADING_SELECTORS = [
    '.loading', '.spinner', '.skeleton', '[class*="loading"]',
    '[class*="spinner"]', '[class*="skeleton"]', '.el-loading-mask',
    '.v-loading', '.nprogress', '.loader', '.waiting',
    '[role="progressbar"]', '.progress-bar',
]

ERROR_SELECTORS = [
    '.error', '.alert-danger', '.alert-error', '[class*="error-message"]',
    '.exception', '.fail', '.failure',
    '.network-error', '.connection-error',
]

GENERIC_ERROR_KEYWORDS = [
    "无法访问此网站", "无法访问", "connection refused", "connection closed",
    "timeout", "超时", "timed out", "not found", "404", "500", "502", "503",
    "internal server error", "service unavailable",
]

SESSION_RECOVERY_STATUSES = (
    "BMC_SESSION_EXPIRED",
    "BMC_TIMEOUT_DIALOG",
    "BMC_SESSION_PREEMPTED",
    "BMC_LOGIN_PAGE_RETURNED",
    "BMC_LOGIN_FORM_STILL_VISIBLE",
)

PAGE_RECOVERABLE_STATUSES = (
    "BMC_PAGE_STILL_LOADING_VISIBLE",
    "BMC_PAGE_EMPTY",
    "BMC_EMPTY_DOM",
    "BMC_DOM_NOT_STABLE",
)


def is_session_recoverable_status(status: str) -> bool:
    raw = str(status or "")
    return any(raw.startswith(prefix) for prefix in SESSION_RECOVERY_STATUSES)


def is_page_recoverable_status(status: str) -> bool:
    raw = str(status or "")
    return any(raw.startswith(prefix) for prefix in PAGE_RECOVERABLE_STATUSES)


def is_recoverable_health_status(status: str) -> bool:
    return is_session_recoverable_status(status) or is_page_recoverable_status(status)

# ======================================================================
# JS snippet for element inspection
# ======================================================================

_PAGE_STATE_JS = """
() => {
    const result = {
        frame_count: window.frames ? window.frames.length : 0,
        visible_loading_count: 0, hidden_loading_count: 0,
        visible_error_count: 0, hidden_error_count: 0,
        visible_loading: [], hidden_loading: [],
        visible_error: [], hidden_error: [],
        has_fullscreen_overlay: false,
        overlay_details: [],
    };

    // Selectors to check
    const loadingSels = ['.loading', '.spinner', '.skeleton', '[class*="loading"]',
        '[class*="spinner"]', '[class*="skeleton"]', '.el-loading-mask',
        '.v-loading', '.nprogress', '.loader', '.progress-bar'];
    const errorSels = ['.error', '.alert-danger', '.alert-error',
        '[class*="error-message"]', '.exception', '.fail', '.network-error',
        '.connection-error'];

    // Check all elements matching selectors
    const checkEls = (sels, isError) => {
        const found = [];
        for (const sel of sels) {
            try {
                document.querySelectorAll(sel).forEach(el => {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const isVisible = (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        rect.width > 0 && rect.height > 0
                    );
                    const text = (el.textContent || el.innerText || '').trim().substring(0, 200);
                    const info = {
                        selector: sel,
                        tag: el.tagName.toLowerCase(),
                        class: (el.className || '').toString().substring(0, 80),
                        text: text,
                        visible: isVisible,
                        area: Math.round(rect.width * rect.height),
                    };
                    found.push(info);
                    if (isVisible && info.area > 1000) {
                        if (isError) {
                            result.visible_error_count++;
                            result.visible_error.push(info);
                        } else {
                            result.visible_loading_count++;
                            result.visible_loading.push(info);
                        }
                    } else if (!isVisible) {
                        if (isError) {
                            result.hidden_error_count++;
                            result.hidden_error.push(info);
                        } else {
                            result.hidden_loading_count++;
                            result.hidden_loading.push(info);
                        }
                    }
                });
            } catch(e) {}
        }
    };

    checkEls(loadingSels, false);
    checkEls(errorSels, true);

    // Check for full-screen overlay / modal
    const overlays = document.querySelectorAll(
        '.modal, .modal-mask, .el-overlay, .el-dialog__wrapper, ' +
        '[class*="overlay"], [class*="mask"], [style*="position: fixed"]'
    );
    overlays.forEach(el => {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        if (style.display !== 'none' && rect.width > 100 && rect.height > 100) {
            result.has_fullscreen_overlay = true;
            result.overlay_details.push({
                tag: el.tagName.toLowerCase(),
                class: (el.className || '').toString().substring(0, 80),
                text: (el.textContent || '').trim().substring(0, 200),
                area: Math.round(rect.width * rect.height),
            });
        }
    });

    return result;
}
"""


# ======================================================================
# Gate functions
# ======================================================================

_GATE_TIMEOUT_SEC = 15  # max wait for a gate to resolve


async def _collect_page_state(page) -> PageState:
    """Collect current page state for gate evaluation."""
    s = PageState()
    try:
        s.url = page.url
        s.title = await page.title()
        s.html = await page.content()
        s.html_length = len(s.html)
    except Exception:
        pass

    try:
        js_result = await page.evaluate(_PAGE_STATE_JS)
        s.frame_count = js_result.get("frame_count", 0)
        s.visible_loading_count = js_result.get("visible_loading_count", 0)
        s.hidden_loading_count = js_result.get("hidden_loading_count", 0)
        s.visible_error_count = js_result.get("visible_error_count", 0)
        s.hidden_error_count = js_result.get("hidden_error_count", 0)
        s.visible_loading_els = js_result.get("visible_loading", [])
        s.hidden_loading_els = js_result.get("hidden_loading", [])
        s.visible_error_els = js_result.get("visible_error", [])
        s.hidden_error_els = js_result.get("hidden_error", [])
        s.has_fullscreen_overlay = js_result.get("has_fullscreen_overlay", False)
        s.overlay_details = js_result.get("overlay_details", [])
    except Exception:
        pass

    return s


def _make_result(gate: str, ok: bool, severity: str, reason: str,
                 state: PageState | None = None, **kw) -> BMCPageGateResult:
    r = BMCPageGateResult(ok=ok, gate=gate, severity=severity, reason=reason,
                          checked_at=time.time(), **kw)
    if state:
        r.url = state.url
        r.title = state.title
        r.html_length = state.html_length
        r.frame_count = state.frame_count
        r.visible_loading_count = state.visible_loading_count
        r.hidden_loading_count = state.hidden_loading_count
        r.visible_error_count = state.visible_error_count
        r.hidden_error_count = state.hidden_error_count
        r.popup_state = state.popup_state
        if state.visible_loading_els:
            el = state.visible_loading_els[0]
            r.matched_selector = el.get("selector", "")
            r.matched_text = el.get("text", "")
            r.is_visible = True
        if state.visible_error_els:
            el = state.visible_error_els[0]
            r.matched_selector = r.matched_selector or el.get("selector", "")
            r.matched_text = r.matched_text or el.get("text", "")
            r.is_visible = True
    return r


# ------------------------------------------------------------------
# Gate 1: OPENED
# ------------------------------------------------------------------

async def check_opened(page, target_url: str = "") -> BMCPageGateResult:
    """Verify page opened successfully."""
    state = await _collect_page_state(page)

    # Empty DOM
    if state.html_length < 1000:
        return _make_result("OPENED", False, "FAIL",
                            f"BMC_EMPTY_DOM: HTML length {state.html_length} < 1000",
                            state)

    # about:blank
    if not state.url or state.url in ("about:blank", "about:"):
        return _make_result("OPENED", False, "FAIL",
                            f"BMC_BROWSER_ERROR_PAGE: url={state.url}", state)

    # Browser error page
    html_lower = state.html.lower()
    if any(kw in html_lower for kw in [
        "err_connection_closed", "err_timed_out", "err_connection_refused",
        "err_cert", "err_ssl", "err_name_not_resolved",
        "this site can't be reached", "无法访问此网站",
    ]):
        return _make_result("OPENED", False, "FAIL",
                            "BMC_BROWSER_ERROR_PAGE: connection error page detected", state)

    # HTTP error page
    if any(kw in html_lower for kw in ["404 not found", "500 internal server",
                                        "502 bad gateway", "503 service"]):
        return _make_result("OPENED", False, "FAIL",
                            "BMC_HTTP_ERROR: HTTP error page detected", state)

    return _make_result("OPENED", True, "PASS",
                        f"Page opened OK ({state.html_length}B)", state)


# ------------------------------------------------------------------
# Gate 2: AUTHENTICATED
# ------------------------------------------------------------------

async def check_authenticated(page) -> BMCPageGateResult:
    """Verify user is authenticated (not on login page, no auth errors)."""
    state = await _collect_page_state(page)
    html_lower = state.html.lower()

    # Account conflict → must FAIL (cannot dismiss)
    for kw in ACCOUNT_CONFLICT_KEYWORDS:
        if kw.lower() in html_lower:
            return _make_result("AUTHENTICATED", False, "FAIL",
                                f"BMC_ACCOUNT_LOGGED_IN_ELSEWHERE: '{kw}'", state)

    # Session expired → FAIL
    for kw in SESSION_EXPIRED_KEYWORDS:
        if kw.lower() in html_lower:
            return _make_result("AUTHENTICATED", False, "FAIL",
                                f"BMC_SESSION_EXPIRED: '{kw}'", state)

    if "custom-dialog timeout" in html_lower:
        return _make_result("AUTHENTICATED", False, "FAIL",
                            "BMC_TIMEOUT_DIALOG: custom-dialog timeout", state)

    # Password expired → FAIL (cannot proceed)
    for kw in PASSWORD_EXPIRED_KEYWORDS:
        if kw.lower() in html_lower:
            return _make_result("AUTHENTICATED", False, "FAIL",
                                f"BMC_PASSWORD_EXPIRED: '{kw}'", state)

    # Login page URL
    if any(p in state.url.lower() for p in LOGIN_PAGE_URL_PATTERNS):
        # Check if login form is visible
        form_visible = False
        for sel in LOGIN_PAGE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    form_visible = True
                    break
            except Exception:
                pass
        if form_visible:
            return _make_result("AUTHENTICATED", False, "FAIL",
                                "BMC_LOGIN_FORM_STILL_VISIBLE: login form visible", state)

    # Login page text without form → WARN only
    login_texts = ["用户登录", "帐号登录", "账号登录", "user login", "sign in"]
    login_text_found = [t for t in login_texts if t.lower() in html_lower]
    if len(login_text_found) >= 2:
        return _make_result("AUTHENTICATED", False, "FAIL",
                            f"BMC_LOGIN_PAGE_RETURNED: {login_text_found}", state)

    return _make_result("AUTHENTICATED", True, "PASS",
                        "Authenticated OK", state)


# ------------------------------------------------------------------
# Gate 3: PAGE_BASIC_HEALTH
# ------------------------------------------------------------------

async def check_page_basic_health(page) -> BMCPageGateResult:
    """Verify page is healthy — no visible errors, not login, has content."""
    state = await _collect_page_state(page)
    html_lower = state.html.lower()

    # Check: still login page? (re-check in case session expired after login)
    for kw in ACCOUNT_CONFLICT_KEYWORDS:
        if kw.lower() in html_lower:
            return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                                f"BMC_ACCOUNT_LOGGED_IN_ELSEWHERE: '{kw}'", state)

    for kw in SESSION_EXPIRED_KEYWORDS:
        if kw.lower() in html_lower:
            return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                                f"BMC_SESSION_EXPIRED: '{kw}'", state)

    if "custom-dialog timeout" in html_lower:
        return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                            "BMC_TIMEOUT_DIALOG: custom-dialog timeout", state)

    # Empty page?
    if state.html_length < 500:
        return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                            f"BMC_PAGE_EMPTY: {state.html_length}B", state)

    # Visible error overlay → FAIL
    if state.visible_error_count > 0 and state.has_fullscreen_overlay:
        return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                            "BMC_GLOBAL_ERROR_VISIBLE: error overlay covering page", state)

    # Visible error (not full-screen) → FAIL
    if state.visible_error_count > 0:
        el = state.visible_error_els[0]
        return _make_result("PAGE_BASIC_HEALTH", False, "FAIL",
                            f"BMC_PAGE_ERROR_VISIBLE: {el.get('text', '')[:80]}", state)

    # Hidden error only → WARN, not FAIL
    if state.hidden_error_count > 0:
        return _make_result("PAGE_BASIC_HEALTH", True, "WARN",
                            f"BMC_PAGE_ERROR_HIDDEN_ONLY: {state.hidden_error_count} hidden error(s)",
                            state)

    return _make_result("PAGE_BASIC_HEALTH", True, "PASS",
                        f"Page healthy ({state.html_length}B, "
                        f"visible_err={state.visible_error_count}, hidden_err={state.hidden_error_count})",
                        state)


# ------------------------------------------------------------------
# Gate 4: READY_FOR_CAPTURE
# ------------------------------------------------------------------

async def check_ready_for_capture(page, max_wait: float = 10.0) -> BMCPageGateResult:
    """Wait for page to stabilize, then verify ready for screenshot."""
    deadline = time.time() + max_wait
    samples: list[PageState] = []
    last_len = 0
    stable_count = 0

    while time.time() < deadline:
        state = await _collect_page_state(page)
        samples.append(state)

        # Visible loading → wait
        if state.visible_loading_count > 0:
            stable_count = 0
            await asyncio.sleep(0.5)
            continue

        # Visible error → FAIL immediately
        if state.visible_error_count > 0:
            el = state.visible_error_els[0]
            return _make_result("READY_FOR_CAPTURE", False, "FAIL",
                                f"BMC_PAGE_ERROR_VISIBLE: {el.get('text', '')[:80]}",
                                state, dom_stability_samples=[s.html_length for s in samples])

        # Check DOM stability
        if abs(state.html_length - last_len) < 200:
            stable_count += 1
        else:
            stable_count = 0
        last_len = state.html_length

        if stable_count >= 3:
            # Stable — now check final state

            # Hidden loading only + content → PASS (WARN if >0)
            if state.hidden_loading_count > 0 and state.html_length > 1000:
                return _make_result("READY_FOR_CAPTURE", True, "WARN",
                                    f"BMC_PAGE_STILL_LOADING_HIDDEN_ONLY: "
                                    f"{state.hidden_loading_count} hidden, "
                                    f"page has {state.html_length}B content → OK",
                                    state, dom_stability_samples=[s.html_length for s in samples])

            # Full-screen overlay still present → FAIL
            if state.has_fullscreen_overlay:
                details = state.overlay_details[0] if state.overlay_details else {}
                return _make_result("READY_FOR_CAPTURE", False, "FAIL",
                                    f"BMC_OVERLAY_BLOCKING: {details.get('text', '')[:80]}",
                                    state, dom_stability_samples=[s.html_length for s in samples])

            return _make_result("READY_FOR_CAPTURE", True, "PASS",
                                f"Page stable ({len(samples)} samples, "
                                f"vl={state.visible_loading_count} hl={state.hidden_loading_count})",
                                state, dom_stability_samples=[s.html_length for s in samples])

        await asyncio.sleep(0.5)

    # Timeout — report last known state
    final = samples[-1] if samples else state
    if final.visible_loading_count > 0:
        return _make_result("READY_FOR_CAPTURE", False, "FAIL",
                            f"BMC_PAGE_STILL_LOADING_VISIBLE: {final.visible_loading_count} visible "
                            f"loading after {max_wait:.0f}s",
                            final, dom_stability_samples=[s.html_length for s in samples])
    return _make_result("READY_FOR_CAPTURE", False, "FAIL",
                        f"BMC_DOM_NOT_STABLE: {len(samples)} samples, "
                        f"last_len={final.html_length}",
                        final, dom_stability_samples=[s.html_length for s in samples])


# ------------------------------------------------------------------
# Gate 5: SCREENSHOT_VALIDATED
# ------------------------------------------------------------------

async def check_screenshot_validated(page, screenshot_path: str,
                                     html_path: str = "", mhtml_path: str = "",
                                     min_size: int = 5000,
                                     min_width: int = 100,
                                     min_height: int = 100) -> BMCPageGateResult:
    """Verify screenshot evidence is valid."""
    result = BMCPageGateResult(ok=False, gate="SCREENSHOT_VALIDATED",
                               screenshot_path=screenshot_path,
                               evidence_html_path=html_path,
                               checked_at=time.time())

    # File existence
    if not screenshot_path or not os.path.exists(screenshot_path):
        result.reason = "SCREENSHOT_MISSING"
        result.severity = "FAIL"
        return result

    # File size
    try:
        fsize = os.path.getsize(screenshot_path)
    except Exception:
        result.reason = "SCREENSHOT_MISSING: cannot stat"
        result.severity = "FAIL"
        return result

    if fsize < min_size:
        result.reason = f"SCREENSHOT_TOO_SMALL: {fsize}B < {min_size}B"
        result.severity = "FAIL"
        return result

    # Image dimensions
    try:
        from PIL import Image
        img = Image.open(screenshot_path)
        w, h = img.size
        img.close()
        if w < min_width or h < min_height:
            result.reason = f"SCREENSHOT_INVALID_SIZE: {w}x{h} < {min_width}x{min_height}"
            result.severity = "FAIL"
            return result

        # Blank/white check: sample pixels
        # Convert to RGB and check variance
        img_rgb = Image.open(screenshot_path).convert("RGB")
        pixels = list(img_rgb.getdata())
        img_rgb.close()
        # Check first 1000 pixels for variance
        sample = pixels[:1000] if len(pixels) > 1000 else pixels
        r_vals = [p[0] for p in sample]
        g_vals = [p[1] for p in sample]
        b_vals = [p[2] for p in sample]
        r_range = max(r_vals) - min(r_vals)
        g_range = max(g_vals) - min(g_vals)
        b_range = max(b_vals) - min(b_vals)
        if r_range < 5 and g_range < 5 and b_range < 5:
            result.reason = "SCREENSHOT_BLANK: uniform color"
            result.severity = "FAIL"
            return result
    except ImportError:
        pass  # PIL not available — skip pixel check
    except Exception as e:
        result.debug["image_check_error"] = str(e)

    # Page still healthy at screenshot time?
    state = await _collect_page_state(page)
    html_lower = state.html.lower()
    for kw in ACCOUNT_CONFLICT_KEYWORDS:
        if kw.lower() in html_lower:
            result.reason = f"SCREENSHOT_PAGE_NOT_HEALTHY: '{kw}'"
            result.severity = "FAIL"
            result.url = state.url
            return result

    if state.visible_error_count > 0:
        result.reason = f"SCREENSHOT_PAGE_NOT_HEALTHY: {state.visible_error_count} visible errors"
        result.severity = "FAIL"
        return result

    if state.has_fullscreen_overlay:
        result.reason = "SCREENSHOT_BLOCKED_BY_OVERLAY: full-screen overlay present"
        result.severity = "FAIL"
        return result

    result.ok = True
    result.severity = "PASS"
    result.reason = f"Screenshot valid ({fsize}B, {w}x{h})"
    return result


# ======================================================================
# Full lifecycle runner
# ======================================================================

async def run_page_lifecycle_gates(
    page,
    stage: str,
    *,
    target_url: str = "",
    screenshot_path: str = "",
    html_path: str = "",
    mhtml_path: str = "",
    wait_for_ready: bool = True,
    max_ready_wait: float = 10.0,
) -> list[BMCPageGateResult]:
    """Run all applicable gates for the current stage.

    Returns list of gate results.  Any FAIL means the page is not healthy.
    """
    results: list[BMCPageGateResult] = []

    if stage in ("after_login", "before_plan"):
        # Full lifecycle from AUTHENTICATED
        r = await check_authenticated(page)
        results.append(r)
        if not r.ok and r.severity == "FAIL":
            logger.warning("[Gate] %s FAIL: %s", stage, r.reason)
            return results

        r = await check_page_basic_health(page)
        results.append(r)
        if not r.ok and r.severity == "FAIL":
            logger.warning("[Gate] %s FAIL: %s", stage, r.reason)
            return results

    if stage in ("after_navigate", "before_screenshot", "before_actions", "after_login", "before_plan"):
        r = await check_opened(page, target_url)
        results.append(r)
        if not r.ok and r.severity == "FAIL":
            logger.warning("[Gate] %s FAIL: %s", stage, r.reason)
            return results

        r = await check_page_basic_health(page)
        results.append(r)
        if not r.ok and r.severity == "FAIL":
            logger.warning("[Gate] %s FAIL: %s", stage, r.reason)
            return results

        if wait_for_ready:
            r = await check_ready_for_capture(page, max_wait=max_ready_wait)
            results.append(r)
            if not r.ok and r.severity == "FAIL":
                logger.warning("[Gate] %s FAIL: %s", stage, r.reason)

    if stage in ("before_complete", "after_capture") and screenshot_path:
        r = await check_screenshot_validated(page, screenshot_path, html_path, mhtml_path)
        results.append(r)
        if not r.ok and r.severity == "FAIL":
            logger.warning("[Gate] %s FAIL: %s", stage, r.reason)

    return results


def save_page_health_debug(results: list[BMCPageGateResult], output_dir: str) -> str:
    """Save gate results to page_health_debug.json.  Returns path."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "page_health_debug.json")

    failed_gate = ""
    for r in results:
        if not r.ok or r.severity == "FAIL":
            failed_gate = r.gate
            break
    if not failed_gate:
        for r in results:
            if r.severity == "WARN":
                failed_gate = r.gate
                break

    data = {
        "all_gate_results": [r.to_dict() for r in results],
        "failed_gate": failed_gate,
        "reason": results[0].reason if results else "",
        "severity": max((r.severity for r in results), key=lambda s: {"PASS": 0, "WARN": 1, "FAIL": 2}.get, default="PASS"),
        "checked_at": time.time(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


# ======================================================================
# Legacy compatibility: check_bmc_page_health (thin wrapper)
# ======================================================================

class HealthResult:
    """Compatibility wrapper for new gate system."""
    __slots__ = ("stage", "healthy", "status", "matched_keyword",
                 "url", "title", "html_size", "screenshot_size", "details",
                 "recoverable", "terminal")

    def __init__(self, stage: str):
        self.stage = stage
        self.healthy = True
        self.status = "OK"
        self.matched_keyword = ""
        self.url = ""
        self.title = ""
        self.html_size = 0
        self.screenshot_size = 0
        self.details = ""
        self.recoverable = False
        self.terminal = False


async def check_bmc_page_health(page, stage: str, target_url: str = "") -> HealthResult:
    """Legacy wrapper — runs new gate system and returns simple HealthResult."""
    hr = HealthResult(stage)
    try:
        results = await run_page_lifecycle_gates(page, stage, target_url=target_url,
                                                  wait_for_ready=True, max_ready_wait=8.0)
        hr.url = results[0].url if results else ""
        hr.title = results[0].title if results else ""
        hr.html_size = results[0].html_length if results else 0

        for r in results:
            if not r.ok and r.severity == "FAIL":
                hr.healthy = False
                hr.status = r.reason.split(":")[0] if ":" in r.reason else r.reason
                hr.matched_keyword = r.matched_text[:100] if r.matched_text else r.reason[:100]
                hr.details = r.reason
                hr.recoverable = is_recoverable_health_status(hr.status)
                hr.terminal = not hr.recoverable
                return hr
            if not r.ok and r.severity == "WARN":
                hr.status = r.reason.split(":")[0] if ":" in r.reason else "WARN"
                hr.matched_keyword = r.matched_text[:100] if r.matched_text else ""
                hr.details = r.reason

        hr.healthy = True
        hr.status = "OK"
    except Exception as e:
        hr.healthy = False
        hr.status = "HEALTH_CHECK_ERROR"
        hr.details = str(e)
        hr.recoverable = False
        hr.terminal = True

    return hr


# ======================================================================
# Evidence file checks (kept from original for backwards compat)
# ======================================================================

def check_evidence_files(output_dir: str) -> dict:
    """Check evidence files exist and are non-empty."""
    result = {"screenshot_ok": False, "html_ok": False, "mhtml_ok": False,
              "log_ok": False, "screenshot_size": 0, "html_size": 0,
              "mhtml_size": 0, "log_size": 0}
    if not output_dir or not os.path.isdir(output_dir):
        return result
    for fname in os.listdir(output_dir):
        fpath = os.path.join(output_dir, fname)
        if not os.path.isfile(fpath):
            continue
        size = os.path.getsize(fpath)
        if fname.endswith(".png") and "raw" not in fname.lower():
            result["screenshot_ok"] = size > 500
            result["screenshot_size"] = max(result["screenshot_size"], size)
        elif fname.endswith(".html") and "evidence" in fname:
            result["html_ok"] = size > 200
            result["html_size"] = max(result["html_size"], size)
        elif fname.endswith(".mhtml"):
            result["mhtml_ok"] = size > 1000
            result["mhtml_size"] = size
        elif fname.endswith(".log") or fname.endswith(".txt"):
            result["log_ok"] = size > 0
            result["log_size"] = size
    html_dir = os.path.join(output_dir, "html")
    if os.path.isdir(html_dir):
        for fname in os.listdir(html_dir):
            fpath = os.path.join(html_dir, fname)
            if not os.path.isfile(fpath):
                continue
            size = os.path.getsize(fpath)
            if fname.endswith(".html"):
                result["html_ok"] = result["html_ok"] or size > 200
                result["html_size"] = max(result["html_size"], size)
            elif fname.endswith(".mhtml"):
                result["mhtml_ok"] = size > 1000
                result["mhtml_size"] = size
    return result


# HTML/MHTML keyword scan (used by evidence_audit)
def scan_html_for_keywords(html_path: str) -> dict:
    result = {"path": html_path, "healthy": True, "status": "OK",
              "matched_keyword": "", "html_size": 0}
    if not html_path or not os.path.exists(html_path):
        result["healthy"] = False
        result["status"] = "HTML_MISSING"
        return result
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        result["html_size"] = len(content)
        cl = content.lower()
        for kw in ACCOUNT_CONFLICT_KEYWORDS:
            if kw.lower() in cl:
                result["healthy"] = False
                result["status"] = "ACCOUNT_LOGGED_IN_ELSEWHERE"
                result["matched_keyword"] = kw
                return result
        for kw in SESSION_EXPIRED_KEYWORDS:
            if kw.lower() in cl:
                result["healthy"] = False
                result["status"] = "SESSION_EXPIRED"
                result["matched_keyword"] = kw
                return result
        if _is_login_page(content, ""):
            result["healthy"] = False
            result["status"] = "LOGIN_PAGE_RETURNED"
            result["matched_keyword"] = "login form detected in saved HTML"
            return result
    except Exception as e:
        result["healthy"] = False
        result["status"] = "HTML_READ_ERROR"
        result["matched_keyword"] = str(e)[:100]
    return result


def scan_mhtml_for_keywords(mhtml_path: str) -> dict:
    result = {"path": mhtml_path, "healthy": True, "status": "OK",
              "matched_keyword": "", "mhtml_size": 0}
    if not mhtml_path or not os.path.exists(mhtml_path):
        result["healthy"] = False
        result["status"] = "MHTML_MISSING"
        return result
    try:
        with open(mhtml_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        result["mhtml_size"] = len(content)
        cl = content.lower()
        for kw in ACCOUNT_CONFLICT_KEYWORDS:
            if kw.lower() in cl:
                result["healthy"] = False
                result["status"] = "ACCOUNT_LOGGED_IN_ELSEWHERE"
                result["matched_keyword"] = kw
                return result
        for kw in SESSION_EXPIRED_KEYWORDS:
            if kw.lower() in cl:
                result["healthy"] = False
                result["status"] = "SESSION_EXPIRED"
                result["matched_keyword"] = kw
                return result
    except Exception as e:
        result["healthy"] = False
        result["status"] = "MHTML_READ_ERROR"
        result["matched_keyword"] = str(e)[:100]
    return result


def _is_login_page(html: str, url: str) -> bool:
    html_lower = html.lower()
    score = 0
    if 'input type="password"' in html_lower or "input[type=\"password\"]" in html_lower:
        score += 3
    if 'name="username"' in html_lower or 'id="username"' in html_lower:
        score += 2
    if "#login" in html_lower or "login-container" in html_lower:
        score += 2
    if "id=\"btlogin\"" in html_lower:
        score += 3
    url_lower = url.lower()
    if "/login" in url_lower:
        score += 2
    login_text_count = sum(1 for t in ["用户登录", "帐号登录", "账号登录", "user login", "sign in"] if t.lower() in html_lower)
    score += login_text_count
    return score >= 4
