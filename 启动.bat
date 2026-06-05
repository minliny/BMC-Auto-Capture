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
::  Python 检测（优先离线部署包，再 fallback 到系统 Python）
:: ============================================================
set "PYTHON="

:: 离线 Python（与 bmc-auto-capture/ 平级）
set "OFFLINE_PYTHON=%ROOT%\..\offline_bmc_deps\python311\python.exe"
if exist "%OFFLINE_PYTHON%" (
    set "PYTHON=%OFFLINE_PYTHON%"
    goto :found_python
)

:: 系统 Python
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    for /f "delims=" %%p in ('where python') do (
        set "PYTHON=%%p"
        goto :found_python
    )
)

:: 引擎模式（bmc-engine.exe）
if exist "%ROOT%\runtime\bmc-engine.exe" (
    set "ENGINE=%ROOT%\runtime\bmc-engine.exe"
    goto :check_args
)

echo [错误] 找不到 Python 或执行引擎。
echo.
echo 请确保以下之一存在：
echo   1. offline_bmc_deps\python311\python.exe     （离线部署包）
echo   2. python（系统 PATH 中的 Python 3.9+）
echo   3. runtime\bmc-engine.exe                     （编译引擎）
echo.
pause
exit /b 1

:found_python
:: --- 设置 PYTHONPATH 使 api/ 模块可导入 ---
set "PYTHONPATH=%ROOT%\app;%PYTHONPATH%"
echo [信息] 使用 Python: %PYTHON%
echo [信息] PYTHONPATH: %ROOT%\app
echo.

:check_args
:: ============================================================
::  参数解析
:: ============================================================
if "%ENGINE%"=="" set "ENGINE=%PYTHON% %ROOT%\run.py"

:: --- 检测是否为 Server 模式 ---
set "IS_SERVER=0"
set "HAS_CLI_ARGS=0"
set "ARGS="

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

:: 非 server 参数：透传给 run.py
set "ARGS=%ARGS% %~1"
set "HAS_CLI_ARGS=1"
shift
goto :parse_args

:check_mode
if "%IS_SERVER%"=="1" goto :run_server
if "%HAS_CLI_ARGS%"=="1" goto :run_direct

:: 无参数 → 进入交互菜单
goto :menu

:: ============================================================
::  Server 模式
:: ============================================================
:run_server
:: 确保 PYTHON 已设置（引擎模式时没有 PYTHON）
if "%PYTHON%"=="" (
    if exist "%OFFLINE_PYTHON%" (
        set "PYTHON=%OFFLINE_PYTHON%"
    ) else (
        echo [错误] Server 模式需要 Python，但未找到。
        echo 请确保以下路径存在：
        echo   ..\offline_bmc_deps\python311\python.exe
        pause
        exit /b 1
    )
    set "PYTHONPATH=%ROOT%\app;%PYTHONPATH%"
)

if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=8080"
if "%LOG_LEVEL%"=="" set "LOG_LEVEL=info"

cls
echo ============================================================
echo   BMC Auto-Capture — API Server
echo ============================================================
echo.
echo   Python    : %PYTHON%
echo   PYTHONPATH: %ROOT%\app
echo   Host      : %HOST%
echo   Port      : %PORT%
echo   Log Level : %LOG_LEVEL%
echo.
echo   --- 健康检查: http://%HOST%:%PORT%/health ---
echo   --- 网络检测: http://%HOST%:%PORT%/network/ping ---
echo   --- 版本信息: http://%HOST%:%PORT%/version ---
echo.
echo   按 Ctrl+C 停止服务
echo ============================================================
echo.

%PYTHON% %ROOT%\run.py --server --host %HOST% --port %PORT% --log-level %LOG_LEVEL%
if %ERRORLEVEL% neq 0 (
    echo [错误] 服务器启动失败，退出码: %ERRORLEVEL%
    pause
)
exit /b %ERRORLEVEL%

:: ============================================================
::  交互菜单（旧版 Excel 任务模式）
:: ============================================================
:menu
cls
echo ============================================================
echo    BMC / SSH 自动化测试证据采集平台
echo ============================================================
echo.
echo    Excel   : %EXCEL%
echo    引擎    : %ENGINE%
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
::  下方保留原 Excel 任务模式的全部功能（与旧版一致）
:: ============================================================

:: --- 默认值 ---
set "EXCEL=%ROOT%\app\examples\task_template.xlsx"
if not exist "%EXCEL%" set "EXCEL=%ROOT%\examples\task_template.xlsx"
set "MODE=sequential"
set "PRECHECK="
set "OUTPUT="
set "MAX_BMC="
set "MAX_SSH="
set "SSH_CMD_TIMEOUT="
set "SSH_IDLE_TIMEOUT="
set "BMC_PAGE_TIMEOUT="
set "DEBUG="

:run_seq
cls
echo ============================================================
echo    顺序执行模式
echo ============================================================
echo.
call :check_excel || goto :menu
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode sequential %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "SEQ_EXIT=%ERRORLEVEL%"
echo.
if %SEQ_EXIT% neq 0 (
    echo    [提示] 执行有错误，退出码: %SEQ_EXIT%。上方可查看详情。
)
echo    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_debug
cls
echo ============================================================
echo    Debug 模式（顺序执行 + 详细日志）
echo ============================================================
echo.
call :check_excel || goto :menu
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode sequential --verbose %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "DBG_EXIT=%ERRORLEVEL%"
echo.
if %DBG_EXIT% neq 0 (
    echo    [DEBUG] 执行结束，退出码: %DBG_EXIT%
) else (
    echo    [DEBUG] 执行成功完成。
)
echo    按任意键返回菜单...
pause >nul
goto :menu

:run_full
cls
echo ============================================================
echo    动态并发执行模式
echo ============================================================
echo.
call :check_excel || goto :menu
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode full %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "FULL_EXIT=%ERRORLEVEL%"
echo.
if %FULL_EXIT% neq 0 (
    echo    [提示] 执行有错误，退出码: %FULL_EXIT%。上方可查看详情。
)
echo    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_precheck
cls
echo ============================================================
echo    网络连通性预检
echo ============================================================
echo.
call :check_excel || goto :menu
echo    正在检测 TCP 443/22 端口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --preflight-only %OUTPUT%
echo.
echo    预检完成。按任意键返回菜单...
pause >nul
goto :menu

:run_direct
if "%EXCEL%"=="" (
    set "EXCEL=%ROOT%\app\examples\task_template.xlsx"
    if not exist "!EXCEL!" set "EXCEL=%ROOT%\examples\task_template.xlsx"
)
call :check_excel
if %ERRORLEVEL% neq 0 exit /b 1
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode %MODE% %DEBUG% %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "EXITCODE=%ERRORLEVEL%"
echo.
echo    执行完成，退出码: %EXITCODE%
if %EXITCODE% neq 0 (
    pause
)
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
set "NEW_EXCEL="
set /p NEW_EXCEL="   路径（支持拖拽）: "
if "!NEW_EXCEL!"=="" goto :menu
call :strip_quotes NEW_EXCEL
if exist "!NEW_EXCEL!" (
    set "EXCEL=!NEW_EXCEL!"
    echo [成功] 已更新
) else (
    echo [错误] 文件不存在
)
timeout /t 2 >nul
goto :menu

:check_excel
if not exist "%EXCEL%" (
    echo [错误] Excel 文件不存在: %EXCEL%
    pause
    exit /b 1
)
exit /b 0

:strip_quotes
setlocal EnableDelayedExpansion
set "_VAL=!%~1!"
for /f "tokens=* delims= " %%a in ("!_VAL!") do set "_VAL=%%a"
if "!_VAL:~0,1!"=="^"" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"=="'" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"=="「" set "_VAL=!_VAL:~1!"
if "!_VAL:~-1!"=="^""" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="'" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="」" set "_VAL=!_VAL:~0,-1!"
for /f "tokens=* delims= " %%a in ("!_VAL!") do set "_VAL=%%a"
endlocal & set "%~1=%_VAL%"
exit /b 0

:end
echo 再见。
endlocal
exit /b 0
