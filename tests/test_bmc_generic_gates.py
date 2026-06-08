#!/usr/bin/env python3
"""Mock tests for BMC generic page lifecycle gates (no HW, no real browser).

Tests all 5 gates: OPENED, AUTHENTICATED, PAGE_BASIC_HEALTH,
READY_FOR_CAPTURE, SCREENSHOT_VALIDATED.

Run: python tests/test_bmc_generic_gates.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILS = 0
TOTAL = 0


def check(name: str, cond: bool, detail: str = ""):
    global FAILS, TOTAL
    TOTAL += 1
    if cond:
        print(f"  OK  {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# ======================================================================
# Helpers: mock Playwright page
# ======================================================================

_BODY_HTML = "<h1>System Information</h1><table>" + "<tr><td>CPU</td><td>Intel Xeon E5-2699 v4</td></tr>" * 30 + "</table>"

def _mock_page(url="https://10.0.0.1/UI/Static/#/dashboard",
               title="iBMC Dashboard",
               html=None,
               visible_loading=0, hidden_loading=0,
               visible_error=0, hidden_error=0,
               has_overlay=False):
    """Create a mock Playwright page with controllable JS evaluation."""
    if html is None:
        html = "<html><body>" + _BODY_HTML + "</body></html>"
    page = AsyncMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.content = AsyncMock(return_value=html)

    js_result = {
        "frame_count": 0,
        "visible_loading_count": visible_loading,
        "hidden_loading_count": hidden_loading,
        "visible_error_count": visible_error,
        "hidden_error_count": hidden_error,
        "visible_loading": [],
        "hidden_loading": [],
        "visible_error": [],
        "hidden_error": [],
        "has_fullscreen_overlay": has_overlay,
        "overlay_details": [],
    }
    # Build element details if counts > 0
    for _ in range(visible_loading):
        js_result["visible_loading"].append({
            "selector": ".loading", "tag": "div", "class": "loading",
            "text": "Loading...", "visible": True, "area": 10000,
        })
    for _ in range(hidden_loading):
        js_result["hidden_loading"].append({
            "selector": ".el-loading-mask", "tag": "div",
            "class": "el-loading-mask", "text": "", "visible": False, "area": 0,
        })
    for _ in range(visible_error):
        js_result["visible_error"].append({
            "selector": ".error", "tag": "div", "class": "error",
            "text": "Error occurred", "visible": True, "area": 10000,
        })
    for _ in range(hidden_error):
        js_result["hidden_error"].append({
            "selector": ".error-template", "tag": "div",
            "class": "error-template", "text": "", "visible": False, "area": 0,
        })
    if has_overlay:
        js_result["overlay_details"].append({
            "tag": "div", "class": "el-overlay", "text": "Loading...", "area": 800000,
        })

    page.evaluate = AsyncMock(return_value=js_result)
    page.query_selector = AsyncMock(return_value=None)  # no login form by default
    return page


def _set_login_form_visible(page):
    el = AsyncMock()
    el.is_visible = AsyncMock(return_value=True)
    page.query_selector = AsyncMock(return_value=el)


async def _run_gate(gate_fn, *args, **kwargs):
    """Helper: run an async gate function and return result."""
    return await gate_fn(*args, **kwargs)


# ======================================================================
# OPENED tests
# ======================================================================

def test_opened_normal():
    print("\n── OPENED: normal page ──")
    from src.executor.bmc_health_check import check_opened
    page = _mock_page(html="<html>" + "x" * 3000 + "</html>")
    r = asyncio.run(_run_gate(check_opened, page))
    check("OK", r.ok)
    check("PASS", r.severity == "PASS", r.severity)


def test_opened_empty_dom():
    print("\n── OPENED: empty DOM ──")
    from src.executor.bmc_health_check import check_opened
    page = _mock_page(html="<html></html>")
    r = asyncio.run(_run_gate(check_opened, page))
    check("not OK", not r.ok)
    check("BMC_EMPTY_DOM", "BMC_EMPTY_DOM" in r.reason, r.reason)


def test_opened_about_blank():
    print("\n── OPENED: about:blank ──")
    from src.executor.bmc_health_check import check_opened
    page = _mock_page(url="about:blank", html="<html>" + "x" * 2000 + "</html>")
    r = asyncio.run(_run_gate(check_opened, page))
    check("not OK", not r.ok)
    check("BMC_BROWSER_ERROR_PAGE", "BMC_BROWSER_ERROR_PAGE" in r.reason, r.reason)


def test_opened_connection_error():
    print("\n── OPENED: ERR_CONNECTION_CLOSED ──")
    from src.executor.bmc_health_check import check_opened
    page = _mock_page(html="<html>" + "x" * 2000 + "err_connection_closed</html>")
    r = asyncio.run(_run_gate(check_opened, page))
    check("not OK", not r.ok)
    check("BMC_BROWSER_ERROR_PAGE", "BMC_BROWSER_ERROR_PAGE" in r.reason, r.reason)


def test_opened_http_500():
    print("\n── OPENED: HTTP 500 ──")
    from src.executor.bmc_health_check import check_opened
    page = _mock_page(html="<html>" + "x" * 2000 + "500 internal server error</html>")
    r = asyncio.run(_run_gate(check_opened, page))
    check("not OK", not r.ok)
    check("BMC_HTTP_ERROR", "BMC_HTTP_ERROR" in r.reason, r.reason)


# ======================================================================
# AUTHENTICATED tests
# ======================================================================

def test_auth_login_form_visible():
    print("\n── AUTHENTICATED: login form visible ──")
    from src.executor.bmc_health_check import check_authenticated
    page = _mock_page(url="https://10.0.0.1/login", html="<html>" + "x" * 2000 + "</html>")
    _set_login_form_visible(page)
    r = asyncio.run(_run_gate(check_authenticated, page))
    check("not OK", not r.ok)
    check("BMC_LOGIN_FORM_STILL_VISIBLE", "BMC_LOGIN_FORM_STILL_VISIBLE" in r.reason, r.reason)


def test_auth_account_elsewhere():
    print("\n── AUTHENTICATED: 账号已在别处登录 ──")
    from src.executor.bmc_health_check import check_authenticated
    page = _mock_page(html="<html>" + "x" * 2000 + "账号已在别处登录</html>")
    r = asyncio.run(_run_gate(check_authenticated, page))
    check("not OK", not r.ok)
    check("BMC_ACCOUNT_LOGGED_IN_ELSEWHERE",
          "BMC_ACCOUNT_LOGGED_IN_ELSEWHERE" in r.reason, r.reason)


def test_auth_session_expired():
    print("\n── AUTHENTICATED: session expired ──")
    from src.executor.bmc_health_check import check_authenticated
    page = _mock_page(html="<html>" + "x" * 2000 + "session expired 请重新登录</html>")
    r = asyncio.run(_run_gate(check_authenticated, page))
    check("not OK", not r.ok)
    check("BMC_SESSION_EXPIRED", "BMC_SESSION_EXPIRED" in r.reason, r.reason)


def test_auth_normal():
    print("\n── AUTHENTICATED: normal dashboard ──")
    from src.executor.bmc_health_check import check_authenticated
    page = _mock_page()
    r = asyncio.run(_run_gate(check_authenticated, page))
    check("OK", r.ok)
    check("PASS", r.severity == "PASS", r.reason)


# ======================================================================
# PAGE_BASIC_HEALTH tests
# ======================================================================

def test_health_visible_error_overlay():
    print("\n── PAGE_BASIC_HEALTH: visible error overlay ──")
    from src.executor.bmc_health_check import check_page_basic_health
    page = _mock_page(visible_error=1, has_overlay=True)
    r = asyncio.run(_run_gate(check_page_basic_health, page))
    check("not OK", not r.ok)
    check("BMC_GLOBAL_ERROR_VISIBLE", "BMC_GLOBAL_ERROR_VISIBLE" in r.reason, r.reason)


def test_health_hidden_error_only():
    print("\n── PAGE_BASIC_HEALTH: hidden error only ──")
    from src.executor.bmc_health_check import check_page_basic_health
    page = _mock_page(hidden_error=2)
    r = asyncio.run(_run_gate(check_page_basic_health, page))
    check("OK (WARN)", r.ok)
    check("WARN", r.severity == "WARN", r.severity)
    check("BMC_PAGE_ERROR_HIDDEN_ONLY",
          "BMC_PAGE_ERROR_HIDDEN_ONLY" in r.reason, r.reason)


def test_health_login_page_returned():
    print("\n── PAGE_BASIC_HEALTH: login page returned ──")
    from src.executor.bmc_health_check import check_page_basic_health
    page = _mock_page(html="<html>" + "x" * 2000 + "账号已在别处登录</html>")
    r = asyncio.run(_run_gate(check_page_basic_health, page))
    check("not OK", not r.ok)
    check("BMC_ACCOUNT_LOGGED_IN_ELSEWHERE",
          "BMC_ACCOUNT_LOGGED_IN_ELSEWHERE" in r.reason, r.reason)


def test_health_normal():
    print("\n── PAGE_BASIC_HEALTH: normal page ──")
    from src.executor.bmc_health_check import check_page_basic_health
    page = _mock_page()
    r = asyncio.run(_run_gate(check_page_basic_health, page))
    check("OK", r.ok)
    check("PASS", r.severity == "PASS", r.reason)


# ======================================================================
# READY_FOR_CAPTURE tests
# ======================================================================

def test_ready_visible_loading():
    print("\n── READY_FOR_CAPTURE: visible loading ──")
    from src.executor.bmc_health_check import check_ready_for_capture
    # visible loading never goes away → timeout → FAIL
    # Use short max_wait to speed up test
    page = _mock_page(visible_loading=1)
    r = asyncio.run(_run_gate(check_ready_for_capture, page, max_wait=0.5))
    # After 0.5s with persistent loading, should FAIL
    # But our mock returns same state each time, so stable_count never builds
    check("not OK (loading persists)", not r.ok or r.severity == "FAIL")
    check("loading-related", "LOADING" in r.reason or "loading" in r.reason.lower(),
          r.reason)


def test_ready_hidden_loading_with_content():
    print("\n── READY_FOR_CAPTURE: hidden loading + content → PASS/WARN ──")
    from src.executor.bmc_health_check import check_ready_for_capture
    page = _mock_page(hidden_loading=2)
    r = asyncio.run(_run_gate(check_ready_for_capture, page, max_wait=2.0))
    check("OK (WARN)", r.ok)
    check("WARN or PASS", r.severity in ("WARN", "PASS"), r.severity)
    check("hidden loading", "hidden" in r.reason.lower(), r.reason)


def test_ready_visible_error():
    print("\n── READY_FOR_CAPTURE: visible error ──")
    from src.executor.bmc_health_check import check_ready_for_capture
    page = _mock_page(visible_error=1)
    r = asyncio.run(_run_gate(check_ready_for_capture, page, max_wait=2.0))
    check("not OK", not r.ok)
    check("BMC_PAGE_ERROR_VISIBLE", "BMC_PAGE_ERROR_VISIBLE" in r.reason, r.reason)


def test_ready_normal_stable():
    print("\n── READY_FOR_CAPTURE: DOM stable, no loading ──")
    from src.executor.bmc_health_check import check_ready_for_capture
    page = _mock_page()
    r = asyncio.run(_run_gate(check_ready_for_capture, page, max_wait=2.0))
    check("OK", r.ok)
    check("PASS", r.severity == "PASS", r.reason)


def test_ready_overlay_blocking():
    print("\n── READY_FOR_CAPTURE: full-screen overlay ──")
    from src.executor.bmc_health_check import check_ready_for_capture
    page = _mock_page(has_overlay=True)
    r = asyncio.run(_run_gate(check_ready_for_capture, page, max_wait=2.0))
    check("not OK", not r.ok)
    check("BMC_OVERLAY_BLOCKING", "BMC_OVERLAY_BLOCKING" in r.reason, r.reason)


# ======================================================================
# SCREENSHOT_VALIDATED tests
# ======================================================================

def test_screenshot_missing():
    print("\n── SCREENSHOT_VALIDATED: missing ──")
    from src.executor.bmc_health_check import check_screenshot_validated
    page = _mock_page()
    r = asyncio.run(_run_gate(check_screenshot_validated, page,
                              "/nonexistent/screenshot.png"))
    check("not OK", not r.ok)
    check("SCREENSHOT_MISSING", "SCREENSHOT_MISSING" in r.reason, r.reason)


def test_screenshot_too_small():
    print("\n── SCREENSHOT_VALIDATED: too small ──")
    from src.executor.bmc_health_check import check_screenshot_validated
    page = _mock_page()
    tmp = tempfile.mkdtemp()
    sp = os.path.join(tmp, "small.png")
    with open(sp, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    r = asyncio.run(_run_gate(check_screenshot_validated, page, sp))
    check("not OK", not r.ok)
    check("SCREENSHOT_TOO_SMALL", "SCREENSHOT_TOO_SMALL" in r.reason, r.reason)
    os.unlink(sp)
    os.rmdir(tmp)


def test_screenshot_normal():
    print("\n── SCREENSHOT_VALIDATED: normal ──")
    from src.executor.bmc_health_check import check_screenshot_validated
    page = _mock_page()
    tmp = tempfile.mkdtemp()
    sp = os.path.join(tmp, "normal.png")
    from PIL import Image
    import random
    random.seed(42)
    pixels = bytes(random.randint(0, 255) for _ in range(800 * 600 * 3))
    img = Image.frombytes("RGB", (800, 600), pixels)
    img.save(sp, "PNG")
    assert os.path.getsize(sp) > 5000, f"PNG too small: {os.path.getsize(sp)}B"
    r = asyncio.run(_run_gate(check_screenshot_validated, page, sp))
    check("OK", r.ok)
    check("PASS", r.severity == "PASS", r.reason)
    os.unlink(sp)
    os.rmdir(tmp)


# ======================================================================
# E2E: lifecycle runner
# ======================================================================

def test_lifecycle_normal():
    print("\n── Lifecycle: normal page, all gates PASS ──")
    from src.executor.bmc_health_check import run_page_lifecycle_gates
    page = _mock_page()
    results = asyncio.run(_run_gate(run_page_lifecycle_gates, page,
                                     "before_screenshot", wait_for_ready=True,
                                     max_ready_wait=3.0))
    check("has results", len(results) > 0)
    all_ok = all(r.ok for r in results)
    check("all gates PASS", all_ok, str([r.reason for r in results]))


def test_lifecycle_auth_fail():
    print("\n── Lifecycle: auth fail → result failed ──")
    from src.executor.bmc_health_check import run_page_lifecycle_gates
    page = _mock_page(html="<html>" + "x" * 2000 + "账号已在别处登录</html>")
    results = asyncio.run(_run_gate(run_page_lifecycle_gates, page,
                                     "after_login", wait_for_ready=False))
    check("has results", len(results) > 0)
    check("AUTHENTICATED FAIL", any(not r.ok and r.severity == "FAIL" for r in results))


def test_lifecycle_save_debug():
    print("\n── Lifecycle: save page_health_debug.json ──")
    from src.executor.bmc_health_check import run_page_lifecycle_gates, save_page_health_debug
    page = _mock_page(html="<html>" + "x" * 2000 + "账号已在别处登录</html>")
    results = asyncio.run(_run_gate(run_page_lifecycle_gates, page,
                                     "after_login", wait_for_ready=False))
    tmp = tempfile.mkdtemp()
    p = save_page_health_debug(results, tmp)
    check("file created", os.path.exists(p))

    import json
    with open(p) as f:
        data = json.load(f)
    check("has all_gate_results", "all_gate_results" in data)
    check("has failed_gate", "failed_gate" in data)
    check("failed_gate = AUTHENTICATED", data["failed_gate"] == "AUTHENTICATED",
          data["failed_gate"])

    import shutil
    shutil.rmtree(tmp)


# ======================================================================
if __name__ == "__main__":
    # OPENED
    test_opened_normal()
    test_opened_empty_dom()
    test_opened_about_blank()
    test_opened_connection_error()
    test_opened_http_500()

    # AUTHENTICATED
    test_auth_login_form_visible()
    test_auth_account_elsewhere()
    test_auth_session_expired()
    test_auth_normal()

    # PAGE_BASIC_HEALTH
    test_health_visible_error_overlay()
    test_health_hidden_error_only()
    test_health_login_page_returned()
    test_health_normal()

    # READY_FOR_CAPTURE
    test_ready_visible_loading()
    test_ready_hidden_loading_with_content()
    test_ready_visible_error()
    test_ready_normal_stable()
    test_ready_overlay_blocking()

    # SCREENSHOT_VALIDATED
    test_screenshot_missing()
    test_screenshot_too_small()
    test_screenshot_normal()

    # Lifecycle
    test_lifecycle_normal()
    test_lifecycle_auth_fail()
    test_lifecycle_save_debug()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
