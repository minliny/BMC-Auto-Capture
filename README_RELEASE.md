# BMC Auto-Capture v0.2.4-RC5 — 使用说明

## 一、简介

BMC Auto-Capture 是一个 BMC/SSH 自动化测试证据采集平台，支持：

- BMC 浏览器截图（Playwright + Chromium）
- SSH/TELNET 命令执行 + 终端截图（Paramiko）
- endpoint-aware 并发调度（同 endpoint 串行，不同 endpoint 并发）
- BMC 会话复用（同 endpoint 多任务共享一次登录）
- 5-Gate 页面健康检查（自动检测登录页/账号冲突/loading/error）
- 账户密码可用性预检

## 二、目录结构

```
bmc-auto-capture/
├── 启动.bat                  ← 双击运行
├── run.py                    ← Python 源码入口（引擎 fallback）
├── runtime/
│   ├── bmc-engine.exe        ← 执行引擎（PyInstaller 编译）
│   ├── _internal/            ← Python 运行时 + 依赖库
│   └── playwright_browsers/  ← Chromium 浏览器
├── app/
│   ├── src/                  ← 业务源码
│   ├── config/               ← YAML 配置
│   │   └── default_config.yaml
│   ├── examples/             ← Excel 模板
│   │   └── task_template.xlsx
│   ├── api/                  ← API Server
│   └── tasks.json            ← 任务定义
└── output/                   ← 执行结果（自动生成）
```

## 三、使用方法

### 方式一：双击运行（推荐）

1. 双击 `启动.bat`
2. 菜单 `[7]` 设定 Excel 路径（支持拖拽）
3. 菜单 `[1]` 顺序执行 或 `[2]` 并发执行
4. 结果在 `output/` 目录

### 方式二：账户密码可用性检测

```cmd
启动.bat
→ 选择 [4] 账户密码可用性检测 (all)
→ 或 [5] 仅 BMC / [6] 仅 SSH
```

结果保存在 `output/auth_check_result.csv`。

### 方式三：命令行直接执行

```cmd
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full
runtime\bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-auth all
```

### 方式四：Executor API 服务（推荐用于远程调用）

Executor API 是默认的 server 模式，启动新版 API（基于 FastAPI + uvicorn），
**不需要系统安装 Python**。不需要额外启动 mock callback server，内置 debug callback receiver。

```cmd
REM 默认启动 Executor API（不是旧 Network Boot API）
runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --runner fake

REM 推荐用于测试：开启内置 debug callback receiver，无需额外 Python 进程
runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --runner fake --enable-debug-callback-receiver

REM 如需旧 Network Boot API（仅 health/ping/version），加 --legacy-network-boot
runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --legacy-network-boot
```

提供端点（默认 Executor API 模式）：
- `GET /executor/v1/status` — Executor 状态
- `POST /executor/v1/config/excel:path` — 设置最新 Excel
- `POST /executor/v1/plans/{planId}:run` — 启动 plan 执行
- `GET /executor/v1/plans/{planId}/runs/{runId}` — 查询执行进度
- `GET /health` — 兼容健康检查
- `GET /version` — 兼容版本信息
- `GET /network/ping` — 兼容网络检测
- `GET /routes` — 路由列表

如果启动时加 `--enable-debug-callback-receiver`，额外提供：
- `POST /debug/plan-item-statuses` — 接收 plan item 状态回调
- `GET /debug/plan-item-statuses` — 查询已收到回调
- `DELETE /debug/plan-item-statuses` — 清空回调记录

> **注意：** `--legacy-network-boot` 参数可启动旧版 API（仅 health/ping/version/routes），
> 默认不传时启动的是完整 Executor API（含 plan run、callback 等能力）。

**状态枚举分层：**

| 上下文 | 字段 | 允许值 |
|--------|------|--------|
| Plan Run 级别 | `status` | `ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED` |
| Run Item 查询 | `items[].status` | `PENDING` / `RUNNING` / `SUCCESS` / `FAILED` |
| Item 状态回调 | `status` | `SUCCESS` / `FAILED` |
| Debug Callback | `payload.status` | `SUCCESS` / `FAILED` |

开箱即用验证：

```powershell
.\scripts\smoke_executor_runtime.ps1
```

该脚本启动 `runtime\bmc-engine.exe`，检查所有 Executor API 端点，
使用内置 debug callback receiver 接收 plan run 回调，验证 total/success/failed。
**不需要系统安装 Python。**

也可以双击 `启动.bat` 后选 `[0] 启动 Executor API`。

## 四、常用 CLI 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--app-dir` | app 目录路径 | 必填 |
| `--excel` | Excel 文件路径 | 自动查找 |
| `--mode` | sequential / full | sequential |
| `--max-bmc-workers` | BMC 并发 endpoint 数 | 4 |
| `--max-ssh-workers` | SSH 并发 endpoint 数 | 8 |
| `--preflight-only` | 仅 TCP 预检 | — |
| `--preflight-target` | 预检对象 all/bmc/ssh | all |
| `--preflight-auth` | 凭证检测 all/bmc/ssh | — |
| `--no-preflight` | 跳过预检 | — |
| `--server` | Executor API Server 模式（默认新版 API） | — |
| `--host` | Server 地址 | 0.0.0.0 |
| `--port` | Server 端口 | 18000 |
| `--runner` | 执行器模式: fake / real | fake |
| `--callback-transport` | 回调传输: fake / http | fake |
| `--executor-id` | Executor 唯一标识 | exec-default |
| `--enable-debug-callback-receiver` | 启用内置调试回调接收器 | — |
| `--legacy-network-boot` | 启用旧版 Network Boot API（仅 health/ping） | — |
| `--verbose` | Debug 日志 | — |

## 五、Excel 配置

Excel 需两个工作表：

**设备信息 sheet：**

| 列 | 说明 |
|----|------|
| 设备分类 | A3 / L1 / L2 / RM211 等 |
| 设备名称 | 唯一标识 |
| 是否启用 | 是/否 |
| 带外管理IP | BMC OOB IP |
| 带外管理用户名 | BMC 用户名 |
| 带外管理密码 | BMC 密码 |
| 带内管理IP | SSH/TELNET IP |
| 带内管理用户名 | SSH 用户名 |
| 带内管理密码 | SSH 密码 |

**任务列表 sheet：**

| 列 | 说明 |
|----|------|
| 任务序号 | 执行顺序 |
| 任务名称 | 与 tasks.json 匹配 |
| 任务类型 | BMC / SSH / TELNET |
| 设备分组 | 限制只在指定分组设备上执行 |
| 是否启用 | 是/否 |

模板：`app/examples/task_template.xlsx`

## 六、输出文件说明

```
output/<timestamp>/
├── result.csv                    ← 所有任务结果（1 行/设备/任务）
├── final_result.csv              ← 同上，按状态排序
├── summary_pivot.csv             ← 设备×任务 透视表
├── failure_detail.csv            ← 失败任务详情
├── plan_timing.csv               ← 每 plan 完整耗时
├── device_timing.csv             ← 每设备耗时汇总
├── endpoint_timing.csv           ← 每 endpoint 耗时汇总
├── execution_summary.json        ← 整次执行总览（含 parallel_efficiency）
├── execution_summary.csv         ← 同上 CSV 版
├── evidence_audit.csv            ← 证据质量审计（含 account logged elsewhere 检测）
├── auth_check_result.csv         ← 凭证检测结果（如运行 preflight-auth）
└── <设备分组>/<设备名称>/<任务名称>/
    ├── *.png                     ← 全页截图（含地址栏合成）
    ├── html/*.html               ← 保存的 HTML
    ├── html/*.mhtml              ← MHTML 归档
    ├── html/*.evidence.html      ← 渲染后 DOM（离线可查看）
    ├── html/*.state.json         ← 页面状态快照
    ├── page_health_debug.json    ← 页面健康检查详细结果（如有 FAIL/WARN）
    └── raw/                      ← 原始截图（合成前）
```

## 七、常见问题

**Q: 双击启动.bat 闪退**
A: 右键管理员运行 CMD，cd 到项目目录后手动执行 `启动.bat`，查看错误信息。

**Q: 提示"找不到执行引擎"**
A: 确保 `runtime/bmc-engine.exe` 存在。如使用源码模式，需 `python run.py`。

**Q: 默认启动的是旧版 Network Boot API 还是新版 Executor API？**
A: 默认启动新版 Executor API。如需旧版 Network Boot API，加 `--legacy-network-boot`。

**Q: 客户机器没有 Python，能否启动 Executor API？**
A: 可以。使用 `runtime\bmc-engine.exe --server` 或 `启动.bat --server`，无需系统安装 Python。
   `python scripts/*.py` 只是开发态命令。

**Q: 测试 Plan Run 时需要 mock callback receiver，但没有 Python 怎么办？**
A: 使用内置 debug callback receiver。启动时加 `--enable-debug-callback-receiver`，
   然后将 `itemStatusUrl` 指向 `http://127.0.0.1:18000/debug/plan-item-statuses`，
   无需额外 Python 进程。支持 POST/GET/DELETE 三个路由。

**Q: Windows 安全提示**
A: 在 PowerShell 中运行：
```powershell
Get-ChildItem "<解压目录>" -Recurse -File | Unblock-File
```

**Q: BMC 任务全部失败**
A: 先用菜单 `[3]` 网络连通性预检，确认 BMC IP 可达。再用 `[4]` 账户密码检测确认凭证正确。

**Q: 截图出现"账号已在别处登录"**
A: 升级到 v0.2.4+。新版 BMC session 复用 + 5-Gate 健康检查会自动检测并 FAIL，不再出现假阳性。

**Q: SSH 任务报 "Error reading SSH protocol banner"**
A: 设备 SSH 端口（22）不通或 IP 不可达。先用 `[3]` 预检确认。

## 八、更新升级

App 层更新（不涉及 runtime）：

1. 下载最新 `bmc-auto-capture-vX.X.X-win-x64.zip`
2. 解压后仅复制 `app/`、`run.py`、`启动.bat` 覆盖旧文件
3. `runtime/` 目录无需更新（除非版本号大版本跳跃）
