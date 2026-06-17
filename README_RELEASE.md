# BMC Auto-Capture 使用说明

本说明面向 Windows 发布包用户。发布包已经包含执行引擎、Python 运行时、依赖库和 Chromium，客户机器不需要额外安装 Python。

## 目录结构

```text
bmc-auto-capture\
├── 启动.bat
├── runtime\
│   ├── bmc-engine.exe
│   ├── _internal\
│   ├── playwright_browsers\
│   └── build_info.json
├── app\
│   ├── src\
│   ├── config\
│   ├── examples\
│   │   └── task_template.xlsx
│   ├── api\
│   └── tasks.json
├── scripts\
├── run.py
└── output\
```

## 首次使用

1. 将 zip 完整解压到固定目录。
2. 如果 Windows 提示文件来自 Internet，在 PowerShell 中执行：

```powershell
Get-ChildItem "<解压目录>" -Recurse -File | Unblock-File
```

3. 复制 `app\examples\task_template.xlsx`，填写现场设备和任务。
4. 双击 `启动.bat`。
5. 选择 `[5]` 设置 Excel 文件路径。
6. 选择 `[3]` 做网络连通性预检。
7. 选择 `[6]` 检查 BMC/SSH 账号密码。
8. 选择 `[1]` 顺序执行，或选择 `[2]` 并发执行。
9. 到 `output\` 查看结果。

## 启动菜单

```text
[0] 启动 Executor API
[1] 顺序执行
[2] 并发执行
[3] 网络连通性预检
[R] 直接测试 IP:端口
[4] Debug 模式顺序执行
[5] 设定 Excel 配置文件路径
[6] 检查 BMC/SSH 账号密码
[7] 退出
```

## Excel 配置

Excel 至少需要「设备信息」和「任务列表」两个工作表。

「设备信息」常用列：

| 列 | 说明 |
|----|------|
| 设备分类 | 设备分组，例如 A3、L1、L2、RM211 |
| 设备名称 | 结果目录和报表中的设备名 |
| 是否启用 | 是/否 |
| 带外管理IP | BMC/iBMC 地址 |
| 带外管理用户名 | BMC/iBMC 用户名 |
| 带外管理密码 | BMC/iBMC 密码 |
| 带内管理IP | SSH/TELNET 地址 |
| 带内管理用户名 | SSH 用户名 |
| 带内管理密码 | SSH 密码 |

「任务列表」常用列：

| 列 | 说明 |
|----|------|
| 任务序号 | 执行顺序 |
| 任务名称 | 与 `tasks.json` 中的任务名称匹配 |
| 任务类型 | BMC / SSH / TELNET |
| 设备分组 | 限制只在指定分组设备上执行 |
| 是否启用 | 是/否 |

## 命令行执行

```cmd
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode sequential
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-only
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-auth all
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full --verbose
```

常用参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--app-dir` | app 目录路径 | 自动识别 |
| `--excel` | Excel 文件路径 | 自动查找模板 |
| `--mode` | sequential / full | sequential |
| `--concurrency` | 兼容旧脚本的并发参数 | 无 |
| `--max-bmc-workers` | BMC endpoint 最大并发数 | 配置文件 |
| `--max-ssh-workers` | SSH/TELNET endpoint 最大并发数 | 配置文件 |
| `--ssh-command-timeout` | SSH 单条命令最大等待秒数 | 配置文件 |
| `--ssh-idle-timeout` | SSH 输出空闲等待秒数 | 配置文件 |
| `--bmc-page-timeout` | BMC 页面等待秒数 | 配置文件 |
| `--bmc-artifact-profile` | full 保存完整证据，fast 只保存 PNG+HTML | full |
| `--preflight-only` | 只做 TCP 连通性预检 | 关闭 |
| `--preflight-target` | all / bmc / ssh | all |
| `--preflight-auth` | all / bmc / ssh | 关闭 |
| `--no-preflight` | 跳过执行前连通性预检 | 关闭 |
| `--verbose` | 输出 debug 日志 | 关闭 |

## Executor API

启动 API：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner fake
```

允许 API 请求执行真实任务：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner real --enable-real-runner
```

启用内置调试回调接收器：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner fake --enable-debug-callback-receiver
```

推荐调用流程：

```text
1. POST /executor/v1/config/excel          上传 Excel，得到 excelHash
2. POST /executor/v1/plans                 使用 excelHash 启动任务批次，得到 planId
3. GET  /executor/v1/plans/{planId}        查询批次汇总
4. GET  /executor/v1/plans/{planId}/items  查询任务明细
```

调试回调接口：

```text
POST   /debug/plan-item-statuses
GET    /debug/plan-item-statuses
DELETE /debug/plan-item-statuses
```

发布包自检：

```powershell
.\scripts\smoke_executor_runtime.ps1
```

## 输出文件

```text
output\<timestamp>\
├── result.csv
├── final_result.csv
├── summary_pivot.csv
├── failure_detail.csv
├── plan_timing.csv
├── device_timing.csv
├── endpoint_timing.csv
├── execution_summary.csv
├── execution_summary.json
├── evidence_audit.csv
├── auth_check_result.csv
└── <设备分组>\<设备名称>\<任务名称>\
    ├── *.png
    ├── html\*.html
    ├── html\*.mhtml
    ├── html\*.evidence.html
    ├── html\*.state.json
    ├── page_health_debug.json
    └── raw\
```

## 更新发布包

1. 备份现场 Excel、`tasks.json` 自定义改动和 `output\` 结果目录。
2. 下载最新 Windows x64 完整 zip 包。
3. 解压到新目录，确认 `runtime\`、`app\`、`启动.bat` 都存在。
4. 复制现场 Excel 或自定义任务配置到新目录。
5. 先运行网络预检和账号密码检测，再执行正式任务。
6. 不建议在旧目录混合覆盖不同发布包的 `runtime\` 和 `app\`。

## 常见问题

**双击 `启动.bat` 闪退**

在 CMD 或 PowerShell 中进入解压目录后手动执行 `启动.bat`，查看错误信息。

**提示找不到执行引擎**

确认 `runtime\bmc-engine.exe` 存在；源码模式才需要本机 Python。

**BMC 任务大量失败**

先做网络连通性预检，再做账号密码检测。若 BMC 页面加载慢，调大 `--bmc-page-timeout`；若出现账号冲突，确认没有其他浏览器或工具使用同一账号登录。

**SSH 任务输出不完整**

长输出命令调大 `--ssh-command-timeout`；输出间隔较长时调大 `--ssh-idle-timeout`。命令应能返回到下一条命令可输入的提示符。

**只想快速保存截图**

BMC 可使用 `--bmc-artifact-profile fast`，只保存 PNG 和 HTML。需要完整取证或问题复盘时使用默认 `full`。
