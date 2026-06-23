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
::  Windows 发布包完整性检查(runtime + app 分层)
:: ============================================================
set "PACKAGE_LAYOUT=0"
if exist "%ROOT%\app\src" set "PACKAGE_LAYOUT=1"
if exist "%ROOT%\runtime\bmc-engine.exe" set "PACKAGE_LAYOUT=1"

if "%PACKAGE_LAYOUT%"=="1" (
    if not exist "%ROOT%\runtime\bmc-engine.exe" (
        echo( [错误] 未找到 runtime\bmc-engine.exe。
        echo( 请先解压 Windows runtime 依赖包:
        echo(   bmc-runtime-^<版本^>-win-x64.7z
        echo( 并保持 runtime\ 与 app\ 位于同一根目录。
        echo.
        pause
        exit /b 1
    )
    if not exist "%ROOT%\runtime\playwright_browsers" (
        echo( [错误] 未找到 runtime\playwright_browsers。
        echo( runtime 依赖包不完整,请重新解压:
        echo(   bmc-runtime-^<版本^>-win-x64.7z
        echo.
        pause
        exit /b 1
    )
    if not exist "%ROOT%\app\src" (
        echo( [错误] 未找到 app\src。
        echo( app 脚本包不完整,请重新解压:
        echo(   bmc-app-^<版本^>.zip
        echo.
        pause
        exit /b 1
    )
    if not exist "%ROOT%\app\config" (
        echo( [错误] 未找到 app\config。
        echo( app 脚本包不完整,请重新解压 bmc-app-^<版本^>.zip。
        echo.
        pause
        exit /b 1
    )
    if not exist "%ROOT%\run.py" (
        echo( [错误] 未找到 run.py。
        echo( app 脚本包不完整,请重新解压 bmc-app-^<版本^>.zip。
        echo.
        pause
        exit /b 1
    )
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
::  Exe 来源验证 & 构建信息
:: ============================================================
if exist "%ROOT%\runtime\build_info.json" (
    echo( [构建信息]
    for /f "tokens=*" %%a in ('type "%ROOT%\runtime\build_info.json"') do set "JSON_LINE=%%a"
    :: 提取 version, git_commit, build_time
    for /f "tokens=2 delims=:" %%v in ('type "%ROOT%\runtime\build_info.json" ^| findstr "version"') do set "EXE_VER=%%v"
    for /f "tokens=2 delims=:" %%c in ('type "%ROOT%\runtime\build_info.json" ^| findstr "git_commit"') do set "EXE_COMMIT=%%c"
    for /f "tokens=2 delims=:" %%t in ('type "%ROOT%\runtime\build_info.json" ^| findstr "build_time"') do set "EXE_TIME=%%t"
    if defined EXE_VER (
        set "EXE_VER=%EXE_VER:"=%
        echo(   引擎版本: %EXE_VER%
    )
    if defined EXE_COMMIT (
        set "EXE_COMMIT=%EXE_COMMIT:"=%
        echo(   构建提交: %EXE_COMMIT%
    )
    if defined EXE_TIME (
        set "EXE_TIME=%EXE_TIME:"=%
        echo(   构建时间: %EXE_TIME%
    )
)

:: 验证 exe 是否包含 --preflight-auth (仅在编译 exe 模式)
if /i "%ENGINE_EXE:~-4%"==".exe" if "%USE_PYTHON%"=="0" (
    echo( [校验] 检测编译引擎参数 ...
    "%ENGINE_EXE%" --help 2>&1 | findstr "preflight-auth" >nul
    if !ERRORLEVEL! NEQ 0 (
        echo( ╔══════════════════════════════════════════════════════════╗
        echo( ║ [警告] 当前 bmc-engine.exe 缺少 --preflight-auth 参数。 ║
        echo( ║   请重新打包或替换 runtime\bmc-engine.exe。              ║
        echo( ║   如果使用旧版 exe，账户密码检测功能不可用。             ║
        echo( ╚══════════════════════════════════════════════════════════╝
        echo(
    ) else (
        echo( [OK] 引擎参数完整，包含 --preflight-auth
    )
    echo(
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
set "ACCEPTANCE_DOCX=0"
set "ACCEPTANCE_ARGS="

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
if "%PORT%"=="" set "PORT=18000"
if "%LOG_LEVEL%"=="" set "LOG_LEVEL=info"

cls
echo( ============================================================
echo(   BMC Auto-Capture Executor API
echo( ============================================================
echo(
echo(   程序 : %ENGINE_DISPLAY%
echo(   Host : %HOST%
echo(   Port : %PORT%
echo(
echo(   --- Executor API: http://%HOST%:%PORT%/executor/v1/status ---
echo(   --- 配置 Excel:  POST /executor/v1/config/excel:path ---
echo(   --- 执行计划:    POST /executor/v1/plans/{planId}:run ---
echo(   --- 查询进度:    GET  /executor/v1/plans/{planId} ---
echo(
echo(   调试回调接收器: 加 --enable-debug-callback-receiver 参数
echo(   --- 接收回调:    POST /debug/plan-item-statuses ---
echo(   --- 查询回调:    GET  /debug/plan-item-statuses ---
echo(   --- 清空回调:    DELETE /debug/plan-item-statuses ---
echo(
echo(   Legacy 兼容: /health /version /network/ping /routes
echo(   旧 Network Boot: 使用 --legacy-network-boot 参数
echo(
echo(   按 Ctrl+C 停止服务
echo( ============================================================
echo(

set "PYTHONPATH=%ROOT%\app;%ROOT%;%PYTHONPATH%"
if "%RAW_ARGS%"=="" (
    call :run_engine --app-dir "%APP_DIR%" --server --host %HOST% --port %PORT% --log-level %LOG_LEVEL%
) else (
    call :run_engine --app-dir "%APP_DIR%" %RAW_ARGS%
)
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
if "%BMC_WORKERS%"=="" (set "BMC_DISP=默认") else (set "BMC_DISP=%BMC_WORKERS%")
if "%SSH_WORKERS%"=="" (set "SSH_DISP=默认") else (set "SSH_DISP=%SSH_WORKERS%")
echo(    并发  : BMC=!BMC_DISP! SSH=!SSH_DISP!
echo.
echo(    [1] 执行任务 - 顺序模式(逐台设备,最稳定)
echo(    [2] 执行任务 - 并发模式(多设备同时,高效)
echo(    [3] 执行前检查 - 网络连通性/账号密码
echo(    [4] 直接测试单个 IP:端口(无需 Excel)
echo(    [5] 设定 Excel 配置文件路径
echo(    [6] 调整 BMC/SSH 并发量
echo(    [7] 启动 Executor API
echo(    [8] 使用已有截图手动生成测试用例报告
echo(    [9] 退出
echo.
echo(    命令行: 启动.bat --server [--host 0.0.0.0] [--port 18000] [--enable-debug-callback-receiver]
echo.

set "MENU_CHOICE="
set /p MENU_CHOICE="   请选择 [1-9]: "
set "MENU_CHOICE=!MENU_CHOICE:"=!"
for /f "tokens=* delims= " %%a in ("!MENU_CHOICE!") do set "MENU_CHOICE=%%a"

if "!MENU_CHOICE!"=="1" goto :run_seq
if "!MENU_CHOICE!"=="2" goto :run_full
if "!MENU_CHOICE!"=="3" goto :run_precheck
if "!MENU_CHOICE!"=="4" goto :test_ip
if "!MENU_CHOICE!"=="5" goto :set_excel
if "!MENU_CHOICE!"=="6" goto :set_workers
if "!MENU_CHOICE!"=="7" goto :run_server_menu
if "!MENU_CHOICE!"=="8" goto :manual_acceptance_docx
if "!MENU_CHOICE!"=="9" goto :end

REM Backward-compatible hidden shortcuts from older menus.
if "!MENU_CHOICE!"=="0" goto :run_server_menu
if /i "!MENU_CHOICE!"=="R" goto :test_ip
goto :menu

:run_server_menu
cls
echo( ============================================================
echo(    Executor API 配置
echo( ============================================================
echo.
echo(    默认值直接回车即可
echo.
set /p HOST="    监听地址 [0.0.0.0]: "
if "!HOST!"=="" set "HOST=0.0.0.0"
set /p PORT="    监听端口 [18000]: "
if "!PORT!"=="" set "PORT=18000"
set /p LOG_LEVEL="    日志级别 (debug/info/warning/error) [info]: "
if "!LOG_LEVEL!"=="" set "LOG_LEVEL=info"
echo.
echo(    启动中: %ENGINE_DISPLAY%
echo(    Host: !HOST!  Port: !PORT!  Log: !LOG_LEVEL!
echo.
goto :run_server

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
call :configure_acceptance_for_run
echo(    执行中,请勿关闭此窗口...
echo.
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode sequential %WORKER_ARGS% %ACCEPTANCE_ARGS%
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
call :configure_acceptance_for_run
echo(    执行中,请勿关闭此窗口...
echo.
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode full %WORKER_ARGS% %ACCEPTANCE_ARGS%
set "FULL_EXIT=%ERRORLEVEL%"
echo.
if %FULL_EXIT% neq 0 (echo    [提示] 执行有错误,退出码: %FULL_EXIT%)
echo(    执行完成。按任意键返回菜单...
pause >nul
goto :menu

:run_precheck
set "PF_MODE="
set "PF_TARGET="
set "PF_CHOICE="
cls
echo( ============================================================
echo(    执行前检查
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
set "PF_CHOICE=!PF_CHOICE:"=!"
for /f "tokens=* delims= " %%a in ("!PF_CHOICE!") do set "PF_CHOICE=%%a"

:: 网络连通性检测
if "!PF_CHOICE!"=="1" (set "PF_MODE=connect" & set "PF_TARGET=all")
if "!PF_CHOICE!"=="2" (set "PF_MODE=connect" & set "PF_TARGET=bmc")
if "!PF_CHOICE!"=="3" (set "PF_MODE=connect" & set "PF_TARGET=ssh")

:: 账户密码可用性检测
if "!PF_CHOICE!"=="4" (set "PF_MODE=auth" & set "PF_TARGET=all")
if "!PF_CHOICE!"=="5" (set "PF_MODE=auth" & set "PF_TARGET=bmc")
if "!PF_CHOICE!"=="6" (set "PF_MODE=auth" & set "PF_TARGET=ssh")

if "!PF_CHOICE!"=="7" goto :menu
if "!PF_MODE!"=="" goto :run_precheck

cls
echo( ============================================================
if "!PF_MODE!"=="connect" echo(    网络连通性检测 (!PF_TARGET!)
if "!PF_MODE!"=="auth" echo(    账户密码可用性检测 (!PF_TARGET!)
echo( ============================================================
echo.
if not exist "%EXCEL%" (echo [错误] Excel 文件不存在: %EXCEL% & pause & goto :menu)
if "!PF_MODE!"=="connect" echo(    正在检测网络连通性 target=!PF_TARGET! ...
if "!PF_MODE!"=="auth" echo(    正在检测账户密码 target=!PF_TARGET! ...
echo.
if "!PF_MODE!"=="connect" call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --preflight-only --preflight-target "!PF_TARGET!" %WORKER_ARGS%
if "!PF_MODE!"=="auth" call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --preflight-auth "!PF_TARGET!" %WORKER_ARGS%

set "PF_EXIT=%ERRORLEVEL%"
if %PF_EXIT% neq 0 (
    echo    [FAIL] Preflight/Auth failed, exit code: %PF_EXIT%
) else (
    echo    Preflight completed successfully.
)
echo    按任意键继续...
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
call :run_engine --app-dir "%APP_DIR%" --excel "%EXCEL%" --mode sequential %WORKER_ARGS% %ACCEPTANCE_ARGS% --verbose
set "DBG_EXIT=%ERRORLEVEL%"
echo.
if %DBG_EXIT% neq 0 (echo    [DEBUG] 执行结束,退出码: %DBG_EXIT%) else (echo    [DEBUG] 执行成功完成)
echo(    按任意键返回菜单...
pause >nul
goto :menu

:run_direct
set "DIRECT_ACCEPTANCE_ONLY=0"
echo(%RAW_ARGS% | findstr /i /c:"--acceptance-docx" >nul
if !ERRORLEVEL! EQU 0 (
    echo(%RAW_ARGS% | findstr /i /c:"--acceptance-run-output" /c:"--acceptance-evidence-dir" /c:"--acceptance-evidence-dirs" >nul
    if !ERRORLEVEL! EQU 0 set "DIRECT_ACCEPTANCE_ONLY=1"
)
if "!DIRECT_ACCEPTANCE_ONLY!"=="1" (
    call :run_engine --app-dir "%APP_DIR%" %RAW_ARGS%
    set "EXITCODE=!ERRORLEVEL!"
    echo(    执行完成,退出码: !EXITCODE!
    if !EXITCODE! neq 0 (pause)
    endlocal
    exit /b !EXITCODE!
)
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


:manual_acceptance_docx
cls
echo( ============================================================
echo(    使用已有截图手动生成测试用例报告
echo( ============================================================
echo.
echo(    请填写一个或多个已执行的截图目录路径,也可以把目录拖拽进来。
echo(    本功能只回填 DOCX 并打包证据,不会执行任务或网络连通性检查。
echo(    示例路径:
echo(      output\20260623_103000\4.2.4.计算节点部件信息查询测试-CPU\A3
echo(      output\20260623_103000\4.2.4.计算节点部件信息查询测试-CPU
echo(      output\20260623_103000
echo.
set /p ACCEPTANCE_DIRS="   目录路径(可拖拽目录进来): "
if "!ACCEPTANCE_DIRS!"=="" goto :menu
echo.
echo(    正在生成测试用例报告,不执行网络预检...
echo.
call :run_engine --app-dir "%APP_DIR%" --acceptance-docx --acceptance-evidence-dirs !ACCEPTANCE_DIRS!
set "DOCX_EXIT=!ERRORLEVEL!"
echo.
if %DOCX_EXIT% neq 0 (echo    [报告] 生成失败,退出码: %DOCX_EXIT%) else (echo    [报告] 生成完成。)
echo(    按任意键返回菜单...
pause >nul
goto :menu

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

:configure_acceptance_for_run
set "ACCEPTANCE_DOCX=0"
set "ACCEPTANCE_ARGS="
echo.
set /p DOCX_CHOICE="   本次执行完成后是否生成测试用例报告(DOCX/ZIP)? [y/N]: "
if /i "!DOCX_CHOICE!"=="Y" set "ACCEPTANCE_DOCX=1"
if /i "!DOCX_CHOICE!"=="YES" set "ACCEPTANCE_DOCX=1"
call :refresh_acceptance_args
if "%ACCEPTANCE_DOCX%"=="1" (
    echo(    已开启: 执行完成后生成测试用例报告。
) else (
    echo(    已关闭: 本次只执行任务,不生成测试用例报告。
)
echo.
exit /b 0

:refresh_acceptance_args
set "ACCEPTANCE_ARGS="
if "%ACCEPTANCE_DOCX%"=="1" set "ACCEPTANCE_ARGS=--acceptance-docx"
exit /b 0

:run_engine
if "%ENGINE_SCRIPT%"=="" (
    "%ENGINE_EXE%" %*
) else (
    "%ENGINE_EXE%" "%ENGINE_SCRIPT%" %*
)
exit /b %ERRORLEVEL%

:end
echo( 再见。
endlocal
exit /b 0
