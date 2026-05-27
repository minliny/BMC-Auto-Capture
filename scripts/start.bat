@echo off
chcp 65001 >nul 2>nul
setlocal
title BMC Auto-Capture v0.2.1

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "RUNTIME=%ROOT%\runtime"
set "APP=%ROOT%\app"
set "ENGINE=%RUNTIME%\bmc-engine.exe"
set "EXCEL=%APP%\examples\task_template.xlsx"

:check_files
if not exist "%ENGINE%" (
    echo [ERROR] Engine not found: %ENGINE%
    echo Please extract runtime package first.
    pause
    exit /b 1
)

if not exist "%APP%\src" (
    echo [ERROR] App directory incomplete: %APP%
    echo Please extract bmc-app-*.zip first.
    pause
    exit /b 1
)

if not exist "%EXCEL%" (
    echo [INFO] Default Excel not found: %EXCEL%
    echo Use menu [4] to specify a custom path.
    timeout /t 3 >nul
)

:menu
cls
echo ============================================================
echo    BMC / SSH Auto-Capture v0.2.1
echo ============================================================
echo.
echo    Config: %EXCEL%
echo    Engine: %ENGINE%
echo.
echo    [1] Run (sequential, stable)
echo    [2] Run (concurrent, fast)
echo    [3] Preflight only (no execution)
echo    [4] Set Excel file path
echo    [5] View latest result
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
echo    Sequential Mode
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Running, do not close this window.
echo    Output will be saved to output\ folder.
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode sequential
echo.
echo    Done. Press any key to return to menu...
pause >nul
goto menu

:run_full
cls
echo ============================================================
echo    Concurrent Mode
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Running, do not close this window.
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode full
echo.
echo    Done. Press any key to return to menu...
pause >nul
goto menu

:preflight
cls
echo ============================================================
echo    Connectivity Preflight
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [ERROR] Excel not found: %EXCEL%
    pause
    goto menu
)
echo    Checking device connectivity (TCP 443/22)...
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --preflight-only
echo.
echo    Preflight done. Press any key to return to menu...
pause >nul
goto menu

:set_excel
cls
echo ============================================================
echo    Set Excel Config
echo ============================================================
echo.
echo    Current: %EXCEL%
echo.
echo    Excel must contain two sheets:
echo      Sheet 1 - Device info (IP, user, password)
echo      Sheet 2 - Task list (name, group, enabled)
echo    Template: app\examples\task_template.xlsx
echo.
set /p NEW_EXCEL="    Enter Excel path (drag-n-drop supported): "
if not "%NEW_EXCEL%"=="" set "EXCEL=%NEW_EXCEL%"
goto menu

:view_result
cls
echo ============================================================
echo    Latest Result
echo ============================================================
echo.
if exist "%ROOT%\output\result.csv" (
    echo    output\result.csv
    echo    ----------------------------------------
    type "%ROOT%\output\result.csv"
) else (
    echo    No result.csv found. Run a task first.
)
echo.
echo    Press any key to return to menu...
pause >nul
goto menu

:end
endlocal
exit /b 0
