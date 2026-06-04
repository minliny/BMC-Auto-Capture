from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.out.addressbar import (  # noqa: E402
    addressbar_height_for_width,
    image2_template_height_for_width,
    normalize_bmc_addressbar_url,
    render_chrome_addressbar,
    render_image2_template_addressbar,
)


def test_normalize_bmc_addressbar_url_accepts_only_current_bmc_host():
    assert (
        normalize_bmc_addressbar_url(
            "/UI/Static/#/navigate/system/storage",
            "192.168.1.10",
        )
        == "https://192.168.1.10/UI/Static/#/navigate/system/storage"
    )
    assert (
        normalize_bmc_addressbar_url(
            "http://192.168.1.10/UI/Static/#/navigate/system/info/memory",
            "192.168.1.10",
        )
        == "https://192.168.1.10/UI/Static/#/navigate/system/info/memory"
    )
    assert normalize_bmc_addressbar_url("https://example.com/UI/Static/", "192.168.1.10") == ""


def test_render_chrome_addressbar_adds_bar_and_replaces_existing(tmp_path: Path):
    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (800, 480), (20, 80, 120)).save(source)

    stripped_first = render_chrome_addressbar(
        source,
        output,
        "https://192.168.1.10/UI/Static/#/navigate/system/storage",
    )

    assert stripped_first is False
    with Image.open(output) as img:
        assert img.size == (800, 480 + addressbar_height_for_width(800))

    stripped_second = render_chrome_addressbar(
        output,
        second,
        "https://192.168.1.10/UI/Static/#/navigate/system/info/memory",
    )

    assert stripped_second is True
    with Image.open(second) as img:
        assert img.size == (800, 480 + addressbar_height_for_width(800))


def test_render_chrome_addressbar_scales_to_screenshot_width(tmp_path: Path):
    narrow_source = tmp_path / "narrow.png"
    narrow_output = tmp_path / "narrow_out.png"
    wide_source = tmp_path / "wide.png"
    wide_output = tmp_path / "wide_out.png"
    Image.new("RGB", (420, 300), (20, 80, 120)).save(narrow_source)
    Image.new("RGB", (1920, 1080), (20, 80, 120)).save(wide_source)

    render_chrome_addressbar(narrow_source, narrow_output, "https://192.168.1.10/UI/Static/#/navigate/system/storage")
    render_chrome_addressbar(wide_source, wide_output, "https://192.168.1.10/UI/Static/#/navigate/system/storage")

    with Image.open(narrow_output) as img:
        assert img.size == (420, 300 + addressbar_height_for_width(420))
    with Image.open(wide_output) as img:
        assert img.size == (1920, 1080 + addressbar_height_for_width(1920))
    assert addressbar_height_for_width(420) < addressbar_height_for_width(1920)


def test_render_image2_template_addressbar_scales_to_screenshot_width(tmp_path: Path):
    narrow_source = tmp_path / "narrow.png"
    narrow_output = tmp_path / "narrow_out.png"
    wide_source = tmp_path / "wide.png"
    wide_output = tmp_path / "wide_out.png"
    Image.new("RGB", (640, 360), (20, 80, 120)).save(narrow_source)
    Image.new("RGB", (1920, 1080), (20, 80, 120)).save(wide_source)

    render_image2_template_addressbar(
        narrow_source,
        narrow_output,
        "https://192.168.1.10/UI/Static/#/navigate/system/storage",
    )
    render_image2_template_addressbar(
        wide_source,
        wide_output,
        "https://192.168.1.10/UI/Static/#/navigate/system/storage",
    )

    with Image.open(narrow_output) as img:
        assert img.size == (640, 360 + image2_template_height_for_width(640))
    with Image.open(wide_output) as img:
        assert img.size == (1920, 1080 + image2_template_height_for_width(1920))
    assert image2_template_height_for_width(640) < image2_template_height_for_width(1920)
