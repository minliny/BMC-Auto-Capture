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
    """Overlay device/task watermark at the top-left of the screenshot."""
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    font = _default_font(15)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Semi-transparent overlay at top-left
    lines = [
        f"{device_name}  |  {device_ip}",
        f"Task: {task_name}",
        timestamp,
    ]
    if page_url:
        lines.append(page_url[:100])

    # Draw text with dark shadow for readability
    x, y = 10, 10
    for line in lines:
        # Shadow
        draw.text((x + 1, y + 1), line, fill=(0, 0, 0), font=font)
        # White text
        draw.text((x, y), line, fill=(255, 255, 255), font=font)
        y += 22

    out_path = image_path.replace(".png", "_annotated.png")
    img.save(out_path, "PNG")
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
