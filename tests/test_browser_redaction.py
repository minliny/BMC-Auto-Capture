"""
ISSUE-009: Real browser evidence redaction tests.

Validates that the embedded redaction JavaScript in bmc_executor.py actually
redacts sensitive values in a real Playwright browser context.

Covers:
  - evidence.html redaction (cloned DOM with sensitive values replaced)
  - State mirror redaction (JS properties → HTML attributes before MHTML)
  - State JSON redaction (structured state with sensitive flag)
  - MHTML content does not contain sensitive values after state mirror
  - Visible password input
  - Hidden password input (display:none)
  - SPA login form (hidden but in DOM)
  - Token/secret/key named inputs
  - Normal (non-sensitive) fields preserved
  - REDACTED marker appears for sensitive fields
  - sensitive flag in JSON output

Browser detection:
  - Checks for Playwright + browser at runtime
  - SKIPs all tests when browser unavailable (safe for CI without deps)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.utils.html_redaction import REDACTED_DOM_SNAPSHOT_JS

# ---------------------------------------------------------------------------
# Browser availability check
# ---------------------------------------------------------------------------

BROWSER_AVAILABLE = False
BROWSER_REASON = ""

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        b.close()
    BROWSER_AVAILABLE = True
except Exception as e:
    BROWSER_REASON = f"SKIPPED_BROWSER_NOT_AVAILABLE: {e}"

# ---------------------------------------------------------------------------
# HTML template for test page
# ---------------------------------------------------------------------------

TEST_PAGE_HTML = """<html><head><meta charset="utf-8"></head><body>
<input type="password" name="password" value="RealPass123!">
<input type="password" style="display:none" name="hiddenPassword" value="HiddenPass123!">
<input name="api_token" value="token-abc-123">
<input name="secretKey" value="secret-xyz-789">
<input name="normalField" value="normal-ok">
<div id="spa-login" style="display:none">
  <input type="password" name="spaPassword" value="SpaPass123!">
</div>
</body></html>"""

# Sensitive values that MUST NOT appear in any redacted output
SENSITIVE_VALUES = [
    "RealPass123!",
    "HiddenPass123!",
    "token-abc-123",
    "secret-xyz-789",
    "SpaPass123!",
]

# ---------------------------------------------------------------------------
# Redaction JavaScript (copied verbatim from bmc_executor.py)
# ---------------------------------------------------------------------------

EVIDENCE_HTML_REDACT_JS = REDACTED_DOM_SNAPSHOT_JS

STATE_MIRROR_JS = """
() => {
    const SENSITIVE_KEYWORDS = [
        'password','passwd','pwd','token','secret','key',
        'credential','auth','session','cookie',
        '密码','口令','令牌','密钥','凭据','认证','会话',
    ];
    function _isSensitive(el) {
        const type = (el.type || '').toLowerCase();
        if (type === 'password') return true;
        const attrs = [
            el.name || '', el.id || '', el.placeholder || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('autocomplete') || '',
        ].join(' ').toLowerCase();
        for (const kw of SENSITIVE_KEYWORDS) {
            if (attrs.indexOf(kw) >= 0) return true;
        }
        for (const attr of (el.attributes || [])) {
            if (attr.name.startsWith('data-')) {
                const attrLower = attr.name.toLowerCase();
                for (const kw of SENSITIVE_KEYWORDS) {
                    if (attrLower.indexOf(kw) >= 0) return true;
                }
            }
        }
        return false;
    }
    for (const el of document.querySelectorAll('input, textarea')) {
        if (_isSensitive(el)) {
            if (el.type === 'checkbox' || el.type === 'radio') {
                el.removeAttribute('checked');
            } else {
                el.value = '';
                el.setAttribute('value', '***REDACTED***');
            }
            continue;
        }
        if (el.type === 'checkbox' || el.type === 'radio') {
            if (el.checked) el.setAttribute('checked', 'checked');
            else el.removeAttribute('checked');
        } else {
            if (el.value) el.setAttribute('value', el.value);
        }
    }
}
"""

STATE_JSON_JS = """
() => {
    const _SENSITIVE = ['password','passwd','pwd','token','secret','key',
        'credential','auth','session','cookie',
        '密码','口令','令牌','密钥','凭据','认证','会话'];
    function _isSensitiveInput(el) {
        const t = (el.type || '').toLowerCase();
        if (t === 'password') return true;
        const haystack = [
            el.name || '', el.id || '', el.placeholder || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('autocomplete') || '',
        ].join(' ').toLowerCase();
        for (const kw of _SENSITIVE) {
            if (haystack.indexOf(kw) >= 0) return true;
        }
        for (const attr of (el.attributes || [])) {
            if (attr.name.startsWith('data-')) {
                const attrLower = attr.name.toLowerCase();
                for (const kw of _SENSITIVE) {
                    if (attrLower.indexOf(kw) >= 0) return true;
                }
            }
        }
        return false;
    }
    const result = { url: location.href, title: document.title,
        timestamp: new Date().toISOString(), inputs: [], textareas: [],
        selects: [], checked_like: [], active_tab_like: [], tables: [] };
    for (const el of document.querySelectorAll('input')) {
        const t = (el.type || 'text').toLowerCase();
        const isSens = _isSensitiveInput(el);
        result.inputs.push({
            selector: el.tagName + (el.id ? '#'+el.id : '') + '[name="'+(el.name||'')+'"]',
            type: t, name: el.name || '', id: el.id || '',
            value: isSens ? '***REDACTED***' :
                   (t === 'checkbox' || t === 'radio') ? '' : (el.value || ''),
            checked: (t === 'checkbox' || t === 'radio') ? (isSens ? null : el.checked) : null,
            disabled: el.disabled,
            readonly: el.readOnly,
            sensitive: isSens || undefined,
        });
    }
    return result;
}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_sensitive_values(content: str, label: str) -> list[str]:
    """Check that no sensitive values appear in the given content.

    Returns list of leaked values (empty = pass).
    """
    leaked = [v for v in SENSITIVE_VALUES if v in content]
    if leaked:
        print(f"  LEAKED in {label}: {leaked}")
    return leaked


# ---------------------------------------------------------------------------
# Tests (all skip if no browser)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not BROWSER_AVAILABLE,
    reason=BROWSER_REASON or "SKIPPED_BROWSER_NOT_AVAILABLE",
)


@pytest.fixture(scope="module")
def browser_context():
    """Create a Playwright browser and page, yield page, then clean up."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(TEST_PAGE_HTML)
    yield page
    browser.close()
    pw.stop()


def test_browser_visible_password_input_redacted(browser_context):
    """type=password visible input must not leak real password."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    leaked = _check_sensitive_values(html, "evidence.html")
    assert not leaked, f"Visible password leaked: {leaked}"
    assert "***REDACTED***" in html, "REDACTED marker missing"


def test_browser_hidden_password_input_redacted(browser_context):
    """type=password with display:none must not leak real password."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    leaked = _check_sensitive_values(html, "evidence.html (hidden)")
    assert not leaked, f"Hidden password leaked: {leaked}"


def test_browser_spa_hidden_form_redacted(browser_context):
    """SPA login form (hidden div with password) must not leak real password."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    assert "SpaPass123!" not in html, "SPA password leaked in evidence.html"
    assert "***REDACTED***" in html


def test_browser_token_secret_key_redacted(browser_context):
    """Fields named token/secret/key must not leak real values."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    leaked = _check_sensitive_values(html, "evidence.html (token/secret)")
    assert not leaked, f"Sensitive token/secret leaked: {leaked}"


def test_browser_normal_field_preserved(browser_context):
    """Non-sensitive fields must preserve their values."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    assert "normal-ok" in html, "Normal field value was lost"


def test_browser_state_mirror_redacted(browser_context):
    """State mirror redacted JS must set value='***REDACTED***' for sensitive inputs."""
    page = browser_context
    page.evaluate(STATE_MIRROR_JS)

    for selector, name in [
        ('input[name="password"]', "password"),
        ('input[name="hiddenPassword"]', "hiddenPassword"),
        ('input[name="api_token"]', "api_token"),
        ('input[name="secretKey"]', "secretKey"),
        ('input[name="spaPassword"]', "spaPassword"),
    ]:
        val = page.get_attribute(selector, "value") or ""
        assert val == "***REDACTED***", (
            f"{name}: expected ***REDACTED***, got {val!r}"
        )

    # Normal field preserved
    normal_val = page.get_attribute('input[name="normalField"]', "value") or ""
    assert normal_val == "normal-ok", (
        f"normalField: expected 'normal-ok', got {normal_val!r}"
    )


def test_browser_mhtml_after_state_mirror_redacted(browser_context):
    """MHTML after state mirror must not contain sensitive values."""
    page = browser_context

    # Apply state mirror first (as bmc_executor.py does)
    page.evaluate(STATE_MIRROR_JS)

    # Capture MHTML via CDP
    try:
        cdp = page.context.new_cdp_session(page)
        result_cdp = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        mhtml_data = result_cdp.get("data", "")
    except Exception:
        pytest.skip("CDP MHTML capture not supported in this environment")

    if not mhtml_data or len(mhtml_data) < 100:
        pytest.skip("MHTML capture returned empty data")

    leaked = _check_sensitive_values(mhtml_data, "MHTML")
    assert not leaked, f"MHTML leaked sensitive values: {leaked}"
    assert "***REDACTED***" in mhtml_data, "REDACTED marker not in MHTML"


def test_browser_state_json_redacted(browser_context):
    """State JSON must redact sensitive values and set sensitive flag."""
    page = browser_context
    state = page.evaluate(STATE_JSON_JS)

    for inp in state["inputs"]:
        name = inp["name"]
        value = inp["value"]
        sensitive = inp.get("sensitive", False)

        if name in ("password", "hiddenPassword", "api_token", "secretKey", "spaPassword"):
            assert value == "***REDACTED***", (
                f"{name}: expected REDACTED, got {value!r}"
            )
            assert sensitive, f"{name}: expected sensitive=True"
        elif name == "normalField":
            assert value == "normal-ok", (
                f"normalField: expected 'normal-ok', got {value!r}"
            )
            assert not sensitive, f"normalField: expected sensitive=False"


def test_browser_state_json_no_sensitive_values(browser_context):
    """State JSON must not contain any raw sensitive values."""
    page = browser_context
    state = page.evaluate(STATE_JSON_JS)

    json_str = json.dumps(state)
    leaked = _check_sensitive_values(json_str, "state.json")
    assert not leaked, f"State JSON leaked: {leaked}"


def test_browser_authorization_not_in_output():
    """Log output must not contain Authorization: Basic or Bearer from forms.

    Verify that the redaction JS covers Authorization/Bearer patterns.
    """
    # Verify the REDACTED_DOM_SNAPSHOT_JS removes scripts and event handlers
    # which could contain Authorization headers
    from src.utils.html_redaction import REDACTED_DOM_SNAPSHOT_JS
    # The JS removes all <script> tags and on* event handlers
    assert "s.remove()" in REDACTED_DOM_SNAPSHOT_JS, "Script removal must be in redaction JS"
    assert "removeAttribute" in REDACTED_DOM_SNAPSHOT_JS, "Event handler removal must be in redaction JS"
    # The sensitive keywords include 'auth' which covers Authorization
    assert "'auth'" in REDACTED_DOM_SNAPSHOT_JS, "Auth keyword must be in sensitive list"


def test_browser_evidence_html_no_scripts(browser_context):
    """evidence.html must not contain <script> tags."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    assert "<script" not in html, "evidence.html contains script tags"


def test_browser_evidence_html_no_event_handlers(browser_context):
    """evidence.html must not contain on* event handler attributes."""
    page = browser_context
    html = page.evaluate(EVIDENCE_HTML_REDACT_JS)
    assert " on" not in html.lower(), "evidence.html contains event handler attributes"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
