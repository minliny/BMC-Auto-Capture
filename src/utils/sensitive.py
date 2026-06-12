"""Small redaction helpers for values that may be written to logs."""

from __future__ import annotations

import base64
import json
import logging
import quopri
import re
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

_logger = logging.getLogger(__name__)


_REDACTED = "***REDACTED***"

_SENSITIVE_KEY_PATTERNS = (
    "password", "passwd", "pwd", "secret", "token", "credential",
    "apikey", "api_key", "access_token", "refresh_token", "authorization",
    "auth", "cookie", "session", "key",
)

# Sensitive key patterns for URL query params
_SENSITIVE_QUERY_KEYS = frozenset({
    'token', 'secret', 'api_key', 'access_token', 'refresh_token',
    'password', 'passwd', 'pwd', 'key', 'auth',
})

# Sensitive value patterns for text redaction
_SENSITIVE_VALUE_PATTERNS = [
    re.compile(r'(Authorization\s*:\s*)(Bearer\s+\S+|Basic\s+\S+)', re.IGNORECASE),
    re.compile(r'(Bearer\s+)\S+', re.IGNORECASE),
    re.compile(r'(Basic\s+)[A-Za-z0-9+/=]+', re.IGNORECASE),
]

_SENSITIVE_FIELD_KEYWORDS = (
    "password", "passwd", "pwd", "token", "secret", "api_key", "apikey",
    "access_token", "refresh_token", "authorization", "auth", "credential",
    "cookie", "session", "bearer", "basic",
)


def redact_sensitive_text(text: str) -> str:
    """Redact sensitive values from arbitrary text content.

    Handles:
    - Authorization: Bearer/Basic headers
    - Standalone Bearer/Basic tokens
    - key=value or key:value patterns for sensitive keys
    - Does NOT redact normal text like 'normal-ok' or device names
    """
    if not text:
        return text or ""
    result = str(text)
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        result = pattern.sub(r'\1***REDACTED***', result)
    # Redact key=value or key:value patterns for sensitive keys
    kv_pattern = (
        r'(?i)((?:password|passwd|pwd|secret|token|credential|apikey|api_key|'
        r'access_token|refresh_token|authorization|auth|cookie|session|key)'
        r'\s*[=:]\s*)[^\s&;,<>"\']+'
    )
    result = re.sub(kv_pattern, rf'\1{_REDACTED}', result)
    # Defensive handling for standalone secret-like identifiers found in
    # response bodies and captured page text.
    result = re.sub(
        r'(?i)\b[\w.-]*(?:password|passwd|pwd|secret|token|credential|'
        r'apikey|api_key|access_token|refresh_token|authorization|cookie|'
        r'session)[\w.-]*\b',
        _REDACTED,
        result,
    )
    return result


def _is_sensitive_field(tag_text: str) -> bool:
    """Check if an HTML element has sensitive attributes (name, id, type, autocomplete, aria-label, placeholder)."""
    attr_pattern = re.compile(
        r'(?:name|id|type|autocomplete|aria-label|placeholder)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))',
        re.IGNORECASE,
    )
    for match in attr_pattern.finditer(tag_text):
        value = (match.group(1) or match.group(2) or match.group(3) or "").lower()
        if any(kw in value for kw in _SENSITIVE_FIELD_KEYWORDS):
            return True
    return False


def redact_html_sensitive_fields(html_text: str) -> str:
    """Redact values in sensitive HTML form fields (input, textarea, select).

    Detects sensitive fields by checking name, id, type, autocomplete,
    aria-label, and placeholder attributes for sensitive keywords.
    For sensitive <input>: replaces value attribute with ***REDACTED***.
    For sensitive <textarea>: replaces content with ***REDACTED***.
    For sensitive <select>: replaces all option values and texts with ***REDACTED***.
    """
    if not html_text:
        return html_text or ""

    result = html_text

    # Handle <input> elements - redact value attribute
    def _redact_input(match: re.Match) -> str:
        tag = match.group(0)
        if not _is_sensitive_field(tag):
            return tag
        return re.sub(
            r'(\bvalue\s*=\s*)(["\'])(.*?)\2',
            rf'\1\2{_REDACTED}\2',
            tag,
            count=1,
            flags=re.IGNORECASE,
        )

    result = re.sub(r'<input\b[^>]*/?\s*>', _redact_input, result, flags=re.IGNORECASE)

    # Handle <textarea> elements - redact content
    def _redact_textarea(match: re.Match) -> str:
        open_tag = match.group(1)
        content = match.group(2)
        close_tag = match.group(3)
        if not _is_sensitive_field(open_tag):
            return match.group(0)
        return f"{open_tag}{_REDACTED}{close_tag}"

    result = re.sub(
        r'(<textarea\b[^>]*>)(.*?)(</textarea\s*>)',
        _redact_textarea,
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Handle <select> elements - redact option values and texts
    def _redact_select(match: re.Match) -> str:
        open_tag = match.group(1)
        content = match.group(2)
        close_tag = match.group(3)
        if not _is_sensitive_field(open_tag):
            return match.group(0)
        # Redact option value attributes
        redacted = re.sub(
            r'(<option\b[^>]*?\bvalue\s*=\s*)(["\'])(.*?)\2',
            rf'\1\2{_REDACTED}\2',
            content,
            flags=re.IGNORECASE,
        )
        # Redact option text content
        redacted = re.sub(
            r'(<option\b[^>]*>)(.*?)(</option\s*>)',
            rf'\1{_REDACTED}\3',
            redacted,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return f"{open_tag}{redacted}{close_tag}"

    result = re.sub(
        r'(<select\b[^>]*>)(.*?)(</select\s*>)',
        _redact_select,
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return result


def redact_sensitive_url(url: str) -> str:
    """Redact sensitive components from a URL.

    - Removes userinfo (user:password@)
    - Redacts sensitive query parameters (token, secret, api_key, password, etc.)
    - Preserves scheme, host, path, and non-sensitive query params
    """
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        # Redact userinfo
        username = parsed.username or ""
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{hostname}{port}"
        if username:
            netloc = f"{username}:***REDACTED***@{netloc}"

        # Redact sensitive query params
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            redacted = {}
            for k, v in params.items():
                if k.lower() in _SENSITIVE_QUERY_KEYS:
                    redacted[k] = '***REDACTED***'
                else:
                    redacted[k] = v[-1] if v else ''
            query = urlencode(redacted, safe='*')
        else:
            query = ""

        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(pattern in key_lower for pattern in _SENSITIVE_KEY_PATTERNS)


def _looks_like_url_key(key: str) -> bool:
    key_lower = key.lower()
    return key_lower == "url" or key_lower.endswith("_url") or key_lower.endswith("url")


def redact_nested_payload(obj, depth: int = 0) -> Any:
    """Recursively redact sensitive values from nested dicts/lists.

    Redacts values for keys containing sensitive patterns:
    password, passwd, pwd, token, secret, api_key, access_token, refresh_token,
    Authorization, authorization, credential, apikey, auth

    Uses substring matching: a key like 'auth_token' matches because it
    contains 'token'. A key like 'password_hash' matches because it
    contains 'password'.

    Handles nested dicts, lists, and tuples up to max depth.
    """
    MAX_DEPTH = 20

    if depth >= MAX_DEPTH:
        return "***TRUNCATED***"

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                result[k] = _REDACTED
            elif isinstance(k, str) and _looks_like_url_key(k) and isinstance(v, str):
                result[k] = redact_sensitive_url(v)
            else:
                result[k] = redact_nested_payload(v, depth + 1)
        return result
    elif isinstance(obj, list):
        return [redact_nested_payload(item, depth + 1) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(redact_nested_payload(item, depth + 1) for item in obj)
    elif isinstance(obj, str):
        return redact_sensitive_text(obj)
    return obj


def _redact_state_node(obj: Any, depth: int = 0, parent_sensitive: bool = False) -> Any:
    if depth >= 30:
        return "***TRUNCATED***"
    if isinstance(obj, dict):
        identity = " ".join(
            str(obj.get(field, ""))
            for field in ("selector", "name", "id", "label", "aria_label")
        )
        node_sensitive = parent_sensitive or _is_sensitive_key(identity)
        result = {}
        for key, value in obj.items():
            key_text = str(key)
            if _looks_like_url_key(key_text) and isinstance(value, str):
                result[key] = redact_sensitive_url(value)
            elif _is_sensitive_key(key_text):
                result[key] = _REDACTED
            elif node_sensitive and key_text.lower() in {
                "value", "text", "selected_values", "selected_texts",
                "visible_text", "visible_text_excerpt",
            }:
                result[key] = _REDACTED
            else:
                result[key] = _redact_state_node(value, depth + 1, node_sensitive)
        return result
    if isinstance(obj, list):
        return [_redact_state_node(item, depth + 1, parent_sensitive) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_state_node(item, depth + 1, parent_sensitive) for item in obj)
    if isinstance(obj, str):
        return redact_sensitive_text(obj)
    return obj


def redact_state_payload(state_data: dict) -> dict:
    """Redact sensitive values from state.json payload before writing to disk.

    Handles:
    - URL field (redact_sensitive_url)
    - visible_text (redact_sensitive_text)
    - table visible_text_excerpt (redact_sensitive_text)
    - custom element text (checked_like, active_tab_like)
    - metadata.url
    - metadata.addressbar_url
    """
    if not state_data or not isinstance(state_data, dict):
        return state_data
    return _redact_state_node(state_data)


def _replace_transfer_encoded_payload(part, text: str, charset: str, cte: str) -> None:
    encoded = text.encode(charset, errors="replace")
    if cte == "base64":
        payload = base64.encodebytes(encoded).decode("ascii")
    elif cte == "quoted-printable":
        payload = quopri.encodestring(encoded).decode("ascii")
    else:
        try:
            payload = encoded.decode("ascii")
        except UnicodeDecodeError:
            payload = quopri.encodestring(encoded).decode("ascii")
            cte = "quoted-printable"
    part.set_payload(payload)
    if part.get("Content-Transfer-Encoding"):
        part.replace_header("Content-Transfer-Encoding", cte or "7bit")
    elif cte:
        part["Content-Transfer-Encoding"] = cte


def _redact_base64_fallback(text: str) -> str:
    pattern = re.compile(r"(?m)^[A-Za-z0-9+/]{16,}={0,2}$")

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        try:
            decoded = base64.b64decode(raw, validate=True)
            decoded_text = decoded.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return raw
        redacted = redact_sensitive_text(decoded_text)
        if redacted == decoded_text:
            return raw
        return base64.b64encode(redacted.encode("utf-8")).decode("ascii")

    return pattern.sub(replace, text)


def redact_mhtml_payload(mhtml_data: str) -> str:
    """Redact sensitive values from MHTML content before writing to disk.

    MIME text parts are decoded, redacted, and re-encoded using their original
    transfer encoding. Malformed messages receive conservative text and base64
    fallback scanning.
    """
    if not mhtml_data:
        return mhtml_data or ""
    raw_bytes = mhtml_data.encode("utf-8", errors="replace")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        processed = False
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type().lower()
            if not (
                content_type.startswith("text/")
                or content_type in {"application/json", "application/xhtml+xml", "application/xml"}
            ):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                payload_text = str(part.get_payload() or "")
            else:
                charset = part.get_content_charset() or "utf-8"
                try:
                    payload_text = payload.decode(charset, errors="replace")
                except LookupError:
                    charset = "utf-8"
                    payload_text = payload.decode(charset, errors="replace")
            charset = part.get_content_charset() or "utf-8"
            cte = (part.get("Content-Transfer-Encoding") or "7bit").lower()
            # Apply HTML-aware field redaction for HTML content first
            if content_type in {"text/html", "application/xhtml+xml"}:
                payload_text = redact_html_sensitive_fields(payload_text)
            # For JSON content, use structured redaction to avoid replacing
            # keys instead of values (e.g. {"token":"secret"} must become
            # {"token":"***REDACTED***"}, not {"***REDACTED***":"secret"}).
            if content_type == "application/json":
                try:
                    parsed = json.loads(payload_text)
                    redacted_obj = redact_nested_payload(parsed)
                    redacted = json.dumps(redacted_obj, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError, TypeError):
                    redacted = redact_sensitive_text(payload_text)
            else:
                redacted = redact_sensitive_text(payload_text)
            _replace_transfer_encoded_payload(part, redacted, charset, cte)
            processed = True
        if processed:
            return message.as_bytes(policy=policy.default).decode("utf-8", errors="replace")
    except Exception as _e:
        _logger.debug("redact_mhtml_payload MIME walk failed: %s", _e)
    return _redact_base64_fallback(redact_sensitive_text(mhtml_data))


def redact_url_for_log(url: str) -> str:
    """Keep URL location while dropping credentials, query, and fragment."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"
