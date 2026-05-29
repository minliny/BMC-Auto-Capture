# UTF-8 with BOM
# BMC/SSH 自动化测试证据采集平台 v0.2.1
# 直接右键 "使用 PowerShell 运行" 或终端执行: .\start.ps1

$host.UI.RawUI.WindowTitle = "BMC Auto-Capture v0.2.1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RUNTIME = Join-Path $ScriptDir "..\runtime"
$APP = Join-Path $ScriptDir "..\app"
$ENGINE = Join-Path $RUNTIME "bmc-engine.exe"
$EXCEL = Join-Path $APP "examples\task_template.xlsx"

# ====== 自动解除网络下载文件的安全锁定 ======
Write-Host "正在解除文件安全锁定..." -ForegroundColor Gray

# Unblock scripts dir (shallow — just .ps1/.bat)
Get-ChildItem -Path $ScriptDir -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# Unblock runtime dir (shallow — bmc-engine.exe only, skip _internal/ to avoid hang)
if (Test-Path $RUNTIME) {
    Get-ChildItem -Path $RUNTIME -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
}

# Unblock app dir (recurse — only .py/.json/.yaml source files, small tree)
if (Test-Path $APP) {
    Get-ChildItem -Path $APP -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
}

Write-Host "完成。" -ForegroundColor Gray

# ====== 环境检查 ======
function 检查环境 {
    if (-not (Test-Path $ENGINE)) {
        Write-Host ""
        Write-Host "[错误] 找不到引擎文件: $ENGINE" -ForegroundColor Red
        Write-Host "请确保 runtime 目录已正确解压，包含 bmc-engine.exe。" -ForegroundColor Yellow
        Write-Host "下载地址: https://github.com/minliny/BMC-Auto-Capture/releases" -ForegroundColor Yellow
        Read-Host "按 Enter 键退出"
        exit 1
    }

    if (-not (Test-Path (Join-Path $APP "src"))) {
        Write-Host ""
        Write-Host "[错误] app 目录不完整: $APP" -ForegroundColor Red
        Write-Host "请确保 app 目录已正确解压，包含 src/config/examples 子目录。" -ForegroundColor Yellow
        Read-Host "按 Enter 键退出"
        exit 1
    }

    if (-not (Test-Path $EXCEL)) {
        Write-Host ""
        Write-Host "[提示] 默认配置文件不存在: $EXCEL" -ForegroundColor Yellow
        Write-Host "可通过菜单 [4] 指定 Excel 文件路径。" -ForegroundColor Yellow
        Write-Host "模板位置: app\examples\task_template.xlsx" -ForegroundColor Gray
        Start-Sleep -Seconds 2
    }
}

# ====== 菜单 ======
function 显示菜单 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   BMC/SSH 自动化测试证据采集平台 v0.2.1" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   配置文件: $EXCEL" -ForegroundColor Gray
    Write-Host "   引擎位置: $ENGINE" -ForegroundColor Gray
    Write-Host ""
    Write-Host "   [1] 顺序执行（逐台设备，稳定）" -ForegroundColor White
    Write-Host "   [2] 并发执行（多设备同时，高效）" -ForegroundColor White
    Write-Host "   [3] 网络连通性预检（仅测试端口可达性）" -ForegroundColor White
    Write-Host "   [4] 指定 Excel 配置文件路径" -ForegroundColor White
    Write-Host "   [5] 查看最近执行结果" -ForegroundColor White
    Write-Host "   [6] 退出" -ForegroundColor White
    Write-Host ""
}

# ====== 功能函数 ======
function 执行顺序模式 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   顺序执行模式" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL" -ForegroundColor Red
        Read-Host "按 Enter 键返回菜单"
        return
    }
    Write-Host "任务执行中，请勿关闭此窗口。" -ForegroundColor Yellow
    Write-Host "结果将保存到 output\ 目录。" -ForegroundColor Gray
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --mode sequential
    Write-Host ""
    Write-Host "执行完成。按 Enter 键返回菜单..." -ForegroundColor Green
    Read-Host
}

function 执行并发模式 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   动态并发执行模式" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL" -ForegroundColor Red
        Read-Host "按 Enter 键返回菜单"
        return
    }
    Write-Host "任务执行中，请勿关闭此窗口。" -ForegroundColor Yellow
    Write-Host "系统会根据 CPU/内存自动调整并发数。" -ForegroundColor Gray
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --mode full
    Write-Host ""
    Write-Host "执行完成。按 Enter 键返回菜单..." -ForegroundColor Green
    Read-Host
}

function 执行网络预检 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   网络连通性预检" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL" -ForegroundColor Red
        Read-Host "按 Enter 键返回菜单"
        return
    }
    Write-Host "正在检测设备网络连通性（TCP 443/22 端口）..." -ForegroundColor Yellow
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --preflight-only
    Write-Host ""
    Write-Host "预检完成。按 Enter 键返回菜单..." -ForegroundColor Green
    Read-Host
}

function 设置Excel路径 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   指定 Excel 配置文件" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   当前文件: $EXCEL" -ForegroundColor Gray
    Write-Host ""
    Write-Host "   Excel 需包含两个工作表:" -ForegroundColor Yellow
    Write-Host "     「设备信息」— 设备名称、IP、用户名、密码、是否启用" -ForegroundColor White
    Write-Host "     「任务列表」— 任务名称、类型、分组、标签、是否启用" -ForegroundColor White
    Write-Host "   模板位置: app\examples\task_template.xlsx" -ForegroundColor Gray
    Write-Host ""
    $新路径 = Read-Host "请输入 Excel 文件完整路径（支持拖拽文件到此处）"
    if ($新路径) {
        $新路径 = $新路径.Trim('"').Trim("'")
        if (Test-Path $新路径) {
            $script:EXCEL = $新路径
            Write-Host "已更新配置文件路径。" -ForegroundColor Green
        } else {
            Write-Host "[错误] 文件不存在: $新路径" -ForegroundColor Red
        }
        Start-Sleep -Seconds 1
    }
}

function 查看最近结果 {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "   最近执行结果" -ForegroundColor White
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    $结果目录 = Join-Path $ScriptDir "..\output"
    $结果文件 = Get-ChildItem -Path $结果目录 -Recurse -Filter "result.csv" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 1

    if ($结果文件) {
        Write-Host "   文件: $($结果文件.FullName)" -ForegroundColor Gray
        Write-Host "   更新时间: $($结果文件.LastWriteTime)" -ForegroundColor Gray
        Write-Host "   ----------------------------------------" -ForegroundColor Gray
        Get-Content $结果文件.FullName -Encoding UTF8 | Select-Object -First 50 | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "   未找到 result.csv，请先执行任务。" -ForegroundColor Yellow
    }
    Write-Host ""
    Read-Host "按 Enter 键返回菜单"
}

# ====== 主程序 ======
检查环境

while ($true) {
    显示菜单
    $选择 = Read-Host "请输入选项 [1-6]"
    switch ($选择) {
        "1" { 执行顺序模式 }
        "2" { 执行并发模式 }
        "3" { 执行网络预检 }
        "4" { 设置Excel路径 }
        "5" { 查看最近结果 }
        "6" {
            Write-Host "再见。" -ForegroundColor Green
            exit 0
        }
        default {
            Write-Host "无效选项，请重新输入。" -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
}
