@echo off
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM Launch PowerShell launcher (UTF-8 safe path)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\start.ps1"

endlocal
