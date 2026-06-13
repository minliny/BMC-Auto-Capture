from __future__ import annotations

from src.scheduler.dynamic_scheduler import DynamicScheduler


def test_dynamic_target_uses_max_workers_as_real_cap_when_resources_ok():
    assert DynamicScheduler._compute_target_size(
        base_workers=2,
        max_workers=20,
        demand=15,
        scale=1.0,
    ) == 15


def test_dynamic_target_is_limited_by_max_workers():
    assert DynamicScheduler._compute_target_size(
        base_workers=2,
        max_workers=8,
        demand=15,
        scale=1.3,
    ) == 8


def test_dynamic_target_scales_down_under_resource_pressure():
    assert DynamicScheduler._compute_target_size(
        base_workers=4,
        max_workers=10,
        demand=10,
        scale=0.3,
    ) == 3
