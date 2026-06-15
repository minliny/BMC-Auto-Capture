# BMC Auto-Capture v0.2.4 — 使用说明

## 一、简介

BMC Auto-Capture 是一个 BMC/SSH 自动化测试证据采集平台，支持：

- BMC 浏览器截图（Playwright + Chromium）
- SSH/TELNET 命令执行 + 终端截图（Paramiko）
- endpoint-aware 并发调度（同 endpoint 串行，不同 endpoint 并发）
- BMC 会话复用（同 endpoint 多任务共享一次登录）
- 5-Gate 页面健康检查（自动检测登录页/账号冲突/loading/error）
- 账户密码可用性预检

新增 SSH/BMC 任务的配置方法见 [任务添加指南](docs/TASK_ADDING_GUIDE.md)。

## 二、目录结构

```
bmc-auto-capture/
├── 启动.bat                  ← 双击运行
├── run.py                    ← Python 源码入口（引擎 fallback）
├── runtime/
│   ├── bmc-engine.exe        ← 执行引擎（PyInstaller 编译）
│   ├── _internal/            ← Python 运行时 + 依赖库
│   ├── playwright_browsers/  ← Chromium 浏览器
│   └── build_info.json       ← runtime 构建及兼容版本信息
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

`--concurrency N` 是兼容参数。`N > 1` 时会自动使用 full 动态调度，并在未显式指定 worker 时同时映射到 BMC/SSH worker。新脚本建议直接使用 `--mode full --max-bmc-workers N --max-ssh-workers M`。

BMC 证据默认使用 `--bmc-artifact-profile full`，会保存 PNG、HTML、evidence.html、MHTML 和 state JSON。现场批量跑得很慢且只需要最终截图/HTML 时，可显式使用 `--bmc-artifact-profile fast`，该模式只保存 PNG + HTML。

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

### 外部 Plan API（推荐服务端使用，基于 excelHash + planId）

服务端只需 `excelHash + planId`，无需理解 `runId/jobId` 等内部概念。

```text
1. POST /executor/v1/config/excel          → 上传 Excel，得到 excelHash
2. POST /executor/v1/plans with excelHash  → 启动任务批次，得到 planId
3. GET  /executor/v1/plans/{planId}?excelHash=...  → 查询批次汇总
4. GET  /executor/v1/plans/{planId}/items?excelHash=...  → 查询任务明细
```

### 旧接口（保留兼容，新服务端不推荐使用）

```text
- POST /executor/v1/plans/{planId}:run
- GET  /executor/v1/runs/{runId}              （内部/调试兼容，不推荐服务端主链路使用）
- GET  /executor/v1/runs/{runId}/items        （内部/调试兼容，不推荐服务端主链路使用）
```

### 所有端点列表
- `GET /executor/v1/status` — Executor 状态
- `POST /executor/v1/config/excel:path` — 设置最新 Excel
- `POST /executor/v1/plans/{planId}:run` — 启动 plan 执行
- `GET /executor/v1/plans/{planId}` — 查询批次汇总
- `GET /executor/v1/plans/{planId}/items` — 查询任务明细
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
| `--concurrency` | 兼容参数，>1 时隐式 full 并映射缺省 worker | — |
| `--max-bmc-workers` | BMC 并发 endpoint 数 | 4 |
| `--max-ssh-workers` | SSH 并发 endpoint 数 | 8 |
| `--bmc-artifact-profile` | BMC 证据模式：full 或 fast | full |
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

### 正式版

正式版发布一个 Windows x64 开箱即用完整包：

```text
bmc-auto-capture-${tag}-win-x64.zip
```

解压后直接双击 `启动.bat`，或运行 `runtime\bmc-engine.exe`。

### RC 版

RC 发布两个 Windows x64 构件：

```text
bmc-runtime-${tag}-win-x64.7z
bmc-app-${tag}.zip
```

首次使用 RC：

1. 解压 runtime 依赖包
2. 解压 app 脚本包
3. 保持 `runtime/`、`app/`、`run.py`、`启动.bat` 位于同一根目录
4. 运行 `启动.bat`

更新 RC 时，如果 release notes 或 `release_manifest.json` 声明现有 runtime
仍可复用，只需覆盖 `app/`、`run.py`、`启动.bat`。

```text
依赖包可复用的最早版本：以 release notes / release_manifest.json 为准
Minimum reusable runtime package version: see release notes / release_manifest.json
```

以下任一内容变化时，应更新 runtime 包并调整最早可复用版本：

1. `requirements.txt`
2. Python 版本
3. PyInstaller / frozen engine 构建配置
4. Playwright / Chromium 版本
5. `runtime/_internal`
6. `runtime/playwright_browsers`
7. `bmc-engine.exe` 构建输入

以下变化通常只需要更新 app 包：

1. `app/src`
2. `app/config`
3. `app/examples`
4. `app/api`
5. `app/assets`
6. `app/tasks.json`
7. `run.py`
8. `启动.bat`
9. `README_RELEASE.md`

所有发布构件均附带同名 `.sha256` 文件：

```text
<sha256>  <filename>
```

---

## 当前 RC 预留问题

本 RC tag：以当前 GitHub Release / release_manifest.json 为准

### Security / 代码冻结前阻断项

1. **FZ-AUDIT-001：MHTML 结构化敏感字段值可在解码后落盘**
   - 状态：预留
   - 说明：opaque secret 场景下，敏感 key 对应的随机 value 仍可能在 MHTML 解码后出现。
   - 后续动作：实现 HTML/MHTML 结构化上下文脱敏，并补充 opaque secret 回归测试。

2. **FZ-AUDIT-002：callback response / outbox JSON 敏感 key 对应 value 泄漏**
   - 状态：预留
   - 说明：opaque secret 场景下，敏感 key 对应 value 可能进入 log / outbox jsonl。
   - 后续动作：JSON parse 后递归脱敏 value，parse 失败再走文本 fallback，并补充 caplog/outbox opaque secret 测试。

### Release / 发布项

3. **Frozen 安全实现一致性**
   - 状态：预留
   - 说明：当前 frozen engine / runtime 包需要在安全修复完成后重新构建并验证。
   - 后续动作：重建 Windows frozen engine，执行 safety parity。

4. **Windows 实际打包与 CMD 启动验证**
   - 状态：预留
   - 说明：当前构建脚本已通过静态检查和 dry-run，仍需 GitHub Actions Windows runner 实际产包验证。
   - 后续动作：运行 Windows release workflow，验证 full zip、runtime 7z、app zip、sha256、启动.bat。

### Field / 现场项

5. **A3/L1/L2 最小真实复测**
   - 状态：预留
   - 说明：代码侧 A3/L1/L2 命令路由通过，仍需真实硬件确认。
   - 后续动作：A3×1、L1×1、L2×1 最小复测。
