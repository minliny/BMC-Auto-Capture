# UTF-8 encoded — PowerShell handles this natively
$host.UI.RawUI.WindowTitle = "BMC Auto-Capture v0.2.1"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$RUNTIME = Join-Path $ROOT "..\runtime"
$APP = Join-Path $ROOT "..\app"
$ENGINE = Join-Path $RUNTIME "bmc-engine.exe"
$EXCEL = Join-Path $APP "examples\task_template.xlsx"

function Check-Files {
    if (-not (Test-Path $ENGINE)) {
        Write-Host "[错误] 找不到引擎: $ENGINE"
        Write-Host "请确保 runtime\bmc-engine.exe 存在，重新解压 runtime 包。"
        Pause
        exit 1
    }
    if (-not (Test-Path (Join-Path $APP "src"))) {
        Write-Host "[错误] app 目录不完整: $APP"
        Write-Host "请重新解压 bmc-app-*.zip 到当前目录。"
        Pause
        exit 1
    }
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[提示] 默认 Excel 不存在: $EXCEL"
        Write-Host "可通过菜单 [4] 指定文件路径。"
        Start-Sleep -Seconds 3
    }
}

function Show-Menu {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   BMC/SSH 自动化测试证据采集平台 v0.2.1"
    Write-Host "============================================================"
    Write-Host ""
    Write-Host "   配置文件: $EXCEL"
    Write-Host "   引擎位置: $ENGINE"
    Write-Host ""
    Write-Host "   [1] 开始执行（顺序模式，稳定）"
    Write-Host "   [2] 开始执行（并发模式，高效）"
    Write-Host "   [3] 仅网络预检（不执行任务）"
    Write-Host "   [4] 指定 Excel 文件"
    Write-Host "   [5] 查看最近结果"
    Write-Host "   [6] 退出"
    Write-Host ""
}

function Start-Sequential {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   顺序执行模式"
    Write-Host "============================================================"
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL"
        Pause
        return
    }
    Write-Host "   执行中，请勿关闭此窗口。"
    Write-Host "   结果将保存到 output\ 目录。"
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --mode sequential
    Write-Host ""
    Write-Host "   执行完成。按任意键返回菜单..."
    $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Start-Concurrent {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   动态并发执行模式"
    Write-Host "============================================================"
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL"
        Pause
        return
    }
    Write-Host "   执行中，请勿关闭此窗口。"
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --mode full
    Write-Host ""
    Write-Host "   执行完成。按任意键返回菜单..."
    $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Start-Preflight {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   网络连通性预检"
    Write-Host "============================================================"
    Write-Host ""
    if (-not (Test-Path $EXCEL)) {
        Write-Host "[错误] Excel 文件不存在: $EXCEL"
        Pause
        return
    }
    Write-Host "   正在检测设备网络连通性 (TCP 443/22)..."
    Write-Host ""
    & $ENGINE --app-dir $APP --excel $EXCEL --preflight-only
    Write-Host ""
    Write-Host "   预检完成。按任意键返回菜单..."
    $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Set-ExcelPath {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   指定 Excel 配置文件"
    Write-Host "============================================================"
    Write-Host ""
    Write-Host "   当前文件: $EXCEL"
    Write-Host ""
    Write-Host "   Excel 需包含两个 Sheet:"
    Write-Host "     「设备信息」— 设备 IP、用户名、密码"
    Write-Host "     「任务列表」— 任务名称、分组、启用状态"
    Write-Host "   模板位置: app\examples\task_template.xlsx"
    Write-Host ""
    $newPath = Read-Host "   请输入 Excel 文件路径（支持拖拽）"
    if ($newPath) {
        $script:EXCEL = $newPath
    }
}

function Show-Results {
    Clear-Host
    Write-Host "============================================================"
    Write-Host "   最近执行结果"
    Write-Host "============================================================"
    Write-Host ""
    $resultFile = Join-Path $ROOT "..\output\result.csv"
    if (Test-Path $resultFile) {
        Write-Host "    output\result.csv"
        Write-Host "    ----------------------------------------"
        Get-Content $resultFile | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "    未找到 result.csv，请先执行任务。"
    }
    Write-Host ""
    Write-Host "    按任意键返回菜单..."
    $null = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

# --- Main ---
Check-Files

while ($true) {
    Show-Menu
    $choice = Read-Host "   请选择 [1-6]"
    switch ($choice) {
        "1" { Start-Sequential }
        "2" { Start-Concurrent }
        "3" { Start-Preflight }
        "4" { Set-ExcelPath }
        "5" { Show-Results }
        "6" { exit 0 }
    }
}
