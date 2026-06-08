"""
BMC page health check — validates page state at critical execution stages.

Called at:
  1. After login
  2. After page navigation
  3. After pre_capture_actions
  4. Before final screenshot
  5. Before task completion (summary gate)

Detects:
  - Login page returned (session expired / login failed)
  - Account logged in elsewhere (session preempted)
  - Session expired / token invalid
  - Loading spinner still present
  - Page is blank / too short
  - Error / timeout messages
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("bmc_auto_capture.health")


# ---------------------------------------------------------------------------
# Keyword scan patterns
# ---------------------------------------------------------------------------

SESSION_PREEMPTION_KEYWORDS = [
    "账号已在别处登录",
    "账户已在其他地方登录",
    "已在其他地方登录",
    "account already logged in",
    "already logged in elsewhere",
    "session conflict",
    "session_conflict",
    "会话冲突",
    "该用户已登录",
    "用户已在线",
    "您已被迫下线",
    "您的账号在另一台设备登录",
    "someone else has logged in",
]

SESSION_EXPIRED_KEYWORDS = [
    "session expired",
    "session_expired",
    "会话已过期",
    "登录已过期",
    "login expired",
    "login_expired",
    "token invalid",
    "token_invalid",
    "unauthorized",
    "未授权",
    "please login",
    "please_login",
    "请重新登录",
    "重新登录",
    "re-login",
    "re_login",
    "登录超时",
    "login timeout",
    "login_timeout",
]

LOGIN_PAGE_INDICATORS = [
    # Login form elements
    'input[name="username"]',
    'input[name="password"]',
    '#login-container',
    '#login-input',
    '#btLogin',
    '.login-form',
    '.login_box',
    # Login page text
    "用户登录",
    "帐号登录",
    "账号登录",
    "User Login",
    "Sign In",
    "登 录",
]

LOADING_INDICATORS = [
    "loading",
    "spinner",
    "skeleton",
    "Loading",
    "请稍候",
    "正在加载",
    "waiting",
]

ERROR_INDICATORS = [
    "error",
    "Error",
    "错误",
    "failed to load",
    "timeout",
    "超时",
    "无法访问",
    "refused",
    "not found",
    "404",
    "500",
    "502",
    "503",
]


# ---------------------------------------------------------------------------
# Health check result
# ---------------------------------------------------------------------------

class HealthResult:
    """Outcome of a single page health check."""

    __slots__ = (
        "stage", "healthy", "status", "matched_keyword",
        "url", "title", "html_size", "screenshot_size",
        "details",
    )

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "healthy": self.healthy,
            "status": self.status,
            "matched_keyword": self.matched_keyword,
            "url": self.url,
            "title": self.title,
            "html_size": self.html_size,
            "screenshot_size": self.screenshot_size,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Health checker
# ---------------------------------------------------------------------------

async def check_bmc_page_health(
    page,
    stage: str,
    *,
    target_url: str = "",
    min_html_bytes: int = 200,
    min_screenshot_bytes: int = 500,
) -> HealthResult:
    """Check BMC page health at a given execution stage.

    Args:
        page: Playwright page object (must be live).
        stage: Human-readable stage name (e.g. "after_login").
        target_url: Expected target URL for this stage.
        min_html_bytes: Minimum acceptable HTML content length.
        min_screenshot_bytes: Minimum acceptable screenshot file size.

    Returns:
        HealthResult with .healthy=True only if all checks pass.
    """
    result = HealthResult(stage)

    # --- 1. Page alive? ---
    try:
        if page.is_closed():
            result.healthy = False
            result.status = "PAGE_CLOSED"
            result.details = "page is closed"
            return result
    except Exception:
        result.healthy = False
        result.status = "PAGE_ERROR"
        result.details = "page.is_closed() raised exception"
        return result

    # --- 2. Collect page state ---
    html_content = ""
    try:
        result.url = page.url
        result.title = await page.title()
        html_content = await page.content()
        result.html_size = len(html_content)
    except Exception as e:
        result.healthy = False
        result.status = "PAGE_READ_ERROR"
        result.details = f"Failed to read page state: {e}"
        return result

    # --- 3. HTML too short? ---
    if result.html_size < min_html_bytes:
        result.healthy = False
        result.status = "PAGE_TOO_SHORT"
        result.details = (
            f"HTML size {result.html_size}B < {min_html_bytes}B"
        )
        return result

    # --- 4. Keyword scan ---
    html_lower = html_content.lower()

    # 4a. Session preemption
    for kw in SESSION_PREEMPTION_KEYWORDS:
        if kw.lower() in html_lower:
            result.healthy = False
            result.status = "BMC_ACCOUNT_LOGGED_IN_ELSEWHERE"
            result.matched_keyword = kw
            result.details = f"Found '{kw}' in page HTML"
            return result

    # 4b. Session expired
    for kw in SESSION_EXPIRED_KEYWORDS:
        if kw.lower() in html_lower:
            result.healthy = False
            result.status = "BMC_SESSION_EXPIRED"
            result.matched_keyword = kw
            result.details = f"Found '{kw}' in page HTML"
            return result

    # 4c. Login page returned
    if _is_login_page(html_content, result.url):
        result.healthy = False
        result.status = "BMC_LOGIN_PAGE_RETURNED"
        result.matched_keyword = "login form elements detected"
        result.details = f"Page {result.url} appears to be a login page"
        return result

    # 4d. Loading indicators (warn but don't fail unless very long)
    loading_found = []
    for kw in LOADING_INDICATORS:
        if kw.lower() in html_lower:
            loading_found.append(kw)
    if len(loading_found) >= 2:
        result.healthy = False
        result.status = "BMC_PAGE_STILL_LOADING"
        result.matched_keyword = ", ".join(loading_found[:3])
        result.details = f"Loading indicators still present: {result.matched_keyword}"
        return result

    # 4e. Error indicators
    for kw in ERROR_INDICATORS:
        if kw.lower() in html_lower and kw.lower() != "error":
            # "error" is too generic alone; require a companion
            continue
    # Only flag explicit error messages (not generic "error" class names)
    explicit_errors = ["failed to load", "timeout", "超时", "无法访问",
                       "refused", "not found", "404", "500", "502", "503"]
    for kw in explicit_errors:
        if kw.lower() in html_lower:
            result.healthy = False
            result.status = "BMC_PAGE_ERROR"
            result.matched_keyword = kw
            result.details = f"Error indicator '{kw}' found in page"
            return result

    # --- 5. Target URL check (if specified) ---
    if target_url:
        if not _url_matches_target(result.url, target_url):
            result.status = "BMC_PAGE_WRONG_URL"
            result.details = (
                f"Expected target URL {target_url}, got {result.url}"
            )
            # This is a WARN, not a FAIL — the page might have redirected
            # legitimately.  The caller decides severity.
            result.healthy = True  # Not a hard fail

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_login_page(html: str, url: str) -> bool:
    """Heuristic: does this page look like a login page?"""
    html_lower = html.lower()
    score = 0

    # Strong signals: login form elements
    if "input[type=\"password\"]" in html_lower or 'input type="password"' in html_lower:
        score += 3
    if 'name="username"' in html_lower or 'id="username"' in html_lower:
        score += 2
    if "#login" in html_lower or "login-container" in html_lower:
        score += 2
    if "id=\"btlogin\"" in html_lower.lower():
        score += 3

    # URL signals
    url_lower = url.lower()
    if "/login" in url_lower:
        score += 2

    # Weak signals: login text
    login_text_count = 0
    for t in ["用户登录", "帐号登录", "账号登录", "user login", "sign in"]:
        if t.lower() in html_lower:
            login_text_count += 1
    score += login_text_count

    return score >= 4


def _url_matches_target(actual: str, target: str) -> bool:
    """Check if actual URL is on the same path as target."""
    from urllib.parse import urlparse
    ap = urlparse(actual)
    tp = urlparse(target)
    # Same host
    if ap.hostname != tp.hostname:
        return False
    # At least the path prefix should match
    if tp.path and not ap.path.startswith(tp.path):
        return False
    return True


def check_evidence_files(output_dir: str) -> dict[str, Any]:
    """Post-execution check: are evidence files present and non-empty?"""
    result = {
        "screenshot_ok": False,
        "html_ok": False,
        "mhtml_ok": False,
        "log_ok": False,
        "screenshot_size": 0,
        "html_size": 0,
        "mhtml_size": 0,
        "log_size": 0,
    }

    # Find evidence files in the output dir
    if not output_dir or not os.path.isdir(output_dir):
        return result

    for fname in os.listdir(output_dir):
        fpath = os.path.join(output_dir, fname)
        if not os.path.isfile(fpath):
            continue
        size = os.path.getsize(fpath)
        if fname.endswith(".png"):
            result["screenshot_ok"] = size > 500
            result["screenshot_size"] = size
        elif fname.endswith(".html") and "evidence" not in fname:
            result["html_ok"] = size > 200
            result["html_size"] = size
        elif fname.endswith(".mhtml"):
            result["mhtml_ok"] = size > 1000
            result["mhtml_size"] = size
        elif fname.endswith(".log"):
            result["log_ok"] = size > 0
            result["log_size"] = size

    # Also check html/ subdirectory
    html_dir = os.path.join(output_dir, "html")
    if os.path.isdir(html_dir):
        for fname in os.listdir(html_dir):
            fpath = os.path.join(html_dir, fname)
            if not os.path.isfile(fpath):
                continue
            size = os.path.getsize(fpath)
            if fname.endswith(".html") and "evidence" not in fname:
                result["html_ok"] = result["html_ok"] or size > 200
                result["html_size"] = max(result["html_size"], size)
            elif fname.endswith(".mhtml"):
                result["mhtml_ok"] = size > 1000
                result["mhtml_size"] = size

    return result


# ---------------------------------------------------------------------------
# Raw text scan (for post-execution audit of saved files)
# ---------------------------------------------------------------------------

def scan_html_for_keywords(html_path: str) -> dict[str, Any]:
    """Scan a saved HTML file for session/error keywords."""
    result = {
        "path": html_path,
        "healthy": True,
        "status": "OK",
        "matched_keyword": "",
        "html_size": 0,
    }
    if not html_path or not os.path.exists(html_path):
        result["healthy"] = False
        result["status"] = "HTML_MISSING"
        return result

    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        result["html_size"] = len(content)

        content_lower = content.lower()

        for kw in SESSION_PREEMPTION_KEYWORDS:
            if kw.lower() in content_lower:
                result["healthy"] = False
                result["status"] = "ACCOUNT_LOGGED_IN_ELSEWHERE"
                result["matched_keyword"] = kw
                return result

        for kw in SESSION_EXPIRED_KEYWORDS:
            if kw.lower() in content_lower:
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


def scan_mhtml_for_keywords(mhtml_path: str) -> dict[str, Any]:
    """Scan a saved MHTML file for session/error keywords."""
    result = {
        "path": mhtml_path,
        "healthy": True,
        "status": "OK",
        "matched_keyword": "",
        "mhtml_size": 0,
    }
    if not mhtml_path or not os.path.exists(mhtml_path):
        result["healthy"] = False
        result["status"] = "MHTML_MISSING"
        return result

    try:
        with open(mhtml_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        result["mhtml_size"] = len(content)

        content_lower = content.lower()

        for kw in SESSION_PREEMPTION_KEYWORDS:
            if kw.lower() in content_lower:
                result["healthy"] = False
                result["status"] = "ACCOUNT_LOGGED_IN_ELSEWHERE"
                result["matched_keyword"] = kw
                return result

        for kw in SESSION_EXPIRED_KEYWORDS:
            if kw.lower() in content_lower:
                result["healthy"] = False
                result["status"] = "SESSION_EXPIRED"
                result["matched_keyword"] = kw
                return result

    except Exception as e:
        result["healthy"] = False
        result["status"] = "MHTML_READ_ERROR"
        result["matched_keyword"] = str(e)[:100]

    return result
