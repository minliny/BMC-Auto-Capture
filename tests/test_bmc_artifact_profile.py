from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.executor.bmc_executor import BMCExecutor
from src.models.execution_result import ExecutionResult


class _Task:
    task_name = "BMC fast evidence"
    task_type = "BMC"
    execution_mode = "BMC_URL"
    image_name_template = "{device_name}"
    _task_def = {}


class _FastTask(_Task):
    _task_def = {"artifact_profile": "fast"}


class _FakePage:
    url = "https://example.invalid/UI/Static/#/navigate/home"

    def is_closed(self):
        return False

    async def evaluate(self, *_args, **_kwargs):
        raise AssertionError("fast artifact profile must not capture state JSON")

    @property
    def context(self):
        raise AssertionError("fast artifact profile must not capture MHTML")


class _FakeCDPSession:
    async def send(self, *_args, **_kwargs):
        return {"data": "MHTML" + ("x" * 160)}


class _FakeContext:
    async def new_cdp_session(self, _page):
        return _FakeCDPSession()


class _FullFakePage:
    url = "https://example.invalid/UI/Static/#/navigate/home"
    context = _FakeContext()

    def is_closed(self):
        return False

    async def evaluate(self, script, *_args, **_kwargs):
        if "const result = {" not in str(script):
            return None
        return {
            "url": self.url,
            "title": "Home",
            "timestamp": "2026-06-17T00:00:00Z",
            "visible_text": "Home OK",
            "inputs": [],
            "textareas": [],
            "selects": [],
            "checked_like": [],
            "active_tab_like": [],
            "tables": [],
        }


def test_bmc_artifact_profile_resolves_task_override():
    executor = BMCExecutor(browser_manager=None, artifact_profile="full")
    assert executor._resolve_artifact_profile(_Task()) == "full"
    assert executor._resolve_artifact_profile(_FastTask()) == "fast"


def test_bmc_fast_artifact_profile_skips_heavy_evidence(monkeypatch, tmp_path: Path):
    executor = BMCExecutor(browser_manager=None, artifact_profile="fast")
    html_calls = {"count": 0}

    async def fake_screenshot(_page, ss_path, _task, _result):
        Path(ss_path).write_bytes(b"png")

    async def fake_compose(*_args, **_kwargs):
        return None

    async def fake_capture_html(_page):
        html_calls["count"] += 1
        return "<html><body>ok</body></html>"

    monkeypatch.setattr(executor, "_content_aware_screenshot", fake_screenshot)
    monkeypatch.setattr(executor, "_save_raw_and_compose", fake_compose)
    monkeypatch.setattr("src.executor.bmc_executor.capture_redacted_html", fake_capture_html)

    result = ExecutionResult(
        plan_id="p1",
        device_name="D1",
        task_name="BMC fast evidence",
        task_type="BMC",
        execution_mode="BMC_URL",
    )

    asyncio.run(
        executor._execute_final_capture(
            _FakePage(),
            _FastTask(),
            "192.0.2.10",
            "D1",
            str(tmp_path),
            result,
        )
    )

    assert result.artifact_status == "ARTIFACT_SAVED"
    assert html_calls["count"] == 1
    assert result.html_file.endswith(".html")
    assert any(s.step_name == "bmc_artifact_profile" for s in result.step_results)
    summary = next(s for s in result.step_results if s.step_name == "evidence_summary")
    assert "profile=fast" in summary.details
    assert "mhtml=skipped" in summary.details
    assert "manifest=" in summary.details

    runtime_context = json.loads(result.runtime_context)
    manifest_path = Path(runtime_context["artifact_manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["protocol"] == "BMC"
    assert payload["metadata"]["artifact_profile"] == "fast"
    assert payload["artifacts"]["screenshot"]["exists"] is True
    assert payload["artifacts"]["html"]["exists"] is True
    assert len(payload["artifacts"]["screenshot"]["sha256"]) == 64


def test_bmc_full_artifact_profile_writes_hash_manifest(monkeypatch, tmp_path: Path):
    executor = BMCExecutor(browser_manager=None, artifact_profile="full")

    async def fake_screenshot(_page, ss_path, _task, _result):
        Path(ss_path).write_bytes(b"png")

    async def fake_compose(*_args, **_kwargs):
        return None

    async def fake_capture_html(_page):
        return "<html><body>full evidence</body></html>"

    monkeypatch.setattr(executor, "_content_aware_screenshot", fake_screenshot)
    monkeypatch.setattr(executor, "_save_raw_and_compose", fake_compose)
    monkeypatch.setattr("src.executor.bmc_executor.capture_redacted_html", fake_capture_html)

    result = ExecutionResult(
        plan_id="p1",
        task_id="task.full",
        device_name="D1",
        task_name="BMC full evidence",
        task_type="BMC",
        execution_mode="BMC_URL",
    )

    asyncio.run(
        executor._execute_final_capture(
            _FullFakePage(),
            _Task(),
            "192.0.2.10",
            "D1",
            str(tmp_path),
            result,
        )
    )

    runtime_context = json.loads(result.runtime_context)
    manifest_path = Path(runtime_context["artifact_manifest_path"])
    state_json_path = tmp_path / "html" / "D1.state.json"
    state_payload = json.loads(state_json_path.read_text(encoding="utf-8"))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result.artifact_status == "ARTIFACT_SAVED"
    assert state_payload["metadata"]["artifact_manifest_path"] == "html/D1.metadata.json"
    assert manifest_payload["metadata"]["protocol"] == "BMC"
    assert manifest_payload["metadata"]["artifact_profile"] == "full"
    assert manifest_payload["artifacts"]["state_json"]["exists"] is True
    assert manifest_payload["artifacts"]["mhtml"]["exists"] is True
    assert len(manifest_payload["artifacts"]["state_json"]["sha256"]) == 64
