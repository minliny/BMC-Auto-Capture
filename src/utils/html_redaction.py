"""Browser DOM redaction used by every HTML evidence writer."""

from __future__ import annotations


REDACTED_DOM_SNAPSHOT_JS = r"""
() => {
    const root = document.documentElement.cloneNode(true);
    const sensitive = [
        'password', 'passwd', 'pwd', 'token', 'secret', 'key',
        'credential', 'auth', 'session', 'cookie',
        'api_key', 'apikey', 'access_token', 'refresh_token',
        'bearer', 'basic',
        '密码', '口令', '令牌', '密钥', '凭据', '认证', '会话',
    ];
    function isSensitive(el) {
        if ((el.type || '').toLowerCase() === 'password') return true;
        const haystack = [
            el.name || '', el.id || '', el.placeholder || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('autocomplete') || '',
            ...[...(el.attributes || [])]
                .filter(a => a.name.startsWith('data-'))
                .map(a => a.name + ' ' + a.value),
        ].join(' ').toLowerCase();
        return sensitive.some(kw => haystack.includes(kw));
    }
    for (const el of root.querySelectorAll('input, textarea')) {
        if (!isSensitive(el)) continue;
        el.setAttribute('value', '***REDACTED***');
        el.textContent = '***REDACTED***';
    }
    for (const sel of root.querySelectorAll('select')) {
        if (!isSensitive(sel)) continue;
        for (const opt of sel.options) {
            opt.setAttribute('value', '***REDACTED***');
            opt.textContent = '***REDACTED***';
        }
    }
    for (const s of root.querySelectorAll('script')) s.remove();
    for (const el of root.querySelectorAll('*')) {
        for (const attr of [...el.attributes]) {
            if (attr.name.toLowerCase().startsWith('on')) {
                el.removeAttribute(attr.name);
            }
        }
    }
    return '<!DOCTYPE html>\n' + root.outerHTML;
}
"""


async def capture_redacted_html(page) -> str:
    """Return a redacted, inert DOM snapshot suitable for persistence."""
    return await page.evaluate(REDACTED_DOM_SNAPSHOT_JS)
