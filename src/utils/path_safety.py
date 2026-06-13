"""
Path safety utilities — prevent path traversal / output root escape.

All output file/directory paths must go through safe_join_under_root().
"""

from __future__ import annotations
import os
import re
import logging
from pathlib import Path

logger = logging.getLogger("bmc_auto_capture.path_safety")

# Characters that must never appear in safe filenames
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f<>:"/\\|?*\x00]')

# Pattern that detects path traversal
_TRAVERSAL = re.compile(r'(?:^|[\\/])\.\.(?:[\\/]|$)')

# Windows drive letter pattern
_DRIVE_LETTER = re.compile(r'^[A-Za-z]:')

# Keywords that must NOT appear in output paths/filenames (P1-8: password template variables)
_FORBIDDEN_KEYWORDS = [
    "{带外管理密码}", "{带内管理密码}",
    "{OOB_Password}", "{IB_Password}",
]

# Maximum path component length (most filesystems: 255 bytes for UTF-8)
MAX_COMPONENT_LENGTH = 200

# Characters to replace in safe filenames
_FILENAME_REPLACE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, max_len: int = MAX_COMPONENT_LENGTH) -> str:
    """Sanitize a filename component: remove unsafe chars, collapse whitespace, trim.

    Args:
        name: Raw filename (device_name, task_name, template value, etc.)
        max_len: Maximum length of the sanitized name

    Returns:
        Safe filename string, never empty.
    """
    if not name:
        return "unnamed"
    # Strip leading/trailing whitespace
    name = name.strip()
    if not name:
        return "unnamed"
    # Replace unsafe characters
    name = _FILENAME_REPLACE.sub('_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Strip leading/trailing dots and spaces (Windows can't handle them)
    name = name.strip('. ')
    if not name:
        return "unnamed"
    # Trim to max length
    if len(name) > max_len:
        name = name[:max_len].rstrip('. ')
    return name


def is_safe_path_component(component: str) -> bool:
    """Check if a path component is safe (no traversal, no absolute path, no drive letter)."""
    if not component:
        return False
    # Absolute path
    if os.path.isabs(component):
        return False
    # Path traversal
    if _TRAVERSAL.search(component):
        return False
    # Drive letter (Windows)
    if _DRIVE_LETTER.match(component):
        return False
    # Null bytes
    if '\x00' in component:
        return False
    return True


def safe_join_under_root(root: str, *components: str) -> str:
    """Join path components under root, rejecting any traversal or absolute paths.

    Each component is individually sanitized via safe_filename() before joining.
    After joining, the result is resolved and checked to be under root.

    Args:
        root: Absolute output root directory
        *components: Path components to join (filenames, dir names)

    Returns:
        Absolute path under root

    Raises:
        ValueError: If any component attempts traversal or if final path escapes root
    """
    # Normalize root
    root = os.path.abspath(os.path.normpath(root))
    if not os.path.isabs(root):
        raise ValueError(f"Output root must be absolute: {root!r}")

    # Sanitize each component
    safe_parts = []
    for comp in components:
        comp = str(comp)
        # Skip empty components silently (handles trailing slashes, double slashes)
        if not comp.strip():
            continue
        # Check for raw traversal before sanitization
        if not is_safe_path_component(comp):
            raise ValueError(
                f"Unsafe path component: {comp!r} "
                f"(contains traversal, absolute path, or drive letter)"
            )
        safe_parts.append(safe_filename(comp))

    # Join
    candidate = os.path.join(root, *safe_parts)
    # Resolve (handles any remaining .. tricks)
    resolved = os.path.abspath(os.path.normpath(candidate))

    # Containment check
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise ValueError(
            f"Path escape detected: {candidate!r} resolved to {resolved!r} "
            f"which is outside root {root!r}"
        )

    return resolved


def resolve_under_output_root(output_root: str, template_value: str) -> str:
    """Resolve a template-derived path component under output_root.

    This is the main entry point for output dir/file path construction.
    Splits template_value on / to handle multi-level template paths,
    sanitizes each component, and ensures containment.

    Args:
        output_root: Absolute output root directory
        template_value: Template-resolved path (e.g. "01 RAID测试/A3")

    Returns:
        Absolute safe path under output_root
    """
    # Split on / (or \) to handle template paths with subdirectories
    parts = template_value.replace('\\', '/').split('/')
    # Filter empty parts
    parts = [p for p in parts if p.strip()]
    if not parts:
        parts = ["unnamed"]
    return safe_join_under_root(output_root, *parts)


def check_forbidden_template_vars(template: str) -> list[str]:
    """Check if a template string references forbidden password variables.

    Args:
        template: Template string to check

    Returns:
        List of forbidden variable names found (empty = safe)
    """
    found = []
    for kw in _FORBIDDEN_KEYWORDS:
        if kw in template:
            found.append(kw)
    return found


# ISSUE-008: broader sensitive keywords that must fail-fast in path contexts
_PATH_SENSITIVE_KEYWORDS = [
    "{带外管理密码}", "{带内管理密码}",
    "{OOB_Password}", "{IB_Password}",
    "password", "passwd", "pwd",
    "token", "secret", "key",
    "Authorization",
]

# Keywords that indicate password/token/secret in template variable names
# Matches patterns like {xxx_password}, {api_token}, {OOB_Password}, etc.
_PATH_SENSITIVE_RE = re.compile(
    r'\{(?:[^}]*'
    r'(?:密码|password|passwd|pwd|token|secret|key|credential|auth|Authorization)'
    r'[^}]*)\}',
    re.IGNORECASE,
)


def check_path_template_for_sensitive_vars(template: str) -> list[str]:
    """Check a path/file template for sensitive variable references.

    For path/file contexts, sensitive variables must cause a FAIL-FAST error,
    NOT be silently replaced with REDACTED (which could cause filename conflicts).

    Args:
        template: Resolved or unresolved template string for a path/filename

    Returns:
        List of matched sensitive patterns found (empty = safe for path use)
    """
    found: list[str] = []
    # Check static keyword list
    for kw in _PATH_SENSITIVE_KEYWORDS:
        if kw.lower() in template.lower():
            found.append(kw)
    # Check regex for template variable patterns
    for m in _PATH_SENSITIVE_RE.finditer(template):
        matched = m.group(0)
        if matched not in found:
            found.append(matched)
    return found


def validate_template_for_path(template: str, context: str = "path") -> None:
    """Validate that a template is safe for use in file/directory paths.

    Raises ValueError with sanitized message (no real passwords) if sensitive
    variables or keywords are detected.

    Args:
        template: Template string to validate (can be pre- or post-resolution)
        context: Description of what the path is for (e.g. "output_dir", "image_name")

    Raises:
        ValueError: if the template contains sensitive variables/keywords
    """
    found = check_path_template_for_sensitive_vars(template)
    if not found:
        return
    # Sanitize: describe what was found without revealing actual values
    categories = set()
    for item in found:
        lower = item.lower()
        if any(kw in lower for kw in ('密码', 'password', 'passwd', 'pwd')):
            categories.add('password_variable')
        elif any(kw in lower for kw in ('token',)):
            categories.add('token_variable')
        elif any(kw in lower for kw in ('secret',)):
            categories.add('secret_variable')
        elif any(kw in lower for kw in ('key',)):
            categories.add('key_variable')
        elif any(kw in lower for kw in ('auth', 'authorization')):
            categories.add('auth_variable')
        else:
            categories.add('sensitive_variable')
    cats_str = ', '.join(sorted(categories))
    raise ValueError(
        f"TEMPLATE_SENSITIVE_FIELD_IN_PATH: {context} template contains "
        f"sensitive variable types: {cats_str}. "
        f"Password/token/secret/key variables are not allowed in paths. "
        f"Count: {len(found)} sensitive pattern(s) detected."
    )
