@echo off
chcp 65001 >nul 2>nul
setlocal EnableDelayedExpansion
title BMC Auto-Capture

:: ============================================================
::   BMC/SSH 自动化测试证据采集平台 — 统一启动入口
::   双击运行即可，也可在命令行传入参数
:: ============================================================

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:: --- 获取脚本所在目录 ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

:: ============================================================
::  引擎检测（编译版 bmc-engine.exe > 源码 Python run.py）
:: ============================================================
set "ENGINE="
set "USE_PYTHON=0"

if exist "%ROOT%\runtime\bmc-engine.exe" (
    set "ENGINE=%ROOT%\runtime\bmc-engine.exe"
) else if exist "%ROOT%\runtime\bmc-engine" (
    set "ENGINE=%ROOT%\runtime\bmc-engine"
) else if exist "%ROOT%\run.py" (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        for /f "delims=" %%p in ('where python') do set "ENGINE=%%p %ROOT%\run.py"
        set "USE_PYTHON=1"
    )
)

if "%ENGINE%"=="" (
    echo [错误] 找不到执行引擎。
    echo.
    echo 请确保以下之一存在：
    echo   1. runtime\bmc-engine.exe     （推荐，编译版）
    echo   2. run.py + 系统 Python 3.9+   （源码模式）
    echo.
    pause
    exit /b 1
)

:: ============================================================
::  参数解析
:: ============================================================
set "IS_SERVER=0"
set "HAS_CLI_ARGS=0"
set "HOST="
set "PORT="
set "LOG_LEVEL="
set "EXCEL=%ROOT%\app\examples\task_template.xlsx"
if not exist "%EXCEL%" set "EXCEL=%ROOT%\examples\task_template.xlsx"

:parse_args
if "%~1"=="" goto :check_mode

if /i "%~1"=="--server" (
    set "IS_SERVER=1"
    set "HAS_CLI_ARGS=1"
    shift
    goto :parse_args
)
if /i "%~1"=="--host"      set "HOST=%~2" & set "HAS_CLI_ARGS=1" & shift & shift & goto :parse_args
if /i "%~1"=="--port"      set "PORT=%~2" & set "HAS_CLI_ARGS=1" & shift & shift & goto :parse_args
if /i "%~1"=="--log-level" set "LOG_LEVEL=%~2" & set "HAS_CLI_ARGS=1" & shift & shift & goto :parse_args
if /i "%~1"=="--excel"     set "EXCEL=%~2" & set "HAS_CLI_ARGS=1" & shift & shift & goto :parse_args
if /i "%~1"=="-e"          set "EXCEL=%~2" & set "HAS_CLI_ARGS=1" & shift & shift & goto :parse_args

:: 透传给引擎
set "HAS_CLI_ARGS=1"
shift
goto :parse_args

:check_mode
if "%IS_SERVER%"=="1" goto :run_server
if "%HAS_CLI_ARGS%"=="1" goto :run_direct

:: 无参数 → 交互菜单
goto :menu

:: ============================================================
::  Server 模式 — 直接使用编译引擎 bmc-engine.exe
:: ============================================================
:run_server
if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=8080"
if "%LOG_LEVEL%"=="" set "LOG_LEVEL=info"

cls
echo ============================================================
echo   BMC Auto-Capture — API Server
echo ============================================================
echo.
echo   引擎 : %ENGINE%
echo   Host : %HOST%
echo   Port : %PORT%
echo.
echo   --- 健康检查: http://%HOST%:%PORT%/health ---
echo   --- 网络检测: http://%HOST%:%PORT%/network/ping ---
echo   --- 版本信息: http://%HOST%:%PORT%/version ---
echo.
echo   按 Ctrl+C 停止服务
echo ============================================================
echo.

"%ENGINE%" --app-dir "%ROOT%\app" --server --host %HOST% --port %PORT% --log-level %LOG_LEVEL%
if %ERRORLEVEL% neq 0 (
    echo [错误] 服务器启动失败，退出码: %ERRORLEVEL%
    pause
)
exit /b %ERRORLEVEL%

:: ============================================================
::  交互菜单
:: ============================================================
:menu
cls
echo ============================================================
echo    BMC / SSH 自动化测试证据采集平台
echo ============================================================
echo.
echo    Excel : %EXCEL%
echo    引擎  : %ENGINE%
echo.
echo    [1] 顺序执行（逐台设备，最稳定）
echo    [2] 并发执行（多设备同时，高效）
echo    [3] 网络连通性预检
echo    [4] Debug 模式顺序执行
echo    [5] 设定 Excel 配置文件路径
echo    [6] 调整 BMC/SSH 并发量
echo    [7] 退出
echo.
echo    Server 模式: 启动.bat --server [--host 0.0.0.0] [--port 8080]
echo.

set "CHOICE="
set /p CHOICE="   请选择 [1-7]: "

if "%CHOICE%"=="1" goto :run_seq
if "%CHOICE%"=="2" goto :run_full
if "%CHOICE%"=="3" goto :run_precheck
if "%CHOICE%"=="4" goto :run_debug
if "%CHOICE%"=="5" goto :set_excel
if "%CHOICE%"=="6" goto :set_workers
if "%CHOICE%"=="7" goto :end
goto :menu

:: ============================================================
::  执行模式
:: ============================================================

:run_seq
cls
echo ============================================================
echo    顺序执行模式
echo ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode sequential
set "SEQ_EXIT=%ERRORLEVEL%"
echo.
if %SEQ_EXIT% neq 0 (echo    [提示] 执行有错误，退出码: %SEQ_EXIT%)
echo    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_full
cls
echo ============================================================
echo    动态并发执行模式
echo ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode full
set "FULL_EXIT=%ERRORLEVEL%"
echo.
if %FULL_EXIT% neq 0 (echo    [提示] 执行有错误，退出码: %FULL_EXIT%)
echo    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_precheck
cls
echo ============================================================
echo    网络连通性预检
echo ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo    正在检测 TCP 443/22 端口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --preflight-only
echo.
echo    预检完成。按任意键返回菜单...
pause >nul
goto :menu

:run_debug
cls
echo ============================================================
echo    Debug 模式（顺序执行 + 详细日志）
echo ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode sequential --verbose
set "DBG_EXIT=%ERRORLEVEL%"
echo.
if %DBG_EXIT% neq 0 (echo    [DEBUG] 执行结束，退出码: %DBG_EXIT%) else (echo    [DEBUG] 执行成功完成)
echo    按任意键返回菜单...
pause >nul
goto :menu

:run_direct
if not exist "%EXCEL%" (
    set "EXCEL=%ROOT%\app\examples\task_template.xlsx"
    if not exist "!EXCEL!" set "EXCEL=%ROOT%\examples\task_template.xlsx"
)
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在 & pause & exit /b 1)
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%"
set "EXITCODE=%ERRORLEVEL%"
echo    执行完成，退出码: %EXITCODE%
if %EXITCODE% neq 0 (pause)
endlocal
exit /b %EXITCODE%

:set_excel
cls
echo ============================================================
echo    设定 Excel 配置文件路径
echo ============================================================
echo.
echo    当前: %EXCEL%
echo.
set /p NEW_EXCEL="   路径（支持拖拽）: "
if "!NEW_EXCEL!"=="" goto :menu
for /f "tokens=* delims= " %%a in ("!NEW_EXCEL!") do set "NEW_EXCEL=%%a"
if exist "!NEW_EXCEL!" (
    set "EXCEL=!NEW_EXCEL!"
    echo [成功] 已更新
) else (
    echo [错误] 文件不存在
)
timeout /t 2 >nul
goto :menu

:end
echo 再见。
endlocal
exit /b 0
