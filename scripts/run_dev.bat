@echo off
REM Development quick-start script
setlocal

set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo Installing dev dependencies...
pip install -e ".[dev]"

echo.
echo Installing Playwright Chromium...
python -m playwright install chromium

echo.
echo Dev environment ready.
echo Run with: python -m src --excel path\to\任务模板.xlsx
