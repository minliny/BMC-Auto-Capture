@echo off
:: BMC Auto Capture - 统一启动入口
:: 启动.cmd 只负责编码适配和调用 exe --launcher
:: 不负责 Excel 解析、网络检测、任务执行

:: 设置 UTF-8 编码
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LANG=zh_CN.UTF-8
set LC_ALL=zh_CN.UTF-8

:: 获取脚本所在目录
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: 检测 exe 存在
if exist "bmc-auto-capture.exe" (
    set "EXE_PATH=bmc-auto-capture.exe"
    goto :run_exe
)

if exist "bmc-auto-capture" (
    set "EXE_PATH=bmc-auto-capture"
    goto :run_exe
)

:: 未找到 exe
echo [失败] 未找到 bmc-auto-capture.exe 或 bmc-auto-capture
echo [失败] 请确保此目录包含 release 包文件
pause
exit /b 1

:run_exe
:: 调用 exe 的 launcher 模式
echo [信息] BMC Auto Capture 启动中...
echo.

"%EXE_PATH%" --launcher %*

:: 如果执行失败，显示错误
if errorlevel 1 (
    echo.
    echo [失败] 执行失败，请查看上方错误信息
    pause
)

exit /b %errorlevel%
