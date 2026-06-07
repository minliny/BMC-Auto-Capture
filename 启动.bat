@echo off
setlocal EnableDelayedExpansion
title BMC Auto-Capture

:: ============================================================
::   BMC/SSH 自动化测试证据采集平台 — 统一启动入口
::   双击运行即可,也可在命令行传入参数
:: ============================================================

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "RAW_ARGS=%*"

:: --- 获取脚本所在目录 ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "APP_DIR=%ROOT%\app"
if not exist "%APP_DIR%\src" (
    if exist "%ROOT%\src" set "APP_DIR=%ROOT%"
)

:: ============================================================
::  引擎检测(编译版 bmc-engine.exe > 离线 Python)
:: ============================================================
set "ENGINE_EXE="
set "ENGINE_SCRIPT="
set "ENGINE_DISPLAY="
set "USE_PYTHON=0"

:: 1. 编译引擎(开箱即用,最优先)
if exist "%ROOT%\runtime\bmc-engine.exe" (
    set "ENGINE_EXE=%ROOT%\runtime\bmc-engine.exe"
    set "ENGINE_DISPLAY=%ROOT%\runtime\bmc-engine.exe"
) else if exist "%ROOT%\runtime\bmc-engine" (
    set "ENGINE_EXE=%ROOT%\runtime\bmc-engine"
    set "ENGINE_DISPLAY=%ROOT%\runtime\bmc-engine"
)

:: 2. 离线 Python(基于 ROOT 定位)
if "%ENGINE_EXE%"=="" (
    if exist "%ROOT%\.venv\Scripts\python.exe" (
        set "ENGINE_EXE=%ROOT%\.venv\Scripts\python.exe"
        set "ENGINE_SCRIPT=%ROOT%\run.py"
        set "ENGINE_DISPLAY=%ROOT%\.venv\Scripts\python.exe %ROOT%\run.py"
        set "USE_PYTHON=1"
    ) else if exist "%ROOT%\offline_bmc_deps\python311\python.exe" (
        set "ENGINE_EXE=%ROOT%\offline_bmc_deps\python311\python.exe"
        set "ENGINE_SCRIPT=%ROOT%\run.py"
        set "ENGINE_DISPLAY=%ROOT%\offline_bmc_deps\python311\python.exe %ROOT%\run.py"
        set "USE_PYTHON=1"
    ) else if exist "%ROOT%\runtime\python311\python.exe" (
        set "ENGINE_EXE=%ROOT%\runtime\python311\python.exe"
        set "ENGINE_SCRIPT=%ROOT%\run.py"
        set "ENGINE_DISPLAY=%ROOT%\runtime\python311\python.exe %ROOT%\run.py"
        set "USE_PYTHON=1"
    ) else if exist "%USERPROFILE%\Documents\BMC离线部署包v0.2\offline_bmc_deps\python311\python.exe" (
        echo( [fallback] %%USERPROFILE%%\Documents\BMC离线部署包v0.2\offline_bmc_deps\python311\python.exe
        set "ENGINE_EXE=%USERPROFILE%\Documents\BMC离线部署包v0.2\offline_bmc_deps\python311\python.exe"
        set "ENGINE_SCRIPT=%ROOT%\run.py"
        set "ENGINE_DISPLAY=%USERPROFILE%\Documents\BMC离线部署包v0.2\offline_bmc_deps\python311\python.exe %ROOT%\run.py"
        set "USE_PYTHON=1"
    ) else if exist "%USERPROFILE%\Documents\BMC离线部署包 - 多并发版本\offline_bmc_deps\python311\python.exe" (
        echo( [fallback] %%USERPROFILE%%\Documents\BMC离线部署包 - 多并发版本\offline_bmc_deps\python311\python.exe
        set "ENGINE_EXE=%USERPROFILE%\Documents\BMC离线部署包 - 多并发版本\offline_bmc_deps\python311\python.exe"
        set "ENGINE_SCRIPT=%ROOT%\run.py"
        set "ENGINE_DISPLAY=%USERPROFILE%\Documents\BMC离线部署包 - 多并发版本\offline_bmc_deps\python311\python.exe %ROOT%\run.py"
        set "USE_PYTHON=1"
    ) else if exist "%ROOT%\run.py" (
        where python >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            for /f "delims=" %%p in ('where python') do (
                if "!ENGINE_EXE!"=="" set "ENGINE_EXE=%%p"
            )
            set "ENGINE_SCRIPT=%ROOT%\run.py"
            set "ENGINE_DISPLAY=!ENGINE_EXE! %ROOT%\run.py"
            set "USE_PYTHON=1"
        )
    )
)

:: 3. 源码模式自动设置 PYTHONPATH
if "%USE_PYTHON%"=="1" (
    set "PYTHONPATH=%ROOT%\app;%ROOT%;%PYTHONPATH%"
)

if "%ENGINE_EXE%"=="" (
    echo( [错误] 未找到可用执行环境。
    echo.
    echo( 检查路径:
    echo(   1. runtime\bmc-engine.exe
    echo(   2. .venv\Scripts\python.exe + run.py
    echo(   3. offline_bmc_deps\python311\python.exe + run.py
    echo(   4. runtime\python311\python.exe + run.py
    echo.
    echo( 如果文件存在但仍无法启动:
    echo(   可能是下载安全标记导致 exe 被 Windows 阻止。
    echo(   请在 PowerShell 中运行:
    echo(     Get-ChildItem . -Recurse -File ^| Unblock-File
    echo(   或重新解压安装包。
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
set "EXCEL=%APP_DIR%\examples\task_template.xlsx"
if not exist "%EXCEL%" set "EXCEL=%ROOT%\examples\task_template.xlsx"
set "BMC_WORKERS="
set "SSH_WORKERS="
set "WORKER_ARGS="

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

:: 未识别参数在 direct 模式中通过 RAW_ARGS 透传给引擎
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
echo( ============================================================
echo(   BMC Auto-Capture — API Server
echo( ============================================================
echo.
echo(   引擎 : %ENGINE_DISPLAY%
echo(   Host : %HOST%
echo(   Port : %PORT%
echo.
echo(   --- 健康检查: http://%HOST%:%PORT%/health ---
echo(   --- 网络检测: http://%HOST%:%PORT%/network/ping ---
echo(   --- 版本信息: http://%HOST%:%PORT%/version ---
echo.
echo(   按 Ctrl+C 停止服务
echo( ============================================================
echo.

set "PYTHONPATH=%ROOT%\app;%ROOT%;%PYTHONPATH%"
call :run_engine --app-dir "%APP_DIR%" --server --host %HOST% --port %PORT% --log-level %LOG_LEVEL%
set "SERVER_EXIT=%ERRORLEVEL%"
if %SERVER_EXIT% neq 0 (
    echo( [错误] 服务器启动失败,退出码: %SERVER_EXIT%
    pause
)
exit /b %SERVER_EXIT%

:: ============================================================
::  交互菜单
:: ============================================================
:menu
cls
echo( ============================================================
echo(    BMC / SSH 自动化测试证据采集平台
echo( ============================================================
echo.
echo(    Excel : %EXCEL%
echo(    引擎  : %ENGINE_DISPLAY%
echo(    并发  : BMC=%BMC_WORKERS% SSH=%SSH_WORKERS%
echo.
echo(    [1] 顺序执行(逐台设备,最稳定)
echo(    [2] 并发执行(多设备同时,高效)
echo(    [3] 网络连通性预检
echo(    [R] 直接测试 IP:端口(无需 Excel)
echo(    [4] Debug 模式顺序执行
echo(    [5] 设定 Excel 配置文件路径
echo(    [6] 调整 BMC/SSH 并发量
echo(    [7] 退出
echo.
echo(    Server 模式: 启动.bat --server [--host 0.0.0.0] [--port 8080]
echo.

set "CHOICE="
set /p CHOICE="   请选择 [1-7]: "

if "%CHOICE%"=="1" goto :run_seq
if "%CHOICE%"=="2" goto :run_full
if "%CHOICE%"=="3" goto :run_precheck
if /i "%CHOICE%"=="R" goto :test_ip
if /i "%CHOICE%"=="r" goto :test_ip
if "%CHOICE%"=="4" goto :run_debug
if "%CHOICE%"=="5" goto :set_excel
if "%CHOICE%"=="6" goto :set_workers
if "%CHOICE%"=="7" goto :end
goto :menu

:test_ip
cls
echo( ============================================================
echo(    直接测试 IP:端口
echo( ============================================================
echo(
echo(    输入目标 IP 或主机名:
set /p TEST_IP="   IP: "
if "!TEST_IP!"=="" goto :menu
echo(
echo(    输入端口号:
set /p TEST_PORT="   Port: "
if "!TEST_PORT!"=="" set "TEST_PORT=443"
echo.
echo(    正在测试 !TEST_IP!:!TEST_PORT! ...
echo.
powershell -Command "&\$d=@{}; \$s=New-Object Net.Sockets.TcpClient; \$c=\$s.BeginConnect('!TEST_IP!',!TEST_PORT!,\$null,\$null); \$r=\$c.AsyncWaitHandle.WaitOne(5000,\$false); if(\$r -and \$s.Connected){\$d['status']='OK';\$s.EndConnect(\$c)}else{\$d['status']='TIMEOUT'}; \$s.Close(); Write-Host ('结果: '+ \$d['status'])"
echo.
if %ERRORLEVEL% equ 0 (echo(   连接成功!) else (echo(   连接失败或超时)
echo(
pause >nul
goto :menu

:set_workers
cls
echo( ============================================================
echo(   调整 BMC/SSH 并发量
echo( ============================================================
echo(
echo(   当前 BMC max: %BMC_WORKERS%
echo(   当前 SSH max: %SSH_WORKERS%
echo(
set /p BMC_INPUT="   BMC 最大并发数(留空使用配置文件): "
set /p SSH_INPUT="   SSH 最大并发数(留空使用配置文件): "
set "BMC_WORKERS=!BMC_INPUT!"
set "SSH_WORKERS=!SSH_INPUT!"
call :refresh_worker_args
echo(
echo(   已更新: !WORKER_ARGS!
timeout /t 2 >nul
goto :menu

:: ============================================================
::  执行模式
:: ============================================================

:run_seq
cls
echo( ============================================================
echo(    顺序执行模式
echo( ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo(    执行中,请勿关闭此窗口...
echo.
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode sequential %WORKER_ARGS%
set "SEQ_EXIT=%ERRORLEVEL%"
echo.
if %SEQ_EXIT% neq 0 (echo    [提示] 执行有错误,退出码: %SEQ_EXIT%)
echo(    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_full
cls
echo( ============================================================
echo(    动态并发执行模式
echo( ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo(    执行中,请勿关闭此窗口...
echo.
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode full %WORKER_ARGS%
set "FULL_EXIT=%ERRORLEVEL%"
echo.
if %FULL_EXIT% neq 0 (echo    [提示] 执行有错误,退出码: %FULL_EXIT%)
echo(    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_precheck
cls
echo( ============================================================
echo(    预检选项
echo( ============================================================
echo(
echo(  --- 网络连通性检测(TCP端口) ---
echo(    [1] 全量检测(BMC+SSH)
echo(    [2] 仅检测 BMC(443)
echo(    [3] 仅检测 SSH(22)
echo(
echo(  --- 账户密码可用性检测(需登录) ---
echo(    [4] 全量检测(BMC+SSH)
echo(    [5] 仅检测 BMC
echo(    [6] 仅检测 SSH
echo(
echo(    [7] 返回菜单
echo.
set /p PF_CHOICE="   请选择 [1-7]: "

:: 网络连通性检测
if "!PF_CHOICE!"=="1" (set "PF_MODE=connect" & set "PF_TARGET=all")
if "!PF_CHOICE!"=="2" (set "PF_MODE=connect" & set "PF_TARGET=bmc")
if "!PF_CHOICE!"=="3" (set "PF_MODE=connect" & set "PF_TARGET=ssh")

:: 账户密码可用性检测
if "!PF_CHOICE!"=="4" (set "PF_MODE=auth" & set "PF_TARGET=all")
if "!PF_CHOICE!"=="5" (set "PF_MODE=auth" & set "PF_TARGET=bmc")
if "!PF_CHOICE!"=="6" (set "PF_MODE=auth" & set "PF_TARGET=ssh")

if "!PF_CHOICE!"=="7" goto :menu
if "%PF_MODE%"=="" goto :run_precheck

cls
echo( ============================================================
if "%PF_MODE%"=="connect" echo(    网络连通性检测 (%PF_TARGET%)
if "%PF_MODE%"=="auth" echo(    账户密码可用性检测 (%PF_TARGET%)
echo( ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
if "%PF_MODE%"=="connect" echo(    正在检测网络连通性 target=%PF_TARGET% ...
if "%PF_MODE%"=="auth" echo(    正在检测账户密码 target=%PF_TARGET% ...
echo.
if "%PF_MODE%"=="connect" call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --preflight-only --preflight-target "%PF_TARGET%" %WORKER_ARGS%
if "%PF_MODE%"=="auth" call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --preflight-only --preflight-auth "%PF_TARGET%" %WORKER_ARGS%
echo.
echo(    预检完成。按任意键返回菜单...
pause >nul
goto :menu

:run_debug
cls
echo( ============================================================
echo(    Debug 模式(顺序执行 + 详细日志)
echo( ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
echo(    执行中,请勿关闭此窗口...
echo.
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode sequential %WORKER_ARGS% --verbose
set "DBG_EXIT=%ERRORLEVEL%"
echo.
if %DBG_EXIT% neq 0 (echo    [DEBUG] 执行结束,退出码: %DBG_EXIT%) else (echo    [DEBUG] 执行成功完成)
echo(    按任意键返回菜单...
pause >nul
goto :menu

:run_direct
if not exist "%EXCEL%" (
    set "EXCEL=%APP_DIR%\examples\task_template.xlsx"
    if not exist "!EXCEL!" set "EXCEL=%ROOT%\examples\task_template.xlsx"
)
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在 & pause & exit /b 1)
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" %RAW_ARGS%
set "EXITCODE=%ERRORLEVEL%"
echo(    执行完成,退出码: %EXITCODE%
if %EXITCODE% neq 0 (pause)
endlocal
exit /b %EXITCODE%

:set_excel
cls
echo( ============================================================
echo(    设定 Excel 配置文件路径
echo( ============================================================
echo.
echo(    当前: %EXCEL%
echo.
set /p NEW_EXCEL="   路径(支持拖拽): "
if "!NEW_EXCEL!"=="" goto :menu
for /f "tokens=* delims= " %%a in ("!NEW_EXCEL!") do set "NEW_EXCEL=%%a"
if exist "!NEW_EXCEL!" (
    set "EXCEL=!NEW_EXCEL!"
    echo( [成功] 已更新
) else (
    echo( [错误] 文件不存在
)
timeout /t 2 >nul
goto :menu

:refresh_worker_args
set "WORKER_ARGS="
if not "%BMC_WORKERS%"=="" set "WORKER_ARGS=%WORKER_ARGS% --max-bmc-workers %BMC_WORKERS%"
if not "%SSH_WORKERS%"=="" set "WORKER_ARGS=%WORKER_ARGS% --max-ssh-workers %SSH_WORKERS%"
exit /b 0

:run_engine
if "%ENGINE_SCRIPT%"=="" (
    "%ENGINE_EXE%" %1 %2 %3 %4 %5 %6 %7 %8 %9
) else (
    "%ENGINE_EXE%" "%ENGINE_SCRIPT%" %1 %2 %3 %4 %5 %6 %7 %8 %9
)
exit /b %ERRORLEVEL%

:end
echo( 再见。
endlocal
exit /b 0
