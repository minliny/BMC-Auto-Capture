"""Stability & consistency cleanup — STABILITY_CLEANUP_001.

Tests for 5 defects fixed in this batch:
  1. _on_bmc_group_done undefined variables (status_icon/result)
  2. Version consistency across pyproject.toml / __main__.py / config,
     while README stays version-agnostic
  3. _compute_scale configurable scaling coefficients
  4. Popup dismiss selector independent timeout
  5. Output dir writable atomic detection (TOCTOU)

Run:  python -m pytest tests/test_stability_cleanup_001.py -v
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.executor.bmc_executor import BMCExecutor, POPUP_DISMISS_SELECTORS
from src.executor.browser_manager import BrowserManager

# ======================================================================
# Fix 1 — _on_bmc_group_done undefined variables
# ======================================================================


class TestGroupCallbackNoNameError:
    """Verify _on_bmc_group_done does not reference undefined variables."""

    def test_group_callback_no_name_error(self):
        """_on_bmc_group_done must not raise NameError from undefined status_icon/result."""
        config = AppConfig()
        scheduler = DynamicScheduler(config)

        # _on_bmc_group_done takes (results, endpoint_key)
        # It should not reference status_icon, result, or reason as bare variables.
        try:
            scheduler._on_bmc_group_done([], "BMC:10.0.0.1:443")
        except NameError as e:
            pytest.fail(f"_on_bmc_group_done raised NameError: {e}")
        except Exception:
            # Other exceptions (e.g. threading, pool not started) are acceptable
            pass

    def test_group_callback_with_real_results(self):
        """_on_bmc_group_done with actual ExecutionResult list must not fail."""
        from src.models.execution_result import ExecutionResult

        config = AppConfig()
        scheduler = DynamicScheduler(config)

        results = [
            ExecutionResult(
                plan_id="p1",
                device_name="dev1",
                device_group="G1",
                task_name="task1",
                task_type="BMC",
                execution_mode="BMC_URL",
                execution_status="EXEC_SUCCESS",
                started_at=time.time(),
                ended_at=time.time(),
            ),
            ExecutionResult(
                plan_id="p2",
                device_name="dev1",
                device_group="G1",
                task_name="task2",
                task_type="BMC",
                execution_mode="BMC_URL",
                execution_status="EXEC_FAILED",
                execution_failure_reason="Some error",
                started_at=time.time(),
                ended_at=time.time(),
            ),
        ]

        try:
            scheduler._on_bmc_group_done(results, "BMC:10.0.0.1:443")
        except NameError as e:
            pytest.fail(f"_on_bmc_group_done with results raised NameError: {e}")


# ======================================================================
# Fix 2 — Version consistency
# ======================================================================


class TestVersionConsistency:
    """Verify version is consistent across all canonical sources."""

    PROJ = Path(__file__).resolve().parent.parent

    def _read_version_from_pyproject(self) -> str:
        """Extract version from pyproject.toml."""
        path = self.PROJ / "pyproject.toml"
        text = path.read_text("utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert m, "version not found in pyproject.toml"
        return m.group(1)

    def _read_version_from_main(self) -> str:
        """Extract version from __main__.py or shared parser (args.py)."""
        # Check shared parser first (post-refactor: version lives in src/cli/args.py)
        args_path = self.PROJ / "src" / "cli" / "args.py"
        if args_path.exists():
            text = args_path.read_text("utf-8")
            m = re.search(r"BMC Auto-Capture\s+(v?[\w.-]+)", text)
            if m:
                return m.group(1)
        # Fallback: check __main__.py (pre-refactor)
        path = self.PROJ / "src" / "__main__.py"
        text = path.read_text("utf-8")
        m = re.search(r"BMC Auto-Capture\s+(v?[\w.-]+)", text)
        assert m, "version not found in __main__.py or args.py"
        return m.group(1)

    def _read_version_from_config(self) -> str:
        """Extract version from config/default_config.yaml header."""
        path = self.PROJ / "config" / "default_config.yaml"
        text = path.read_text("utf-8")
        m = re.search(r"BMC Auto-Capture\s+(v?[\w.-]+)", text)
        assert m, "version not found in default_config.yaml"
        return m.group(1)

    def test_pyproject_has_version(self):
        v = self._read_version_from_pyproject()
        assert v, "pyproject.toml version is empty"

    def test_main_matches_pyproject(self):
        py_v = self._read_version_from_pyproject()
        main_v = self._read_version_from_main()
        # Normalize: strip leading 'v' if present
        assert py_v == main_v.lstrip("v"), (
            f"Version mismatch: pyproject.toml={py_v}, __main__.py={main_v}"
        )

    def test_config_matches_pyproject(self):
        py_v = self._read_version_from_pyproject()
        cfg_v = self._read_version_from_config()
        assert py_v == cfg_v.lstrip("v"), (
            f"Version mismatch: pyproject.toml={py_v}, config={cfg_v}"
        )

    def test_readme_does_not_pin_release_version(self):
        """Project README should stay stable and not advertise a specific release."""
        path = self.PROJ / "README.md"
        text = path.read_text("utf-8")
        assert re.search(r"^# BMC Auto-Capture\s*$", text, re.MULTILINE), (
            "README.md title must not include a release version"
        )
        forbidden_patterns = [
            r"(?<![\d.])v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?(?![\d.])",
            r"bmc-auto-capture-v",
            r"vX\.X\.X",
            r"\$\{tag\}",
            r"<版本>",
        ]
        for pattern in forbidden_patterns:
            assert not re.search(pattern, text), (
                f"README.md should not pin release versions: {pattern}"
            )


# ======================================================================
# Fix 3 — _compute_scale configurable
# ======================================================================


class TestSchedulerScaleConfig:
    """Verify _compute_scale reads from config instead of hardcoded values."""

    def _make_scheduler(self, **overrides) -> DynamicScheduler:
        config = AppConfig(**overrides)
        return DynamicScheduler(config)

    def test_default_values_match_hardcoded(self):
        """Default config values should match the original hardcoded values."""
        scheduler = self._make_scheduler()
        # Normal resources → scale_up coefficient
        scale = scheduler._compute_scale(50.0, 40.0)
        assert scale == pytest.approx(1.3), f"Expected 1.3, got {scale}"

    def test_emergency_returns_configured_value(self):
        """Emergency threshold returns config.resource_scale_emergency."""
        scheduler = self._make_scheduler(
            cpu_emergency_pct=80.0,
            mem_emergency_pct=80.0,
            resource_scale_emergency=0.15,
        )
        scale = scheduler._compute_scale(90.0, 50.0)
        assert scale == pytest.approx(0.15), f"Expected 0.15, got {scale}"

    def test_scale_down_returns_configured_value(self):
        """Scale-down threshold returns config.resource_scale_down."""
        scheduler = self._make_scheduler(
            cpu_emergency_pct=95.0,
            mem_emergency_pct=95.0,
            cpu_scale_down_pct=80.0,
            mem_scale_down_pct=80.0,
            resource_scale_down=0.5,
        )
        scale = scheduler._compute_scale(85.0, 50.0)
        assert scale == pytest.approx(0.5), f"Expected 0.5, got {scale}"

    def test_scale_up_returns_configured_value(self):
        """Scale-up threshold returns config.resource_scale_up."""
        scheduler = self._make_scheduler(
            cpu_scale_up_pct=70.0,
            mem_scale_up_pct=70.0,
            resource_scale_up=1.5,
        )
        scale = scheduler._compute_scale(50.0, 40.0)
        assert scale == pytest.approx(1.5), f"Expected 1.5, got {scale}"

    def test_normal_returns_configured_value(self):
        """Normal range returns config.resource_scale_normal."""
        scheduler = self._make_scheduler(
            cpu_scale_up_pct=70.0,
            mem_scale_up_pct=70.0,
            cpu_scale_down_pct=85.0,
            mem_scale_down_pct=85.0,
            resource_scale_normal=1.0,
        )
        scale = scheduler._compute_scale(75.0, 60.0)
        assert scale == pytest.approx(1.0), f"Expected 1.0, got {scale}"

    def test_emergency_overrides_all(self):
        """Emergency should override even if scale_down would also match."""
        scheduler = self._make_scheduler(
            mem_emergency_pct=90.0,
            mem_scale_down_pct=80.0,
            resource_scale_emergency=0.1,
            resource_scale_down=0.6,
        )
        scale = scheduler._compute_scale(50.0, 95.0)
        assert scale == pytest.approx(0.1), f"Expected 0.1 (emergency), got {scale}"

    def test_from_yaml_parses_scale_coefficients(self):
        """from_yaml should parse scale coefficients from YAML."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("resource_scale_emergency: 0.25\n")
            f.write("resource_scale_down: 0.55\n")
            f.write("resource_scale_up: 1.45\n")
            f.write("resource_scale_normal: 0.95\n")
            yaml_path = f.name

        try:
            config = AppConfig.from_yaml(yaml_path)
            assert config.resource_scale_emergency == pytest.approx(0.25)
            assert config.resource_scale_down == pytest.approx(0.55)
            assert config.resource_scale_up == pytest.approx(1.45)
            assert config.resource_scale_normal == pytest.approx(0.95)
        finally:
            os.unlink(yaml_path)

    def test_scale_config_fields_in_dataclass(self):
        """All four scale config fields should be present on AppConfig."""
        config = AppConfig()
        assert hasattr(config, "resource_scale_emergency")
        assert hasattr(config, "resource_scale_down")
        assert hasattr(config, "resource_scale_up")
        assert hasattr(config, "resource_scale_normal")


# ======================================================================
# Fix 4 — Popup dismiss selector timeout
# ======================================================================


class TestPopupTimeoutConfig:
    """Verify popup_dismiss_selector_timeout is plumbed through consistently."""

    def test_popup_timeout_in_config(self):
        """AppConfig should have popup_dismiss_selector_timeout field."""
        config = AppConfig()
        assert hasattr(config, "popup_dismiss_selector_timeout")
        assert config.popup_dismiss_selector_timeout == 1000

    def test_popup_timeout_custom(self):
        """popup_dismiss_selector_timeout should accept custom values."""
        config = AppConfig(popup_dismiss_selector_timeout=500)
        assert config.popup_dismiss_selector_timeout == 500

    def test_popup_timeout_from_yaml(self):
        """from_yaml should parse popup_dismiss_selector_timeout."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("popup_dismiss_selector_timeout: 800\n")
            yaml_path = f.name

        try:
            config = AppConfig.from_yaml(yaml_path)
            assert config.popup_dismiss_selector_timeout == 800
        finally:
            os.unlink(yaml_path)

    def test_popup_timeout_on_executor(self):
        """BMCExecutor should receive popup_timeout from config."""
        config = AppConfig(popup_dismiss_selector_timeout=500)
        bm = BrowserManager(headless=True)
        exec_ = BMCExecutor(bm, popup_timeout=config.popup_dismiss_selector_timeout)
        assert exec_._popup_timeout == 500

    def test_popup_dismiss_selectors_not_empty(self):
        """POPUP_DISMISS_SELECTORS should have entries."""
        assert len(POPUP_DISMISS_SELECTORS) > 0

    def test_no_account_conflict_in_dismiss_selectors(self):
        """Dismiss selectors must NOT include account-conflict or session-expired patterns.

        Those patterns should cause FAIL, not be silently dismissed.
        """
        dismiss_texts = " ".join(POPUP_DISMISS_SELECTORS).lower()
        forbidden = ["已在其他地方登录", "会话冲突", "session conflict",
                     "已登录", "已经登录", "已在线", "session expired",
                     "会话已过期"]
        for kw in forbidden:
            assert kw.lower() not in dismiss_texts, (
                f"Account-conflict pattern '{kw}' found in POPUP_DISMISS_SELECTORS"
            )


# ======================================================================
# Fix 5 — Output directory atomic write
# ======================================================================


class TestOutputDirAtomicWrite:
    """Verify _ensure_writable_output_dir uses atomic file creation."""

    def test_output_dir_writable_success(self):
        """Nominal case: writable dir should succeed."""
        from src.app import App
        config = AppConfig(output_root=tempfile.mkdtemp())
        app = App(config)
        result = app._ensure_writable_output_dir()
        assert result is not None
        assert result.exists()
        # No test file left behind
        leftover = list(result.glob(".write_test_*"))
        assert len(leftover) == 0, f"Test files left behind: {leftover}"

    def test_output_dir_creates_missing_dirs(self):
        """Missing directory should be created."""
        from src.app import App
        base = Path(tempfile.mkdtemp()) / "nested" / "deep" / "output"
        config = AppConfig(output_root=str(base))
        app = App(config)
        result = app._ensure_writable_output_dir()
        assert result.exists() and result.is_dir()

    def test_concurrent_writes_no_conflict(self):
        """Multiple concurrent calls to _ensure_writable_output_dir must not conflict."""
        from src.app import App
        base = Path(tempfile.mkdtemp()) / "concurrent_test"
        config = AppConfig(output_root=str(base))

        n_threads = 8
        errors: list[Exception] = []
        results: list[Path] = []
        lock = threading.Lock()

        def _run():
            try:
                app = App(AppConfig(output_root=str(base)))
                p = app._ensure_writable_output_dir()
                with lock:
                    results.append(p)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=_run) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        assert len(results) == n_threads
        # All should resolve to the same directory
        assert len(set(results)) == 1

        # No leftover test files
        leftover = list(base.glob(".write_test_*"))
        assert len(leftover) == 0, f"Test files left behind: {leftover}"

    def test_fallback_on_permission_error(self):
        """If the primary dir is not writable, should fall back."""
        from src.app import App
        # Create a non-writable candidate
        base = Path(tempfile.mkdtemp())
        readonly_dir = base / "readonly"
        readonly_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(readonly_dir), 0o444)

        config = AppConfig(output_root=str(readonly_dir))
        app = App(config)
        result = app._ensure_writable_output_dir()
        assert result is not None
        # Should have fallen back to a writable location
        assert result != readonly_dir

    def test_no_test_file_left_on_success(self):
        """After _ensure_writable_output_dir succeeds, no .write_test_* files remain."""
        from src.app import App
        base = Path(tempfile.mkdtemp()) / "clean_test"
        config = AppConfig(output_root=str(base))
        app = App(config)
        app._ensure_writable_output_dir()
        leftover = list(base.glob(".write_test_*"))
        assert len(leftover) == 0


# ======================================================================
# Smoke: import verification
# ======================================================================

class TestImports:
    """Quick smoke test that all modified modules import cleanly."""

    def test_scheduler_imports(self):
        from src.scheduler import dynamic_scheduler
        assert hasattr(dynamic_scheduler, "DynamicScheduler")

    def test_config_imports(self):
        from src.models import app_config
        cfg = app_config.AppConfig()
        assert cfg.resource_scale_emergency == 0.3
        assert cfg.resource_scale_normal == 1.0
        assert cfg.popup_dismiss_selector_timeout == 1000

    def test_bmc_executor_imports(self):
        from src.executor import bmc_executor
        assert hasattr(bmc_executor, "BMCExecutor")
        assert hasattr(bmc_executor, "POPUP_DISMISS_SELECTORS")
