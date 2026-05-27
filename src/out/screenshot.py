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
    """Add a realistic Windows Chrome browser frame to the screenshot."""
    img = Image.open(image_path)
    width, height = img.size

    font = _default_font(12)
    font_sm = _default_font(11)
    font_bold = _default_font(13)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Chrome color scheme (Windows dark theme)
    TITLE_BG = (53, 54, 58)       # Windows title bar
    TAB_BG = (60, 64, 67)         # Tab strip background
    TAB_ACTIVE = (50, 52, 55)     # Active tab
    TEXT_PRIMARY = (232, 234, 237)
    TEXT_SECONDARY = (154, 160, 166)

    # Layout heights
    title_bar_h = 30
    tab_bar_h = 36
    device_bar_h = 24
    chrome_total_h = title_bar_h + tab_bar_h + device_bar_h

    canvas = Image.new("RGBA", (width, height + chrome_total_h), TITLE_BG)
    draw = ImageDraw.Draw(canvas)

    # ====== Title bar ======
    draw.rectangle([(0, 0), (width, title_bar_h)], fill=TITLE_BG)
    # Chrome icon (colored circle)
    draw.ellipse([(10, 6), (24, 20)], fill=(66, 133, 244))
    draw.text((30, 7), "Google Chrome", fill=TEXT_PRIMARY, font=font_sm)

    # Window controls (Windows 10 style)
    btn_area = width - 140
    for bx, symbol, hover in [
        (btn_area, "—", (80, 80, 80)),
        (btn_area + 46, "□", (80, 80, 80)),
        (btn_area + 92, "✕", (232, 17, 35)),
    ]:
        draw.rectangle([(bx, 0), (bx + 46, title_bar_h)], fill=TITLE_BG)
        draw.text((bx + 16, 6), symbol, fill=TEXT_PRIMARY, font=font_bold)

    # ====== Tab strip ======
    y_tab = title_bar_h
    draw.rectangle([(0, y_tab), (width, y_tab + tab_bar_h)], fill=TAB_BG)

    # Active tab
    tab_left = 4
    tab_w = min(width - 180, 260)
    # Tab shape with rounded top
    draw.rectangle([(tab_left, y_tab + 2), (tab_left + tab_w, y_tab + tab_bar_h)], fill=TAB_ACTIVE)
    draw.line([(tab_left, y_tab + 2), (tab_left, y_tab + tab_bar_h)], fill=TAB_ACTIVE, width=1)
    # Top colored accent line for active tab
    draw.line([(tab_left, y_tab), (tab_left + tab_w, y_tab)], fill=(66, 133, 244), width=3)

    tab_text = page_title[:60] if page_title else "BMC Management"
    draw.text((tab_left + 24, y_tab + 10), tab_text, fill=TEXT_PRIMARY, font=font_sm)
    # Favicon-like dot
    draw.ellipse([(tab_left + 8, y_tab + 13), (tab_left + 17, y_tab + 22)], fill=(100, 180, 100))

    # New tab button (+)
    ntx = tab_left + tab_w + 8
    draw.rectangle([(ntx, y_tab + 6), (ntx + 24, y_tab + tab_bar_h - 6)], fill=TAB_BG)
    draw.text((ntx + 7, y_tab + 8), "+", fill=TEXT_SECONDARY, font=font_bold)

    # ====== Device info bar ======
    y_dev = y_tab + tab_bar_h
    draw.rectangle([(0, y_dev), (width, y_dev + device_bar_h)], fill=(40, 42, 45))
    info = f"Device: {device_name}  |  IP: {device_ip}  |  Task: {task_name}"
    draw.text((12, y_dev + 4), info, fill=TEXT_SECONDARY, font=font_sm)
    draw.text((width - 195, y_dev + 4), timestamp, fill=(120, 125, 130), font=font_sm)

    # ====== Paste page screenshot ======
    canvas.paste(img, (0, chrome_total_h))

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
