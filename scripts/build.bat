@echo off
REM ============================================================
REM BMC Auto-Capture v0.2.5-RC5 - Build Script
REM Builds a self-contained one-directory distribution.
REM ============================================================
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   BMC Auto-Capture v0.2.5-RC5 - Build
echo ============================================================
echo.

set "ROOT=%~dp0.."
set "DIST=%ROOT%\dist\bmc-auto-capture"
set "PLAYWRIGHT_BROWSERS=%ROOT%\playwright_browsers"

REM Step 1: Install dependencies
echo [1/5] Installing Python dependencies...
pip install -r "%ROOT%\requirements.txt" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: pip install failed
    exit /b 1
)

REM Step 2: Install Playwright browsers
echo [2/5] Installing Playwright Chromium...
python -m playwright install chromium
if %ERRORLEVEL% neq 0 (
    echo ERROR: Playwright browser install failed
    exit /b 1
)

REM Step 3: Copy Playwright browsers to local dir
echo [3/5] Copying Playwright browsers...
set "MS_PLAYWRIGHT=%USERPROFILE%\AppData\Local\ms-playwright"
if exist "%MS_PLAYWRIGHT%" (
    if not exist "%PLAYWRIGHT_BROWSERS%" mkdir "%PLAYWRIGHT_BROWSERS%"
    xcopy /E /I /Y "%MS_PLAYWRIGHT%" "%PLAYWRIGHT_BROWSERS%" >nul
    echo   Copied from %MS_PLAYWRIGHT%
) else (
    echo   WARNING: ms-playwright not found at %MS_PLAYWRIGHT%
    echo   Browser will need to be installed manually on target machine
)

REM Step 4: Build with PyInstaller
echo [4/5] Running PyInstaller...
pyinstaller "%ROOT%\scripts\build.spec" --distpath "%ROOT%\dist" --workpath "%ROOT%\build" --clean --noconfirm
if %ERRORLEVEL% neq 0 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)

REM Step 5: Copy additional files
echo [5/5] Copying runtime files...

REM Config files
xcopy /E /I /Y "%ROOT%\config" "%DIST%\config" >nul

REM App layer files used by run.py --app-dir
if not exist "%DIST%\app" mkdir "%DIST%\app"
xcopy /E /I /Y "%ROOT%\src" "%DIST%\app\src" >nul
xcopy /E /I /Y "%ROOT%\config" "%DIST%\app\config" >nul
xcopy /E /I /Y "%ROOT%\examples" "%DIST%\app\examples" >nul
if exist "%ROOT%\templates" xcopy /E /I /Y "%ROOT%\templates" "%DIST%\app\templates" >nul
if exist "%ROOT%\api" xcopy /E /I /Y "%ROOT%\api" "%DIST%\app\api" >nul
if exist "%ROOT%\assets" xcopy /E /I /Y "%ROOT%\assets" "%DIST%\app\assets" >nul
copy "%ROOT%\tasks.json" "%DIST%\app\" >nul

REM Playwright browsers (if available)
if exist "%PLAYWRIGHT_BROWSERS%" (
    xcopy /E /I /Y "%PLAYWRIGHT_BROWSERS%" "%DIST%\playwright_browsers" >nul
)

REM Launcher batch file
(
echo @echo off
echo set PLAYWRIGHT_BROWSERS_PATH=%%~dp0playwright_browsers
echo set PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
echo.
echo BMC Auto-Capture v0.2.5-RC5
echo ========================
echo.
echo Usage: bmc-auto-capture.exe --excel ^<path_to_xlsx^> [--config ^<path_to_yaml^>]
echo.
echo Starting...
echo.
echo bmc-auto-capture.exe --app-dir "%%~dp0app" %%*
) > "%DIST%\run.bat"

REM README: keep README.md as the single operation guide source.
if exist "%ROOT%\README.md" (
    copy "%ROOT%\README.md" "%DIST%\README.txt" >nul
) else (
    (
    echo BMC Auto-Capture
    echo ================
    echo.
    echo Automated test evidence collection platform for BMC/SSH devices.
    echo.
    echo Quick start:
    echo   1. Double-click run.bat or run in terminal:
    echo      run.bat --excel path\to\task_template.xlsx
    echo.
    echo   2. Optional config:
    echo      run.bat --excel path\to\task_template.xlsx --config path\to\config.yaml
    echo.
    echo   3. For API server mode:
    echo      bmc-auto-capture.exe --app-dir app --server --port 8080
    echo.
    echo Requirements: Windows 10+, no Python installation needed.
    echo.
    ) > "%DIST%\README.txt"
)

echo.
echo ============================================================
echo   Build complete!
echo   Distribution: %DIST%
echo ============================================================
echo.
echo To package for distribution:
echo   7z a bmc-auto-capture-v0.2.5-RC5.7z "%DIST%"
echo.

endlocal
