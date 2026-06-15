"""
Screenshot utilities — capture, overlay compositing, long-screenshot generation.

Enhanced for v0.2:
- Chinese font priority (msyh, simsun, PingFang, etc.)
- ANSI escape sequence cleaning
- Tab expansion
- Long output PNG pagination (MAX_IMAGE_HEIGHT=8000)
- Safe filename handling
"""

from __future__ import annotations
import os
import re
import sys
import platform
import json
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# === Constants ===
MAX_IMAGE_HEIGHT = 8000
TAB_SIZE = 4

# === Font paths by platform ===
WINDOWS_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",    # 微软雅黑粗体
    "C:/Windows/Fonts/simsun.ttc",    # 宋体
    "C:/Windows/Fonts/simhei.ttf",    # 黑体
    "C:/Windows/Fonts/Deng.ttf",      # 等线
    "C:/Windows/Fonts/Dengb.ttf",     # 等线粗体
    "C:/Windows/Fonts/Consola.ttf",   # Consolas (fallback)
]

MACOS_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Menlo.ttc",
]

LINUX_FONTS = [
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
]


# === Text cleaning utilities ===

ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def clean_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return ANSI_ESCAPE_PATTERN.sub('', text)


def normalize_output(text: str) -> str:
    """Normalize SSH output: clean ANSI, normalize line endings, expand tabs."""
    text = clean_ansi(text)
    # Normalize \r\n and standalone \r
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Remove duplicate blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Expand tabs
    text = text.expandtabs(TAB_SIZE)
    return text.strip() + '\n'  # Ensure trailing newline


def clean_output_for_png(text: str) -> str:
    """Normalize terminal output for PNG without adding or rewriting content."""
    return normalize_output(text)


# === Safe filename utilities ===

WINDOWS_ILLEGAL_CHARS = r'[<>:"/\\|?*]'
WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
}


def safe_filename(filename: str) -> str:
    """Ensure filename is safe and non-empty.

    - Replace Windows illegal characters with _
    - Handle empty/placeholder names
    - Preserve Chinese characters and alphanumerics
    """
    if not filename:
        return "unnamed_artifact"

    # Strip path separators
    filename = filename.replace('/', '_').replace('\\', '_')

    # Check for placeholder-only names
    stripped = filename.strip('_- .')
    if not stripped:
        return "unnamed_artifact"

    # Replace illegal Windows characters
    filename = re.sub(WINDOWS_ILLEGAL_CHARS, '_', filename)

    # Check for reserved Windows names (e.g., "CON.png")
    name_upper = filename.split('.')[0].upper()
    if name_upper in WINDOWS_RESERVED_NAMES:
        filename = f"file_{filename}"

    # Collapse multiple underscores
    filename = re.sub(r'_+', '_', filename)
    filename = filename.strip('_')

    return filename or "unnamed_artifact"


# === Font loading ===

def _get_font_paths() -> list[str]:
    """Get font paths for current platform."""
    system = platform.system()
    if system == 'Windows':
        return WINDOWS_FONTS
    elif system == 'Darwin':
        return MACOS_FONTS
    else:  # Linux and others
        return LINUX_FONTS


def _default_font(size: int = 14) -> ImageFont.FreeTypeFont:
    """Try to load a CJK-capable monospace font, fall back gracefully.

    Priority:
    1. Chinese-capable fonts (msyh, simsun, PingFang, NotoSansCJK)
    2. Monospace fonts (Consola, Menlo, DejaVuSansMono)
    3. PIL default (may not render Chinese)
    """
    for path in _get_font_paths():
        try:
            font = ImageFont.truetype(path, size)
            return font
        except (OSError, IOError):
            continue

    # Last resort: try PIL default
    try:
        return ImageFont.load_default()
    except Exception:
        raise RuntimeError(f"No suitable font found. Tried: {_get_font_paths()}")


# === Screenshot utilities ===

def overlay_device_info(
    image_path: str,
    device_name: str,
    device_ip: str,
    task_name: str,
    page_url: str = "",
    page_title: str = "",
) -> str:
    """Overlay device/task watermark at the top-left of the screenshot."""
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    font = _default_font(15)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"{device_name}  |  {device_ip}",
        f"Task: {task_name}",
        timestamp,
    ]
    if page_url:
        lines.append(page_url[:100])

    x, y = 10, 10
    for line in lines:
        draw.text((x + 1, y + 1), line, fill=(0, 0, 0), font=font)
        draw.text((x, y), line, fill=(255, 255, 255), font=font)
        y += 22

    out_path = image_path.replace(".png", "_annotated.png")
    img.save(out_path, "PNG")
    return out_path


def _calculate_image_dimensions(lines: list[str], font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """Calculate PNG dimensions based on text content."""
    line_height = 18
    char_width = 8  # approximate for monospace at size 13

    max_line_len = max((len(line) for line in lines), default=80)
    img_width = max(800, char_width * max_line_len + 40)
    img_height = max(600, line_height * len(lines) + 40)

    return img_width, img_height


def _render_single_page(
    lines: list[str],
    output_dir: str,
    base_filename: str,
    page_index: int = 0,
    total_pages: int = 1,
) -> tuple[str, int]:
    """Render a single page of text to PNG. Returns (path, height)."""
    font = _default_font(13)
    line_height = 18
    char_width = 8

    max_line_len = max((len(line) for line in lines), default=80)
    img_width = max(800, char_width * max_line_len + 40)
    img_height = line_height * len(lines) + 40

    img = Image.new("RGB", (img_width, img_height), (15, 15, 15))
    draw = ImageDraw.Draw(img)

    y = 10
    for line in lines:
        # Draw shadow for readability
        draw.text((11, y), line, fill=(50, 50, 50), font=font)
        draw.text((10, y), line, fill=(200, 200, 200), font=font)
        y += line_height

    # Generate filename with consistent _partNNN suffix for all pages
    if base_filename.endswith('.png'):
        name_part = base_filename[:-4]
    else:
        name_part = base_filename
    if total_pages == 1:
        path = os.path.join(output_dir, base_filename)
    else:
        path = os.path.join(output_dir, f"{name_part}_part{page_index + 1:03d}.png")

    img.save(path, "PNG")
    return path, img_height


def _write_manifest(output_dir: str, base_filename: str, all_paths: list[str], total_height: int) -> str:
    """Write manifest for multi-page PNG output."""
    if base_filename.endswith('.png'):
        name_part = base_filename[:-4]
    else:
        name_part = base_filename

    manifest = {
        "original_height": total_height,
        "max_page_height": MAX_IMAGE_HEIGHT,
        "page_count": len(all_paths),
        "paths": all_paths,
        "primary": all_paths[0] if all_paths else "",
    }

    manifest_path = os.path.join(output_dir, f"{name_part}_png_parts.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest_path


def render_text_to_images(
    text: str,
    output_dir: str,
    filename: str = "terminal.png",
) -> list[str]:
    """Render terminal-style text output as PNG(s), with pagination for long output.

    Args:
        text: Raw SSH output (may contain ANSI codes, pagination prompts)
        output_dir: Output directory
        filename: Base filename (e.g., "terminal.png")

    Returns:
        List of PNG paths (may be 1 or more if pagination occurred)
    """
    # Clean and normalize text
    text = clean_output_for_png(text)
    lines = text.split('\n')

    all_paths = []
    current_lines = []
    current_height = 40
    page_index = 0
    total_height = 0

    # First pass: count pages to know total
    page_count = 1
    test_height = 40
    for line in lines:
        if test_height + 18 > MAX_IMAGE_HEIGHT:
            page_count += 1
            test_height = 40
        test_height += 18

    for line in lines:
        line_height_estimate = 18  # Approximate
        if current_height + line_height_estimate > MAX_IMAGE_HEIGHT:
            # Save current page
            if current_lines:
                path, height = _render_single_page(
                    current_lines, output_dir, filename, page_index, page_count
                )
                all_paths.append(path)
                total_height += height
                page_index += 1
                current_lines = []
                current_height = 40

        current_lines.append(line)
        current_height += line_height_estimate

    # Don't forget the last page
    if current_lines:
        path, height = _render_single_page(
            current_lines, output_dir, filename, page_index, page_count
        )
        all_paths.append(path)
        total_height += height

    # Write manifest if multiple pages
    if len(all_paths) > 1:
        manifest_path = _write_manifest(output_dir, filename, all_paths, total_height)

    return all_paths


def render_text_to_image(
    text: str,
    output_dir: str,
    filename: str = "terminal.png",
) -> str:
    """Legacy interface: render text to PNG, returns primary path.

    Internally calls render_text_to_images(), but returns only the first PNG path.
    For multi-page output, generates manifest for additional pages.

    Args:
        text: Raw SSH output
        output_dir: Output directory
        filename: Base filename

    Returns:
        Path to primary PNG (or first page)
    """
    paths = render_text_to_images(text, output_dir, filename)
    return paths[0] if paths else ""
