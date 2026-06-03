@echo off
chcp 65001 >nul 2>nul
setlocal EnableDelayedExpansion
title BMC Auto-Capture

:: ============================================================
::   BMC/SSH 自动化测试证据采集平台 — 统一启动入口
::   双击运行即可，也可在命令行传入参数
::   用法: 启动.bat [--excel <路径>] [--mode sequential|full] ...
:: ============================================================

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:: --- 获取脚本所在目录 ---
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

:: --- 引擎检测 ---
set "ENGINE="
if exist "%ROOT%\runtime\bmc-engine.exe" (
    set "ENGINE=%ROOT%\runtime\bmc-engine.exe"
) else if exist "%ROOT%\runtime\bmc-engine" (
    set "ENGINE=%ROOT%\runtime\bmc-engine"
) else if exist "%ROOT%\bmc-auto-capture.exe" (
    set "ENGINE=%ROOT%\bmc-auto-capture.exe"
) else if exist "%ROOT%\bmc-auto-capture" (
    set "ENGINE=%ROOT%\bmc-auto-capture"
)

:: 源码 fallback：用 run.py
if "%ENGINE%"=="" (
    if exist "%ROOT%\run.py" (
        where python >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            for /f "delims=" %%p in ('where python') do set "ENGINE=%%p %ROOT%\run.py"
        )
    )
)

if "%ENGINE%"=="" (
    echo [错误] 找不到执行引擎。
    echo 请确保 runtime\bmc-engine.exe 存在，或已安装 Python 并能运行 run.py。
    pause
    exit /b 1
)

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

:: --- 解析命令行参数（直接传参跳过菜单） ---
set "HAS_CLI_ARGS=0"
:parse_args
if "%~1"=="" goto :check_cli
set "HAS_CLI_ARGS=1"
if /i "%~1"=="--excel"       set "EXCEL=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-e"            set "EXCEL=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--mode"        set "MODE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-m"            set "MODE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--preflight-only" set "PRECHECK=--preflight-only" & shift & goto :parse_args
if /i "%~1"=="--debug"       set "DEBUG=--verbose" & shift & goto :parse_args
if /i "%~1"=="-d"            set "DEBUG=--verbose" & shift & goto :parse_args
if /i "%~1"=="--output"      set "OUTPUT=--output %~2" & shift & shift & goto :parse_args
if /i "%~1"=="-o"            set "OUTPUT=--output %~2" & shift & shift & goto :parse_args
if /i "%~1"=="--max-bmc-workers"     set "MAX_BMC=--max-bmc-workers %~2" & shift & shift & goto :parse_args
if /i "%~1"=="--max-ssh-workers"     set "MAX_SSH=--max-ssh-workers %~2" & shift & shift & goto :parse_args
if /i "%~1"=="--ssh-command-timeout" set "SSH_CMD_TIMEOUT=--ssh-command-timeout %~2" & shift & shift & goto :parse_args
if /i "%~1"=="--ssh-idle-timeout"    set "SSH_IDLE_TIMEOUT=--ssh-idle-timeout %~2" & shift & shift & goto :parse_args
if /i "%~1"=="--bmc-page-timeout"    set "BMC_PAGE_TIMEOUT=--bmc-page-timeout %~2" & shift & shift & goto :parse_args
shift & goto :parse_args

:check_cli
if "%HAS_CLI_ARGS%"=="1" goto :run_direct

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
echo    引擎   : %ENGINE%
echo.
echo    [1] 顺序执行（逐台设备，最稳定）
echo    [2] 并发执行（多设备同时，高效）
echo    [3] 网络连通性预检（仅测试端口可达性）
echo    [4] Debug 模式顺序执行（显示所有细节，故障不闪退）
echo    [5] 设定 Excel 配置文件路径
echo    [6] 调整 BMC/SSH 并发量
echo    [7] 退出
echo.
echo    提示：也可以直接拖拽 Excel 文件到此窗口，或：
echo          启动.bat --excel "C:\path\to\file.xlsx" --mode full
echo          启动.bat --debug --excel "C:\path\to\file.xlsx"
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

:: ============================================================
:run_debug
cls
echo ============================================================
echo    Debug 模式（顺序执行 + 详细日志）
echo ============================================================
echo.
echo    - 显示所有调试信息
echo    - 故障发生时不会闪退
echo    - 输出目录: output\
echo.
call :check_excel || goto :menu
echo    执行中，请勿关闭此窗口...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode sequential --verbose %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "DBG_EXIT=%ERRORLEVEL%"
echo.
echo ============================================================
if %DBG_EXIT% neq 0 (
    echo    [DEBUG] 执行结束，退出码: %DBG_EXIT%
    echo    [DEBUG] 上方如有错误信息，可截图排查。
) else (
    echo    [DEBUG] 执行成功完成。
)
echo ============================================================
echo.
echo    按任意键返回菜单...
pause >nul
goto :menu

:: ============================================================
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

:: ============================================================
:run_precheck
cls
echo ============================================================
echo    网络连通性预检
echo ============================================================
echo.
call :check_excel || goto :menu
echo    正在检测 TCP 443/22 端口可达性...
echo.
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --preflight-only %OUTPUT%
echo.
echo    预检完成。按任意键返回菜单...
pause >nul
goto :menu

:: ============================================================
:run_direct
echo [信息] 命令行模式，直接执行...
call :check_excel
if %ERRORLEVEL% neq 0 exit /b 1
"%ENGINE%" --app-dir "%ROOT%\app" --excel "%EXCEL%" --mode %MODE% %DEBUG% %PRECHECK% %OUTPUT% %MAX_BMC% %MAX_SSH% %SSH_CMD_TIMEOUT% %SSH_IDLE_TIMEOUT% %BMC_PAGE_TIMEOUT%
set "EXITCODE=%ERRORLEVEL%"
echo.
echo    执行完成，退出码: %EXITCODE%
if %EXITCODE% neq 0 (
    echo    按任意键查看错误信息...
    pause >nul
)
endlocal
exit /b %EXITCODE%

:: ============================================================
:set_excel
cls
echo ============================================================
echo    设定 Excel 配置文件路径
echo ============================================================
echo.
echo    当前: %EXCEL%
echo.
echo    输入文件路径（可直接拖拽文件到此窗口，然后按 Enter）
echo    提示: 在资源管理器 Shift+右键复制文件路径，
echo          粘贴的路径带引号也可以直接使用
echo.
set "NEW_EXCEL="
set /p NEW_EXCEL="   路径: "

if "!NEW_EXCEL!"=="" goto :menu

:: 去掉中英文引号 — 支持  "file"  "file"  'file'  'file'  「file」
call :strip_quotes NEW_EXCEL

if exist "!NEW_EXCEL!" (
    set "EXCEL=!NEW_EXCEL!"
    echo [成功] 已更新: !EXCEL!
) else (
    echo [错误] 文件不存在: !NEW_EXCEL!
)
timeout /t 2 >nul
goto :menu

:: ============================================================
:set_workers
cls
echo ============================================================
echo    调整 BMC/SSH 并发量
echo ============================================================
echo.
echo    当前设置:
if defined MAX_BMC (echo    BMC 并发上限 : %MAX_BMC:~20%) else (echo    BMC 并发上限 : (使用配置文件默认值))
if defined MAX_SSH (echo    SSH 并发上限 : %MAX_SSH:~20%) else (echo    SSH 并发上限 : (使用配置文件默认值))
echo.
echo    直接按 Enter 保留当前值，输入 0 恢复使用配置文件默认值。
echo.

set /p B="   BMC 并发上限（建议 1-16）: "
if defined B (
    if "!B!"=="0" (set "MAX_BMC=") else (set "MAX_BMC=--max-bmc-workers !B!")
)

set /p S="   SSH 并发上限（建议 1-64）: "
if defined S (
    if "!S!"=="0" (set "MAX_SSH=") else (set "MAX_SSH=--max-ssh-workers !S!")
)

echo [成功] 并发量已更新。
timeout /t 1 >nul
goto :menu

:: ============================================================
:check_excel
if not exist "%EXCEL%" (
    echo [错误] Excel 文件不存在: %EXCEL%
    echo.
    echo 请选择菜单 [4] 指定正确的文件路径。
    echo 可以拖拽 .xlsx 文件到此窗口，或在资源管理器中
    echo Shift+右键 → 复制文件路径，粘贴到 [4] 中。
    pause
    exit /b 1
)
exit /b 0

:: ============================================================
:strip_quotes
:: 去除变量中的中英文引号和空格
:: 用法: call :strip_quotes VARNAME
:: 支持: "..."  "..."  '...'  '...'  「...」  "..."
setlocal EnableDelayedExpansion
set "_VAL=!%~1!"
:: 去掉首尾空格
for /f "tokens=* delims= " %%a in ("!_VAL!") do set "_VAL=%%a"
:: 去掉中文引号
if "!_VAL:~0,1!"==""" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"==""" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"=="'" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"=="'" set "_VAL=!_VAL:~1!"
if "!_VAL:~0,1!"=="「" set "_VAL=!_VAL:~1!"
:: 去掉尾部引号
if "!_VAL:~-1!"=="""" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="""" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="'" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="'" set "_VAL=!_VAL:~0,-1!"
if "!_VAL:~-1!"=="」" set "_VAL=!_VAL:~0,-1!"
:: 再去一次空格
for /f "tokens=* delims= " %%a in ("!_VAL!") do set "_VAL=%%a"
endlocal & set "%~1=%_VAL%"
exit /b 0

:: ============================================================
:end
echo 再见。
endlocal
exit /b 0
