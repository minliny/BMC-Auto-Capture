# BMC Auto-Capture

面向服务器 BMC/iBMC、交换设备、SSH/TELNET 命令采集场景的自动化测试证据采集平台。工具会根据 Excel 配置批量登录设备，执行 BMC 页面截图或命令行采集，并输出截图、HTML、命令日志、结果表和证据质量审计文件。

## 核心能力

- BMC 浏览器自动化：Playwright 驱动，支持登录、页面跳转、交互动作、全页截图、HTML/MHTML/State JSON 证据保存。
- SSH/TELNET 命令采集：使用 Python 网络栈执行命令，保存 TXT 输出和终端截图。
- Endpoint-aware 调度：同一 BMC 或带内地址串行执行，不同 endpoint 并发执行，避免同一设备互相抢会话。
- BMC 会话复用：同一 endpoint 下多个 BMC 任务共享一次登录会话。
- 页面生命周期检查：登录态、基础页面健康、截图前 ready 条件和截图后证据质量检查。
- 账户密码可用性检测：支持 BMC、SSH 或全量凭证预检。
- 结果和耗时统计：输出结果表、失败详情、证据审计和多维 timing 报表。
- Windows 开箱即用：发布包内置执行引擎、Python 运行时和 Chromium；源码模式可在 macOS/Linux/Windows 上运行。

## Windows 发布包使用

### 获取和解压

1. 打开 [GitHub Releases](https://github.com/minliny/BMC-Auto-Capture/releases)。
2. 下载最新的 Windows x64 完整 zip 包和同名 SHA256 文件。
3. 将 zip 解压到一个固定目录，例如 `C:\bmc-auto-capture`。
4. 如果 Windows 阻止运行，先在 PowerShell 中执行：

```powershell
Get-ChildItem "C:\bmc-auto-capture" -Recurse -File | Unblock-File
```

解压后的目录应包含：

```text
C:\bmc-auto-capture\
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
│   ├── api\
│   └── tasks.json
├── scripts\
├── run.py
├── tasks.json
└── output\
```

Windows 完整包不需要额外安装 Python、pip 依赖或 Chromium。

### 准备 Excel

1. 复制 `app\examples\task_template.xlsx` 到一个业务目录。
2. 打开 Excel，填写「设备信息」和「任务列表」两个工作表。
3. 保存后在启动菜单里选择该 Excel 文件。

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
| 任务名称 | 必须和 `tasks.json` 中的任务名称匹配 |
| 任务类型 | BMC / SSH / TELNET |
| 设备分组 | 限制任务只在指定分组设备上执行 |
| 是否启用 | 是/否 |

### 双击启动

双击 `启动.bat` 进入菜单：

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

推荐现场执行顺序：

1. 选择 `[5]`，设置 Excel 文件路径。
2. 选择 `[3]`，先做 BMC/SSH 网络连通性预检。
3. 选择 `[6]`，确认账号密码可登录。
4. 小规模验证时选择 `[1]` 顺序执行；批量跑数时选择 `[2]` 并发执行。
5. 执行结束后查看 `output\` 下的新结果目录。

## 命令行使用

Windows 发布包使用内置引擎：

```cmd
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode sequential
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-only
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-auth all
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full --verbose
```

源码模式使用：

```bash
python run.py --app-dir . --excel app/examples/task_template.xlsx --mode full
```

常用参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--app-dir` | app 目录，包含 `src/`、`config/`、`tasks.json` | 自动识别 |
| `--excel` | Excel 配置文件路径 | 自动查找模板 |
| `--config` | YAML 配置文件路径 | `config/default_config.yaml` |
| `--output` | 输出根目录 | YAML 配置 |
| `--mode` | `sequential` 顺序执行，`full` 动态调度并发执行 | `sequential` |
| `--concurrency` | 兼容旧脚本的并发参数，建议改用 worker 参数 | 无 |
| `--max-bmc-workers` | BMC endpoint 最大并发数 | YAML 配置 |
| `--max-ssh-workers` | SSH/TELNET endpoint 最大并发数 | YAML 配置 |
| `--ssh-command-timeout` | SSH 单条命令最大等待秒数 | YAML 配置 |
| `--ssh-idle-timeout` | SSH 输出空闲等待秒数 | YAML 配置 |
| `--bmc-page-timeout` | BMC 页面加载和选择器等待秒数 | YAML 配置 |
| `--bmc-artifact-profile` | `full` 保存完整证据，`fast` 只保存 PNG+HTML | `full` |
| `--preflight-only` | 只做 TCP 连通性预检，不执行任务 | 关闭 |
| `--preflight-target` | 预检对象：`all`、`bmc`、`ssh` | `all` |
| `--preflight-auth` | 凭证检测：`all`、`bmc`、`ssh` | 关闭 |
| `--no-preflight` | 跳过执行前连通性预检 | 关闭 |
| `--server` | 启动 Executor API Server | 关闭 |
| `--host` | Server 监听地址 | `127.0.0.1` |
| `--port` | Server 监听端口 | `8080` |
| `--runner` | API 执行模式：`fake` 或 `real` | `fake` |
| `--callback-transport` | 回调传输：`fake` 或 `http` | `fake` |
| `--executor-id` | Executor 标识 | `exec-default` |
| `--enable-real-runner` | 允许 API 请求执行真实 BMC/SSH 任务 | 关闭 |
| `--enable-debug-callback-receiver` | 启用内置调试回调接收器 | 关闭 |
| `--legacy-network-boot` | 启动旧兼容 API | 关闭 |
| `--verbose` | 输出 debug 日志 | 关闭 |

## Executor API 使用

启动 API：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner fake
```

需要执行真实 BMC/SSH 任务时显式启用 real runner：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner real --enable-real-runner
```

本机联调 callback 时可启用内置 debug receiver：

```cmd
runtime\bmc-engine.exe --app-dir app --server --host 0.0.0.0 --port 18000 --runner fake --enable-debug-callback-receiver
```

推荐服务端调用流程：

```text
1. POST /executor/v1/config/excel          上传 Excel，得到 excelHash
2. POST /executor/v1/plans                 使用 excelHash 启动任务批次，得到 planId
3. GET  /executor/v1/plans/{planId}        查询批次汇总
4. GET  /executor/v1/plans/{planId}/items  查询任务明细
```

常用调试接口：

```text
GET    /executor/v1/status
POST   /executor/v1/config/excel:path
POST   /debug/plan-item-statuses
GET    /debug/plan-item-statuses
DELETE /debug/plan-item-statuses
```

发布包自检：

```powershell
.\scripts\smoke_executor_runtime.ps1
```

该脚本会启动 `runtime\bmc-engine.exe`，检查 Executor API、OpenAPI 路由和内置 debug callback receiver。自检不需要真实设备。

## 输出结果

每次执行会在 `output\` 下生成一个带时间戳的目录：

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

常看文件：

| 文件 | 用途 |
|------|------|
| `final_result.csv` | 最终任务结果，便于按失败项筛查 |
| `failure_detail.csv` | 失败原因、异常信息和任务定位 |
| `evidence_audit.csv` | 检查证据是否缺失、是否回到登录页、是否有会话冲突 |
| `execution_summary.json` | 整次执行统计和并发效率 |
| `auth_check_result.csv` | 凭证检测结果 |

## 任务配置

任务定义在 `tasks.json` 中。Excel 的任务名称必须能匹配到这里的任务定义。

| 执行模式 | 说明 |
|----------|------|
| `BMC_URL` | 打开目标 BMC 页面，等待页面健康后截图并保存 HTML/MHTML/State |
| `BMC_ACTIONS` | 执行 goto/click/fill/press/wait/assert 等页面交互后截图 |
| `SSH_CMD` | SSH 登录后执行命令，保存命令输出和终端截图 |
| `TELNET_CMD` | TELNET 登录后执行命令 |

新增或调整任务时，优先修改 `tasks.json`，并在 Excel「任务列表」中启用对应任务。复杂 BMC 交互应在任务里配置明确的动作和 ready 条件，避免页面还未进入目标状态就截图。

## 调度和并发

```text
Excel → PlanGenerator → Preflight → RouteGuard
  → DynamicScheduler
       ├── BMC pool
       │   └── 同一 BMC endpoint 串行、不同 endpoint 并发
       └── INBAND pool
           └── SSH/TELNET endpoint 并发调度
  → ResultCollector → CSV/JSON/截图/HTML/审计文件
```

建议：

- 首次现场跑数先用 `--mode sequential` 或菜单 `[1]`。
- 网络和账号都稳定后再用 `--mode full` 或菜单 `[2]`。
- BMC 页面较慢时调大 `--bmc-page-timeout`。
- SSH 命令输出很长时调大 `--ssh-command-timeout`，必要时也调大 `--ssh-idle-timeout`。
- 同一台设备的 BMC 任务会串行执行，避免多浏览器会话互相挤掉登录态。

## 源码开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python run.py --app-dir . --excel app/examples/task_template.xlsx --mode full --no-preflight
```

离线验证：

```bash
python scripts/dev_verify_all.py --offline --output ./dev_verify_out
python scripts/dev_verify_all.py --offline --quick --output ./dev_verify_out
python -m pytest -q
```

## 更新发布包

1. 备份现场 Excel、`tasks.json` 自定义改动和 `output\` 结果目录。
2. 下载最新 Windows x64 完整 zip 包。
3. 解压到新目录，确认 `runtime\`、`app\`、`启动.bat` 都存在。
4. 复制现场 Excel 或自定义任务配置到新目录。
5. 先运行 `[3]` 网络预检和 `[6]` 账号密码检测，再执行正式任务。
6. 不建议在旧目录里混合覆盖不同发布包的 `runtime\` 和 `app\`，除非发布说明明确要求这样做。

## 常见问题

**双击 `启动.bat` 闪退**

在 CMD 或 PowerShell 中进入解压目录后手动执行 `启动.bat`，查看错误信息。常见原因是 zip 未完整解压、文件被 Windows 阻止，或 `runtime\bmc-engine.exe` 缺失。

**提示找不到执行引擎**

确认 `runtime\bmc-engine.exe` 存在。源码模式下确认本机 Python 可用，并从项目根目录执行 `python run.py ...`。

**BMC 任务大量失败**

先做网络连通性预检，再做账号密码检测。若 BMC 页面加载慢，调大 `--bmc-page-timeout`；若出现账号冲突，确认没有其他工具或浏览器正在使用同一账号登录。

**SSH 任务输出不完整**

确认设备命令本身会返回到下一条命令可输入的提示符。长输出命令应调大 `--ssh-command-timeout`；输出间隔较长时调大 `--ssh-idle-timeout`。

**只想快速拿截图**

BMC 可使用 `--bmc-artifact-profile fast`，只保存 PNG 和 HTML。需要完整取证、问题复盘或页面审计时使用默认 `full`。
