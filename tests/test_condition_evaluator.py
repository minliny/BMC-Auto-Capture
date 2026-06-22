from __future__ import annotations

import asyncio

from src.rules.condition_evaluator import evaluate_ready_conditions, parse_ready_specs


class _FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        visible: bool = True,
        enabled: bool = True,
        attrs: dict[str, str] | None = None,
    ):
        self._text = text
        self._visible = visible
        self._enabled = enabled
        self._attrs = attrs or {}

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def inner_text(self):
        return self._text

    async def text_content(self):
        return self._text

    async def get_attribute(self, name: str):
        return self._attrs.get(name, "")


class _FakePage:
    def __init__(self, *, body: str = "", url: str = "https://bmc/UI/Static/#/navigate/system"):
        self.url = url
        self.body = body
        self.elements: dict[str, list[_FakeElement]] = {}

    def add(self, selector: str, *elements: _FakeElement):
        self.elements[selector] = list(elements)
        return self

    def is_closed(self):
        return False

    async def query_selector(self, selector: str):
        matches = self.elements.get(selector, [])
        return matches[0] if matches else None

    async def query_selector_all(self, selector: str):
        return self.elements.get(selector, [])

    async def inner_text(self, selector: str):
        if selector == "body":
            return self.body
        element = await self.query_selector(selector)
        return await element.inner_text() if element else ""


def _run_ready(raw_specs, page):
    specs = parse_ready_specs(raw_specs)
    return asyncio.run(evaluate_ready_conditions(page, specs, protocol="BMC"))


def test_ready_conditions_support_text_nonempty_and_placeholder_rejection():
    page = (
        _FakePage(body="Logical Drive 0 OK")
        .add(".raid-level", _FakeElement("RAID 1"))
        .add(".status", _FakeElement("Optimal"))
        .add(".capacity", _FakeElement("--"))
    )

    result = _run_ready([
        {"type": "text_nonempty", "selectors": [".raid-level", ".status", ".capacity"]},
        {
            "type": "text_not_in",
            "selectors": [".raid-level", ".status", ".capacity"],
            "values": ["", "--", "N/A", "Loading"],
        },
    ], page)

    assert result.results[0].status == "PASS"
    assert result.results[1].status == "FAIL"
    assert ".capacity" in result.results[1].details


def test_ready_conditions_support_selector_count_threshold():
    page = _FakePage().add(
        ".data-row",
        _FakeElement("row 1"),
        _FakeElement("row 2"),
    )

    result = _run_ready([
        {"type": "count_ge", "selector": ".data-row", "min_count": 2},
        {"type": "selector_count_ge", "selector": ".data-row", "min_count": 3},
    ], page)

    assert result.results[0].status == "PASS"
    assert result.results[1].status == "FAIL"
    assert "count 2 < 3" in result.results[1].details


def test_ready_conditions_support_region_stable():
    page = _FakePage(body="stable dashboard text").add(
        ".dashboard-main",
        _FakeElement("stable dashboard text"),
    )

    result = _run_ready([
        {
            "type": "region_stable",
            "selector": ".dashboard-main",
            "stable_for_ms": 20,
            "sample_interval_ms": 5,
            "timeout_ms": 100,
        },
    ], page)

    assert result.results[0].status == "PASS"
    assert "stable_for_ms=20" in result.results[0].details


def test_ready_region_stable_respects_timeout():
    page = _FakePage(body="stable dashboard text").add(
        ".dashboard-main",
        _FakeElement("stable dashboard text"),
    )

    result = _run_ready([
        {
            "type": "region_stable",
            "selector": ".dashboard-main",
            "stable_for_ms": 500,
            "sample_interval_ms": 5,
            "timeout_ms": 50,
        },
    ], page)

    assert result.results[0].status == "FAIL"
    assert "not stable" in result.results[0].details


def test_ready_conditions_support_action_state_checks():
    page = (
        _FakePage(body="CPU active details ready")
        .add("#tab-cpu", _FakeElement("CPU", attrs={"class": "el-tabs__item is-active"}))
        .add("#details", _FakeElement("CPU active details ready"))
    )

    result = _run_ready([
        {"type": "active_tab_changed", "selector": "#tab-cpu", "values": ["is-active"]},
        {"type": "post_action_state_changed", "selector": "#details", "expected": "active details"},
    ], page)

    assert [r.status for r in result.results] == ["PASS", "PASS"]
