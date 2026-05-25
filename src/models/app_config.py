"""
AppConfig — YAML-driven configuration with sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass
class AppConfig:
    # --- Worker pools ---
    max_bmc_workers: int = 4
    max_ssh_workers: int = 8
    base_bmc_workers: int = 2
    base_ssh_workers: int = 4

    # --- Resource thresholds ---
    cpu_scale_down_pct: float = 90.0
    mem_scale_down_pct: float = 85.0
    cpu_scale_up_pct: float = 60.0
    mem_scale_up_pct: float = 50.0
    cpu_emergency_pct: float = 95.0
    mem_emergency_pct: float = 92.0
    resource_check_interval: float = 5.0

    # --- Browser lifecycle ---
    browser_max_tasks_before_recycle: int = 50
    browser_max_age_seconds: int = 1800
    browser_headless: bool = True

    # --- Preflight ---
    tcp_connect_timeout: float = 5.0
    preflight_enabled: bool = True
    route_guard_enabled: bool = True
    route_guard_check_interval: float = 30.0

    # --- Output ---
    output_root: str = "./output"

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(
            max_bmc_workers=int(data.get("max_bmc_workers", 4)),
            max_ssh_workers=int(data.get("max_ssh_workers", 8)),
            base_bmc_workers=int(data.get("base_bmc_workers", 2)),
            base_ssh_workers=int(data.get("base_ssh_workers", 4)),
            cpu_scale_down_pct=float(data.get("cpu_scale_down_pct", 90.0)),
            mem_scale_down_pct=float(data.get("mem_scale_down_pct", 85.0)),
            cpu_scale_up_pct=float(data.get("cpu_scale_up_pct", 60.0)),
            mem_scale_up_pct=float(data.get("mem_scale_up_pct", 50.0)),
            cpu_emergency_pct=float(data.get("cpu_emergency_pct", 95.0)),
            mem_emergency_pct=float(data.get("mem_emergency_pct", 92.0)),
            resource_check_interval=float(data.get("resource_check_interval", 5.0)),
            browser_max_tasks_before_recycle=int(data.get("browser_max_tasks_before_recycle", 50)),
            browser_max_age_seconds=int(data.get("browser_max_age_seconds", 1800)),
            browser_headless=bool(data.get("browser_headless", True)),
            tcp_connect_timeout=float(data.get("tcp_connect_timeout", 5.0)),
            preflight_enabled=bool(data.get("preflight_enabled", True)),
            route_guard_enabled=bool(data.get("route_guard_enabled", True)),
            route_guard_check_interval=float(data.get("route_guard_check_interval", 30.0)),
            output_root=str(data.get("output_root", "./output")),
            api_host=str(data.get("api_host", "0.0.0.0")),
            api_port=int(data.get("api_port", 8080)),
        )
