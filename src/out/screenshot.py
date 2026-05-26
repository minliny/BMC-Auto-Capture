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
    """Add an info bar to the top of a screenshot. Returns the new file path."""
    img = Image.open(image_path)
    width, height = img.size

    bar_height = 80
    canvas = Image.new("RGBA", (width, height + bar_height), (30, 30, 30, 255))
    canvas.paste(img, (0, bar_height))

    draw = ImageDraw.Draw(canvas)
    font = _default_font(14)
    font_small = _default_font(11)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"Device: {device_name}  |  IP: {device_ip}",
        f"Task: {task_name}",
    ]
    if page_url:
        lines.append(f"URL: {page_url}")
    if page_title:
        lines.append(f"Title: {page_title}")

    y = 4
    for line in lines:
        draw.text((8, y), line, fill=(255, 255, 255, 255), font=font)
        y += 18

    # Timestamp on the right side
    draw.text((width - 200, 4), timestamp, fill=(180, 180, 180, 255), font=font_small)

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
