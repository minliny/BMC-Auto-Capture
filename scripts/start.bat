@echo off
REM UTF-8 with BOM — DO NOT save as ANSI
REM Works in CMD and PowerShell 5+

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
    echo [错误] 找不到引擎: %ENGINE%
    echo 请确保 runtime\bmc-engine.exe 存在，重新解压 runtime 包。
    pause
    exit /b 1
)

if not exist "%APP%\src" (
    echo [错误] app 目录不完整: %APP%
    echo 请重新解压 bmc-app-*.zip 到当前目录。
    pause
    exit /b 1
)

if not exist "%EXCEL%" (
    echo [提示] 默认 Excel 不存在: %EXCEL%
    echo 可通过菜单 [4] 指定文件路径。
    timeout /t 3 >nul
)

:menu
cls
echo ============================================================
echo    BMC/SSH 自动化测试证据采集平台 v0.2.1
echo ============================================================
echo.
echo    配置文件: %EXCEL%
echo    引擎位置: %ENGINE%
echo.
echo    [1] 开始执行（顺序模式，稳定）
echo    [2] 开始执行（并发模式，高效）
echo    [3] 仅网络预检（不执行任务）
echo    [4] 指定 Excel 文件
echo    [5] 查看最近结果
echo    [6] 退出
echo.

set /p CHOICE=   请选择 [1-6]:

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
echo    顺序执行模式
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [错误] Excel 文件不存在: %EXCEL%
    pause
    goto menu
)
echo    执行中，请勿关闭此窗口。
echo    结果将保存到 output\ 目录。
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode sequential
echo.
echo    执行完成。按任意键返回菜单...
pause >nul
goto menu

:run_full
cls
echo ============================================================
echo    动态并发执行模式
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [错误] Excel 文件不存在: %EXCEL%
    pause
    goto menu
)
echo    执行中，请勿关闭此窗口。
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --mode full
echo.
echo    执行完成。按任意键返回菜单...
pause >nul
goto menu

:preflight
cls
echo ============================================================
echo    网络连通性预检
echo ============================================================
echo.
if not exist "%EXCEL%" (
    echo [错误] Excel 文件不存在: %EXCEL%
    pause
    goto menu
)
echo    正在检测设备网络连通性 (TCP 443/22)...
echo.
"%ENGINE%" --app-dir "%APP%" --excel "%EXCEL%" --preflight-only
echo.
echo    预检完成。按任意键返回菜单...
pause >nul
goto menu

:set_excel
cls
echo ============================================================
echo    指定 Excel 配置文件
echo ============================================================
echo.
echo    当前文件: %EXCEL%
echo.
echo    Excel 需包含两个 Sheet:
echo      「设备信息」— 设备 IP、用户名、密码
echo      「任务列表」— 任务名称、分组、启用状态
echo    模板位置: app\examples\task_template.xlsx
echo.
set /p NEW_EXCEL="    请输入 Excel 文件路径（支持拖拽）: "
if not "%NEW_EXCEL%"=="" set "EXCEL=%NEW_EXCEL%"
goto menu

:view_result
cls
echo ============================================================
echo    最近执行结果
echo ============================================================
echo.
if exist "output\result.csv" (
    echo    output\result.csv
    echo    ----------------------------------------
    type "output\result.csv"
) else (
    echo    未找到 result.csv，请先执行任务。
)
echo.
echo    按任意键返回菜单...
pause >nul
goto menu

:end
endlocal
exit /b 0
