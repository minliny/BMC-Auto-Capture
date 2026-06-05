"""
Chrome/Edge-style address bar compositing for BMC screenshots.

Two rendering paths:
  - final_svg (DEFAULT): uses tracked final-stage SVG assets, renders via Playwright
  - legacy_pillow: old Pillow hand-drawn address bar (debug/fallback only)
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont


# --- Final SVG asset paths ---
def _resolve_asset_root() -> Path:
    """Resolve the project root for assets, works in dev and frozen modes."""
    if getattr(sys, "frozen", False):
        # Frozen: exe is at runtime/bmc-engine.exe, assets at ../app/assets/
        return Path(sys.executable).resolve().parent.parent / "app"
    else:
        # Dev: __file__ is src/out/addressbar.py, project root = parents[2]
        return Path(__file__).resolve().parents[2]

_FINAL_SVG_DIR = _resolve_asset_root() / "assets" / "addressbar" / "final_stage_addressbar"

_FINAL_SVG_TEMPLATES = {
    "16:9":  _FINAL_SVG_DIR / "final_addressbar_source_16x9_1920x1080.svg",
    "16:10": _FINAL_SVG_DIR / "final_addressbar_source_16x10_1920x1200.svg",
    "21:9":  _FINAL_SVG_DIR / "final_addressbar_source_21x9_2560x1080.svg",
}


BAR_HEIGHT = 92
MIN_BAR_HEIGHT = 64
MAX_BAR_HEIGHT = 108
IMAGE2_TEMPLATE_HEIGHT = 126
IMAGE2_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "addressbar"
    / "image2_addressbar_template.png"
)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for item in _FONT_CANDIDATES:
        try:
            path = Path(item)
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _red_pixel_ratio(img: Image.Image, x: int, y: int, w: int, h: int) -> float:
    total = 0
    red = 0
    for yy in range(max(0, y), min(img.height, y + h)):
        for xx in range(max(0, x), min(img.width, x + w)):
            r, g, b = img.getpixel((xx, yy))[:3]
            total += 1
            if r > 150 and g < 120 and b < 120:
                red += 1
    return red / total if total else 0.0


def addressbar_height_for_width(width: int) -> int:
    """Scale the synthetic browser chrome to the screenshot width."""
    if width <= 0:
        return BAR_HEIGHT
    return max(MIN_BAR_HEIGHT, min(MAX_BAR_HEIGHT, round(width * 0.072)))


def image2_template_height_for_width(width: int) -> int:
    """Scale the image-2 template address bar while keeping it usable on small screenshots."""
    if width <= 0:
        return IMAGE2_TEMPLATE_HEIGHT
    return max(70, min(132, round(width * (IMAGE2_TEMPLATE_HEIGHT / 1672))))


def _scaled(value: int | float, scale: float) -> int:
    return max(1, round(value * scale))


def looks_like_composited_addressbar(img: Image.Image) -> bool:
    """Detect this module's own address bar to avoid stacking two bars."""
    bar_height = addressbar_height_for_width(img.width)
    if img.width < 240 or img.height < bar_height + 80:
        return False

    # The synthetic bar always has a red shield near the left side of the URL pill.
    scale = bar_height / BAR_HEIGHT
    return _red_pixel_ratio(
        img.convert("RGB"),
        _scaled(128, scale),
        _scaled(50, scale),
        _scaled(28, scale),
        _scaled(30, scale),
    ) > 0.2


def _draw_back(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple[int, int, int], scale: float) -> None:
    draw.line(
        [
            (cx + _scaled(5, scale), cy - _scaled(7, scale)),
            (cx - _scaled(4, scale), cy),
            (cx + _scaled(5, scale), cy + _scaled(7, scale)),
        ],
        fill=color,
        width=_scaled(2, scale),
    )


def _draw_forward(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple[int, int, int], scale: float) -> None:
    draw.line(
        [
            (cx - _scaled(5, scale), cy - _scaled(7, scale)),
            (cx + _scaled(4, scale), cy),
            (cx - _scaled(5, scale), cy + _scaled(7, scale)),
        ],
        fill=color,
        width=_scaled(2, scale),
    )


def _draw_refresh(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple[int, int, int], scale: float) -> None:
    radius = _scaled(9, scale)
    draw.arc((cx - radius, cy - radius, cx + radius, cy + radius), 35, 315, fill=color, width=_scaled(2, scale))
    draw.polygon(
        [
            (cx + _scaled(8, scale), cy - _scaled(9, scale)),
            (cx + _scaled(13, scale), cy - _scaled(7, scale)),
            (cx + _scaled(8, scale), cy - _scaled(2, scale)),
        ],
        fill=color,
    )


def _draw_star(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple[int, int, int], scale: float) -> None:
    points = []
    for index in range(10):
        radius = (8 if index % 2 == 0 else 3.5) * scale
        angle = -math.pi / 2 + index * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    draw.line(points + [points[0]], fill=color, width=max(1, round(scale)))


def _draw_shield(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float) -> None:
    red = (202, 48, 48)
    draw.rounded_rectangle(
        (cx - _scaled(10, scale), cy - _scaled(12, scale), cx + _scaled(10, scale), cy + _scaled(12, scale)),
        radius=_scaled(5, scale),
        fill=red,
    )
    draw.polygon(
        [
            (cx - _scaled(6, scale), cy - _scaled(6, scale)),
            (cx, cy - _scaled(10, scale)),
            (cx + _scaled(6, scale), cy - _scaled(6, scale)),
            (cx + _scaled(4, scale), cy + _scaled(7, scale)),
            (cx, cy + _scaled(10, scale)),
            (cx - _scaled(4, scale), cy + _scaled(7, scale)),
        ],
        fill=(255, 255, 255),
    )


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text

    ellipsis = "..."
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid] + ellipsis
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis if low > 0 else ellipsis


def _resize_addressbar_template(template: Image.Image, width: int, height: int) -> Image.Image:
    """Resize the template with a simple 3-slice stretch to preserve edge details."""
    if template.width == width and template.height == height:
        return template.copy()

    left_w = min(420, template.width // 3)
    right_w = min(360, template.width // 4)
    center_w = max(1, template.width - left_w - right_w)
    out_left_w = min(round(left_w * height / template.height), max(1, width // 3))
    out_right_w = min(round(right_w * height / template.height), max(1, width // 4))
    out_center_w = max(1, width - out_left_w - out_right_w)

    left = template.crop((0, 0, left_w, template.height)).resize((out_left_w, height), Image.Resampling.LANCZOS)
    center = template.crop((left_w, 0, left_w + center_w, template.height)).resize((out_center_w, height), Image.Resampling.BICUBIC)
    right = template.crop((template.width - right_w, 0, template.width, template.height)).resize((out_right_w, height), Image.Resampling.LANCZOS)

    output = Image.new("RGB", (width, height), (255, 255, 255))
    output.paste(left, (0, 0))
    output.paste(center, (out_left_w, 0))
    output.paste(right, (out_left_w + out_center_w, 0))
    return output


def _draw_image2_style_location_bar(
    draw: ImageDraw.ImageDraw,
    width: int,
    bar_height: int,
    scale: float,
    url: str,
    url_font: ImageFont.ImageFont,
    danger_font: ImageFont.ImageFont,
) -> None:
    """Redraw the whole location bar to remove baked-in image-2 text artifacts."""
    icon = (54, 61, 70)
    text = (48, 56, 68)
    danger = (202, 48, 48)
    pill_fill = (241, 245, 250)
    pill_border = (224, 229, 236)

    # Clear the full second browser row from the generated template. Keeping the
    # tab strip texture but redrawing the controls is more robust than trying to
    # surgically erase generated text.
    row_top = _scaled(60, scale)
    draw.rectangle((0, row_top, width, bar_height), fill=(248, 250, 252))

    cy = _scaled(98, scale)
    _draw_back(draw, _scaled(32, scale), cy, icon, scale)
    _draw_forward(draw, _scaled(96, scale), cy, icon, scale)
    _draw_refresh(draw, _scaled(160, scale), cy, icon, scale)

    url_x1 = _scaled(220, scale)
    url_x2 = width - _scaled(84, scale)
    if url_x2 - url_x1 < _scaled(260, scale):
        url_x1 = _scaled(112, scale)
        url_x2 = width - _scaled(34, scale)

    pill_y1 = _scaled(76, scale)
    pill_y2 = min(bar_height - _scaled(8, scale), _scaled(122, scale))
    draw.rounded_rectangle(
        (url_x1, pill_y1, url_x2, pill_y2),
        radius=_scaled(22, scale),
        fill=pill_fill,
        outline=pill_border,
    )

    shield_x = url_x1 + _scaled(34, scale)
    _draw_shield(draw, shield_x, cy, scale)
    unsafe_x = shield_x + _scaled(28, scale)
    url_text_x = unsafe_x + _scaled(78, scale)
    text_y = pill_y1 + max(1, (pill_y2 - pill_y1 - _scaled(20, scale)) // 2)
    draw.text((unsafe_x, text_y), "不安全", fill=danger, font=danger_font)
    draw.text(
        (url_text_x, text_y),
        _fit_text(draw, url, url_font, max(1, url_x2 - url_text_x - _scaled(48, scale))),
        fill=text,
        font=url_font,
    )
    _draw_star(draw, url_x2 - _scaled(32, scale), cy, icon, scale)
    draw.text((width - _scaled(42, scale), _scaled(80, scale)), "...", fill=icon, font=_load_font(_scaled(24, scale)))


def normalize_bmc_addressbar_url(raw_url: str, bmc_ip: str) -> str:
    """Return a display URL only when it belongs to the current BMC host."""
    raw_url = str(raw_url or "").strip()
    bmc_ip = str(bmc_ip or "").strip()
    if not raw_url or not bmc_ip:
        return ""

    if raw_url.startswith("/"):
        return f"https://{bmc_ip}{raw_url}"

    parsed = urlparse(raw_url)
    if parsed.scheme in ("http", "https") and parsed.hostname == bmc_ip:
        path = parsed.path or ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        query = f"?{parsed.query}" if parsed.query else ""
        return f"https://{bmc_ip}{path}{query}{fragment}"

    return ""


def render_chrome_addressbar(
    source_png: str | Path,
    output_png: str | Path,
    url: str,
    title: str = "BMC Web Console",
    strip_existing_bar: bool = True,
) -> bool:
    """Composite a browser address bar above a screenshot.

    Returns True when an existing synthetic bar was detected and stripped.
    """
    source_png = Path(source_png)
    output_png = Path(output_png)

    image = Image.open(source_png).convert("RGB")
    bar_height = addressbar_height_for_width(image.width)
    scale = bar_height / BAR_HEIGHT
    stripped = False
    if strip_existing_bar and looks_like_composited_addressbar(image):
        image = image.crop((0, bar_height, image.width, image.height))
        stripped = True

    width, height = image.size
    output = Image.new("RGB", (width, height + bar_height), (255, 255, 255))
    draw = ImageDraw.Draw(output)

    tab_bg = (241, 243, 244)
    tab_active = (255, 255, 255)
    border = (218, 220, 224)
    text = (60, 64, 67)
    icon = (95, 99, 104)
    danger = (202, 48, 48)

    title_font = _load_font(_scaled(13, scale))
    url_font = _load_font(_scaled(13, scale))
    bold_font = _load_font(_scaled(13, scale))
    close_font = _load_font(_scaled(15, scale))
    menu_font = _load_font(_scaled(22, scale))

    tab_bar_h = _scaled(36, scale)
    draw.rectangle((0, 0, width, tab_bar_h), fill=tab_bg)
    tab_width = min(_scaled(390, scale), max(_scaled(170, scale), width // 3))
    draw.rounded_rectangle(
        (_scaled(8, scale), _scaled(5, scale), tab_width, tab_bar_h),
        radius=_scaled(10, scale),
        fill=tab_active,
        outline=border,
    )
    title_x = _scaled(38, scale)
    title_y = _scaled(13, scale)
    title_max_width = max(1, tab_width - title_x - _scaled(34, scale))
    draw.text((title_x, title_y), _fit_text(draw, title, title_font, title_max_width), fill=text, font=title_font)
    draw.text((tab_width - _scaled(28, scale), _scaled(11, scale)), "x", fill=icon, font=close_font)

    cy = _scaled(64, scale)
    draw.rectangle((0, tab_bar_h, width, bar_height), fill=(255, 255, 255))
    _draw_back(draw, _scaled(24, scale), cy, icon, scale)
    _draw_forward(draw, _scaled(56, scale), cy, icon, scale)
    _draw_refresh(draw, _scaled(88, scale), cy, icon, scale)

    url_x1 = _scaled(122, scale)
    url_x2 = max(url_x1 + _scaled(180, scale), width - _scaled(78, scale))
    draw.rounded_rectangle(
        (url_x1, _scaled(45, scale), url_x2, _scaled(82, scale)),
        radius=_scaled(18, scale),
        fill=(255, 255, 255),
        outline=border,
    )

    _draw_shield(draw, url_x1 + _scaled(18, scale), cy, scale)
    unsafe_x = url_x1 + _scaled(36, scale)
    url_text_x = url_x1 + _scaled(90, scale)
    text_y = _scaled(55, scale)
    draw.text((unsafe_x, text_y), "不安全", fill=danger, font=bold_font)
    url_max_width = max(1, url_x2 - url_text_x - _scaled(46, scale))
    draw.text((url_text_x, text_y), _fit_text(draw, url, url_font, url_max_width), fill=text, font=url_font)

    _draw_star(draw, url_x2 - _scaled(24, scale), cy, icon, scale)
    draw.text((width - _scaled(35, scale), _scaled(50, scale)), "...", fill=icon, font=menu_font)
    draw.line((0, bar_height - 1, width, bar_height - 1), fill=border)

    output.paste(image, (0, bar_height))
    output_png.parent.mkdir(parents=True, exist_ok=True)
    output.save(output_png, "PNG")
    return stripped


def render_image2_template_addressbar(
    source_png: str | Path,
    output_png: str | Path,
    url: str,
    title: str = "BMC Web Console",
    template_png: str | Path = IMAGE2_TEMPLATE_PATH,
    strip_existing_bar: bool = True,
) -> bool:
    """Composite a screenshot using the image-2 address bar template.

    The template supplies the generated visual style; URL/title text is redrawn
    deterministically so evidence remains readable and repeatable.
    """
    source_png = Path(source_png)
    output_png = Path(output_png)
    template_png = Path(template_png)

    if not template_png.exists():
        return render_chrome_addressbar(source_png, output_png, url, title, strip_existing_bar)

    image = Image.open(source_png).convert("RGB")
    bar_height = image2_template_height_for_width(image.width)
    stripped = False
    if strip_existing_bar:
        synthetic_h = addressbar_height_for_width(image.width)
        if looks_like_composited_addressbar(image):
            image = image.crop((0, synthetic_h, image.width, image.height))
            stripped = True
        elif image.height > bar_height + 80:
            # Image-2 template has a red unsafe marker in roughly this zone.
            scale_probe = bar_height / IMAGE2_TEMPLATE_HEIGHT
            red_ratio = _red_pixel_ratio(
                image,
                _scaled(300, scale_probe),
                _scaled(62, scale_probe),
                _scaled(42, scale_probe),
                _scaled(42, scale_probe),
            )
            if red_ratio > 0.08:
                image = image.crop((0, bar_height, image.width, image.height))
                stripped = True

    width, height = image.size
    template = Image.open(template_png).convert("RGB")
    bar = _resize_addressbar_template(template, width, bar_height)
    draw = ImageDraw.Draw(bar)

    scale = bar_height / IMAGE2_TEMPLATE_HEIGHT
    text = (48, 56, 68)
    danger = (196, 52, 52)
    title_font = _load_font(_scaled(17, scale))
    url_font = _load_font(_scaled(17, scale))
    danger_font = _load_font(_scaled(16, scale))

    # Repaint generated text zones. These coordinates are based on the template
    # crop and are scaled with the template height.
    title_x = _scaled(90, scale)
    title_y = _scaled(19, scale)
    title_max_width = max(1, _scaled(330, scale))
    draw.rounded_rectangle(
        (title_x - _scaled(8, scale), title_y - _scaled(5, scale), title_x + title_max_width, title_y + _scaled(25, scale)),
        radius=_scaled(7, scale),
        fill=(247, 249, 252),
    )
    draw.text((title_x, title_y), _fit_text(draw, title, title_font, title_max_width), fill=text, font=title_font)

    _draw_image2_style_location_bar(draw, width, bar_height, scale, url, url_font, danger_font)

    output = Image.new("RGB", (width, height + bar_height), (255, 255, 255))
    output.paste(bar, (0, 0))
    output.paste(image, (0, bar_height))
    output_png.parent.mkdir(parents=True, exist_ok=True)
    output.save(output_png, "PNG")
    return stripped


# ---------------------------------------------------------------------------
# Final SVG address bar (default rendering path)
# ---------------------------------------------------------------------------

# In-memory cache: svg_path → rendered PNG bytes (keyed by (path, width))
_svg_render_cache: dict[tuple[str, int], bytes] = {}


def _select_svg_template(image_width: int, image_height: int) -> Path:
    """Select the closest final SVG address bar template by aspect ratio."""
    if image_width <= 0 or image_height <= 0:
        return _FINAL_SVG_TEMPLATES["16:9"]

    ratio = image_width / image_height
    # Thresholds between standard ratios
    ratios = {
        "21:9": 21/9,
        "16:9": 16/9,
        "16:10": 16/10,
    }
    best = min(ratios.items(), key=lambda kv: abs(kv[1] - ratio))
    return _FINAL_SVG_TEMPLATES[best[0]]


def _svg_template_ratio_name(svg_path: Path) -> str:
    for name, path in _FINAL_SVG_TEMPLATES.items():
        if path.resolve() == svg_path.resolve():
            return name
    return "nearest"


def _svg_to_png(svg_path: str, output_width: int) -> bytes | None:
    """Render an SVG to PNG bytes using the Playwright sync API.

    Returns PNG bytes on success, None on failure.
    """
    cache_key = (svg_path, output_width)
    if cache_key in _svg_render_cache:
        return _svg_render_cache[cache_key]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": output_width, "height": 200})

            # Load SVG file
            abs_path = os.path.abspath(svg_path)
            page.goto(f"file://{abs_path}", wait_until="load")

            # Get the SVG element's natural height
            svg_height = page.evaluate("""() => {
                const svg = document.querySelector('svg');
                if (!svg) return 126;
                const box = svg.getBoundingClientRect();
                return box.height || 126;
            }""")

            # Screenshot the SVG area
            png_bytes = page.screenshot(
                clip={"x": 0, "y": 0, "width": output_width, "height": svg_height},
                full_page=False,
            )
            browser.close()

            _svg_render_cache[cache_key] = png_bytes
            return png_bytes

    except Exception as e:
        import logging
        logging.getLogger("bmc_auto_capture.addressbar").warning(
            "Failed to render SVG via Playwright: %s", e
        )
        return None


def render_final_addressbar(
    source_png: str | Path,
    output_png: str | Path,
    url: str,
    title: str = "iBMC",
    strip_existing_bar: bool = True,
) -> dict:
    """Composite final SVG address bar above a BMC screenshot.

    Returns metadata dict with keys:
      addressbar_source, addressbar_template, addressbar_ratio, addressbar_legacy_used
    """
    source_png = Path(source_png)
    output_png = Path(output_png)

    meta = {
        "addressbar_source": "final_svg",
        "addressbar_template": "",
        "addressbar_ratio": "",
        "addressbar_legacy_used": False,
    }

    image = Image.open(source_png).convert("RGB")
    width, height = image.size

    # Strip existing bar if detected
    if strip_existing_bar and looks_like_composited_addressbar(image):
        bar_h = addressbar_height_for_width(width)
        image = image.crop((0, bar_h, image.width, image.height))
        width, height = image.size

    # Select template by aspect ratio
    svg_path = _select_svg_template(width, height)
    ratio_name = _svg_template_ratio_name(svg_path)
    meta["addressbar_template"] = svg_path.name
    meta["addressbar_ratio"] = ratio_name

    # Render SVG to PNG
    png_bytes = _svg_to_png(str(svg_path), width)
    if png_bytes is None:
        raise RuntimeError(
            f"Failed to render final SVG address bar: {svg_path}. "
            f"No silent fallback — check Playwright/chromium installation."
        )

    # Load rendered address bar as PIL Image
    import io
    bar_img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bar_height = bar_img.height

    # Composite: address bar on top, screenshot below
    output = Image.new("RGB", (width, height + bar_height), (255, 255, 255))
    output.paste(bar_img, (0, 0))
    output.paste(image, (0, bar_height))
    output_png.parent.mkdir(parents=True, exist_ok=True)
    output.save(output_png, "PNG")

    return meta
