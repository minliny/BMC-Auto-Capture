from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bat_text() -> str:
    return (PROJECT_ROOT / "启动.bat").read_bytes().decode("gbk")


def _section(text: str, start_label: str, end_label: str) -> str:
    start_match = re.search(rf"(?m)^{re.escape(start_label)}\r?$", text)
    assert start_match is not None, f"{start_label} not found"
    end_match = re.search(rf"(?m)^{re.escape(end_label)}\r?$", text[start_match.end():])
    assert end_match is not None, f"{end_label} not found after {start_label}"
    start = start_match.start()
    end = start_match.end() + end_match.start()
    return text[start:end]


def test_launcher_menu_is_contiguous_and_option_8_is_manual_report():
    text = _bat_text()
    menu = _section(text, ":menu", ":run_server_menu")

    expected_lines = [
        "[1] 执行任务 - 顺序模式",
        "[2] 执行任务 - 并发模式",
        "[3] 执行前检查 - 网络连通性/账号密码",
        "[4] 直接测试单个 IP:端口",
        "[5] 设定 Excel 配置文件路径",
        "[6] 调整 BMC/SSH 并发量",
        "[7] 启动 Executor API",
        "[8] 使用已有截图手动生成测试用例报告",
        "[9] 退出",
    ]
    positions = [menu.index(line) for line in expected_lines]
    assert positions == sorted(positions)
    assert 'set /p MENU_CHOICE="   请选择 [1-9]: "' in menu
    assert 'if "!MENU_CHOICE!"=="8" goto :manual_acceptance_docx' in menu


def test_launcher_manual_report_does_not_run_preflight_or_excel_execution():
    text = _bat_text()
    manual = _section(text, ":manual_acceptance_docx", ":set_excel")

    assert "--acceptance-docx" in manual
    assert "--acceptance-evidence-dirs" in manual
    assert "--preflight-only" not in manual
    assert "--preflight-auth" not in manual
    assert '--excel "%EXCEL%"' not in manual
    assert "不会执行任务或网络连通性检查" in manual


def test_launcher_precheck_clears_previous_mode_before_prompting():
    text = _bat_text()
    precheck = _section(text, ":run_precheck", ":run_debug")

    assert 'set "PF_MODE="' in precheck
    assert 'set "PF_TARGET="' in precheck
    assert 'set "PF_CHOICE="' in precheck
    assert 'if "!PF_MODE!"=="" goto :run_precheck' in precheck


def test_windows_release_packages_acceptance_template_and_docx_runtime():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8")

    assert "--hidden-import docx" in workflow
    assert "--hidden-import lxml" in workflow
    assert "--collect-submodules docx" in workflow
    assert "--collect-submodules lxml" in workflow
    assert "templates app-layer\\app\\templates" in workflow
    assert "templates bmc-auto-capture\\app\\templates" in workflow
    assert "Smoke test — acceptance DOCX export from bundled template" in workflow
