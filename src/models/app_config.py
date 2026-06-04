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

    # --- Timeouts ---
    tcp_connect_timeout: float = 5.0
    ssh_command_timeout: float = 60.0
    ssh_idle_timeout: float = 5.0
    bmc_page_timeout: float = 60.0

    # --- Preflight ---
    preflight_enabled: bool = True
    route_guard_enabled: bool = True
    route_guard_check_interval: float = 30.0

    # --- Output ---
    output_root: str = "./output"

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    def apply_cli_overrides(
        self,
        *,
        output_root: str | None = None,
        max_bmc_workers: int | None = None,
        max_ssh_workers: int | None = None,
        ssh_command_timeout: float | None = None,
        ssh_idle_timeout: float | None = None,
        bmc_page_timeout: float | None = None,
        no_preflight: bool = False,
    ) -> list[str]:
        """Apply CLI overrides on top of YAML config.
        Returns list of human-readable change descriptions.
        """
        changes: list[str] = []
        if output_root is not None:
            self.output_root = output_root
            changes.append(f"output_root = {output_root} (CLI)")
        if max_bmc_workers is not None:
            self.max_bmc_workers = max_bmc_workers
            changes.append(f"max_bmc_workers = {max_bmc_workers} (CLI)")
        if max_ssh_workers is not None:
            self.max_ssh_workers = max_ssh_workers
            changes.append(f"max_ssh_workers = {max_ssh_workers} (CLI)")
        if ssh_command_timeout is not None:
            self.ssh_command_timeout = ssh_command_timeout
            changes.append(f"ssh_command_timeout = {ssh_command_timeout} (CLI)")
        if ssh_idle_timeout is not None:
            self.ssh_idle_timeout = ssh_idle_timeout
            changes.append(f"ssh_idle_timeout = {ssh_idle_timeout} (CLI)")
        if bmc_page_timeout is not None:
            self.bmc_page_timeout = bmc_page_timeout
            changes.append(f"bmc_page_timeout = {bmc_page_timeout} (CLI)")
        if no_preflight:
            self.preflight_enabled = False
            changes.append("preflight_enabled = false (CLI --no-preflight)")
        return changes

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
            ssh_command_timeout=float(data.get("ssh_command_timeout", 60.0)),
            ssh_idle_timeout=float(data.get("ssh_idle_timeout", 5.0)),
            bmc_page_timeout=float(data.get("bmc_page_timeout", 60.0)),
            preflight_enabled=bool(data.get("preflight_enabled", True)),
            route_guard_enabled=bool(data.get("route_guard_enabled", True)),
            route_guard_check_interval=float(data.get("route_guard_check_interval", 30.0)),
            output_root=str(data.get("output_root", "./output")),
            api_host=str(data.get("api_host", "0.0.0.0")),
            api_port=int(data.get("api_port", 8080)),
        )
