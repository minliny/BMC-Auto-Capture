@echo off
chcp 65001 >nul
title BMC Auto-Capture v2.0
setlocal

set "EXCEL=examples\任务模板.xlsx"
set "ENGINE=bmc-auto-capture.exe"

:check_files
if not exist "%ENGINE%" (
    echo [错误] 找不到引擎文件: %ENGINE%
    echo 请确保 bmc-auto-capture.exe 在当前目录。
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo    BMC/SSH 自动化测试证据采集平台 v2.0
echo ============================================================
echo.
echo    当前配置文件: %EXCEL%
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
echo    执行中... 请勿关闭此窗口。
echo    结果将保存到 output\ 目录。
echo.
"%ENGINE%" --excel "%EXCEL%" --mode sequential
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
echo    执行中... 请勿关闭此窗口。
echo.
"%ENGINE%" --excel "%EXCEL%" --mode full
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
"%ENGINE%" --excel "%EXCEL%" --preflight-only
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
echo    当前: %EXCEL%
echo.
set /p NEW_EXCEL="    请输入 Excel 文件路径: "
if not "%NEW_EXCEL%"=="" set "EXCEL=%NEW_EXCEL%"
goto menu

:view_result
cls
echo ============================================================
echo    最近执行结果
echo ============================================================
echo.
if exist "output\result.csv" (
    echo    output\result.csv  (%date% %time%)
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
