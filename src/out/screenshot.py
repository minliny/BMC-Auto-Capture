"""
Screenshot utilities — capture, overlay compositing, long-screenshot generation.
"""


from __future__ import annotations
import os
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _default_font(size: int = 14):
    """Try to load a monospace font, fall back to default."""
    try:
        return ImageFont.truetype("C:/Windows/Fonts/consola.ttf", size)
    except (OSError, IOError):
        try:
            return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size)
        except (OSError, IOError):
            return ImageFont.load_default()


def overlay_device_info(
    image_path: str,
    device_name: str,
    device_ip: str,
    task_name: str,
    page_url: str = "",
    page_title: str = "",
) -> str:
    """Add a Chrome-style taskbar + device info bar to the screenshot."""
    img = Image.open(image_path)
    width, height = img.size

    font = _default_font(13)
    font_small = _default_font(11)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- Chrome taskbar (top) ---
    tab_bar_h = 36
    url_bar_h = 32
    device_bar_h = 22
    chrome_h = tab_bar_h + url_bar_h + device_bar_h

    canvas = Image.new("RGBA", (width, height + chrome_h), (40, 40, 40, 255))

    # Tab bar background
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 0), (width, tab_bar_h)], fill=(50, 50, 50))
    # Active tab
    tab_w = min(width - 100, 280)
    draw.rectangle([(8, 6), (tab_w, tab_bar_h)], fill=(60, 60, 60))
    tab_title = page_title[:50] if page_title else "BMC Management"
    draw.text((16, 10), tab_title, fill=(220, 220, 220), font=font_small)
    # Window controls (red/yellow/green dots)
    for cx, color in [(width - 60, (235, 95, 92)), (width - 40, (245, 190, 40)), (width - 20, (100, 200, 80))]:
        draw.ellipse([(cx, 13), (cx + 11, 24)], fill=color)

    # URL bar
    draw.rectangle([(0, tab_bar_h), (width, tab_bar_h + url_bar_h)], fill=(70, 70, 70))
    url_display = page_url[:120] if page_url else "about:blank"
    url_box_w = width - 120
    draw.rectangle([(8, tab_bar_h + 5), (url_box_w, tab_bar_h + url_bar_h - 5)], fill=(55, 55, 55))
    draw.text((16, tab_bar_h + 10), url_display, fill=(200, 200, 200), font=font_small)

    # Device info bar
    draw.rectangle([(0, tab_bar_h + url_bar_h), (width, chrome_h)], fill=(30, 30, 30))
    info = f"Device: {device_name} | IP: {device_ip} | Task: {task_name}"
    draw.text((10, tab_bar_h + url_bar_h + 3), info, fill=(180, 180, 180), font=font_small)
    draw.text((width - 180, tab_bar_h + url_bar_h + 3), timestamp, fill=(150, 150, 150), font=font_small)

    # Paste page screenshot below chrome
    canvas.paste(img, (0, chrome_h))

    out_path = image_path.replace(".png", "_annotated.png")
    canvas.save(out_path, "PNG")
    return out_path


def render_text_to_image(text: str, output_dir: str, filename: str = "terminal.png") -> str:
    """Render terminal-style text output as a tall PNG (long screenshot for SSH)."""
    lines = text.split("\n")
    font = _default_font(13)
    line_height = 18
    char_width = 8  # approximate for monospace

    max_line_len = max((len(line) for line in lines), default=80)
    img_width = max(800, char_width * max_line_len + 40)
    img_height = max(600, line_height * len(lines) + 40)

    img = Image.new("RGB", (img_width, img_height), (15, 15, 15))
    draw = ImageDraw.Draw(img)

    y = 10
    for line in lines:
        draw.text((10, y), line, fill=(200, 200, 200), font=font)
        y += line_height

    path = os.path.join(output_dir, filename)
    img.save(path, "PNG")
    return path
