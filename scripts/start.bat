@echo off
REM Force UTF-8 codepage (must be before any non-ASCII output)
chcp 65001 >nul 2>nul
if errorlevel 1 (
    echo [WARNING] UTF-8 codepage not available, Chinese may display incorrectly
    echo [WARNING] Recommend using Windows Terminal
)

setlocal
title BMC Auto-Capture v2.0

set "ROOT=%~dp0"
REM Remove trailing backslash for clean path joining
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "RUNTIME=%ROOT%\runtime"
set "APP=%ROOT%\app"
set "ENGINE=%RUNTIME%\bmc-engine.exe"
set "EXCEL=%APP%\examples\task_template.xlsx"

:check_files
if not exist "%ENGINE%" (
    echo [ERROR] Engine not found: %ENGINE%
    echo Please ensure runtime\bmc-engine.exe exists and re-extract the runtime package.
    pause
    exit /b 1
)

if not exist "%APP%\src" (
    echo [ERROR] App directory incomplete: %APP%
    echo Please re-extract bmc-app-*.zip to this folder.
    pause
    exit /b 1
)

if not exist "%EXCEL%" (
    echo [WARNING] Default Excel not found: %EXCEL%
    echo You can specify one via menu option [4].
    timeout /t 3 >nul
)

:menu
cls
echo ============================================================
echo    BMC/SSH Automated Test Evidence Capture v2.0
echo ============================================================
echo.
echo    Config: %EXCEL%
echo    Engine: %ENGINE%
echo.
echo    [1] Run (sequential mode)
echo    [2] Run (concurrent mode)
echo    [3] Preflight only (no execution)
echo    [4] Specify Excel file
echo    [5] View latest results
echo    [6] Exit
echo.

set /p CHOICE=   Select [1-6]:

if "%CHOICE%"=="1" goto run_seq
if "%CHOICE%"=="2" goto run_full
if "%CHOICE%"=="3" goto preflight
if "%CHOICE%"=="4" goto set_excel
if "%CHOICE%"=="5" goto view_result
if "%CHOICE%"=="6" goto end
goto menu

:run_seq
cls
echo ============================================================
echo    Sequential Execution Mode
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Running... DO NOT close this window.
echo    Results will be saved to output\ folder.
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode sequential
echo.
echo    Done. Press any key to return to menu...
pause >nul
goto menu

:run_full
cls
echo ============================================================
echo    Concurrent Execution Mode
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Running... DO NOT close this window.
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode full
echo.
echo    Done. Press any key to return to menu...
pause >nul
goto menu

:preflight
cls
echo ============================================================
echo    Network Connectivity Preflight
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Testing device connectivity (TCP 443/22)...
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --preflight-only
echo.
echo    Preflight done. Press any key to return to menu...
pause >nul
goto menu

:set_excel
cls
echo ============================================================
echo    Specify Excel Configuration
echo ============================================================
echo.
echo    Current: %EXCEL%
echo.
set /p NEW_EXCEL="    Enter Excel path: "
if not "%NEW_EXCEL%"=="" set "EXCEL=%NEW_EXCEL%"
goto menu

:view_result
cls
echo ============================================================
echo    Latest Results
echo ============================================================
echo.
if exist "output\result.csv" (
    echo    output\result.csv
    echo    ----------------------------------------
    type "output\result.csv"
) else (
    echo    result.csv not found. Please run a task first.
)
echo.
echo    Press any key to return to menu...
pause >nul
goto menu

:end
endlocal
exit /b 0
