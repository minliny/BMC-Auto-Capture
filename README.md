# BMC Auto-Capture v0.2.4-RC5

面向服务器 BMC/iBMC、交换设备、SSH/TELNET 命令采集场景的自动化测试证据采集平台。

## 核心能力

- **BMC 浏览器自动化** — Playwright 驱动，自动处理 HTTPS 证书/登录/弹窗/验证码，全页截图+HTML/MHTML/State JSON 保存
- **SSH/TELNET 命令采集** — Paramiko 纯 Python 实现，满足安全策略（仅 Python 可访问 22/23 端口）
- **Endpoint-aware 动态调度** — 同一 BMC/INBAND endpoint 串行执行，不同 endpoint 并发，BMC/INBAND 分池调度，支持跨进程文件锁
- **BMC 会话复用** — 同一 endpoint 下多个 BMC 任务共享一个登录会话，login once / execute many / logout once
- **5-Gate 页面生命周期** — OPENED → AUTHENTICATED → PAGE_BASIC_HEALTH → READY_FOR_CAPTURE → SCREENSHOT_VALIDATED，区分 visible/hidden loading/error
- **证据审计** — evidence_audit.csv 扫描已保存 HTML/MHTML/log，检测会话冲突/登录页返回/页面加载异常
- **账户密码可用性检测** — `--preflight-auth all/bmc/ssh` 预检 BMC/SSH 认证凭证
- **timing 报表** — plan_timing.csv / device_timing.csv / endpoint_timing.csv / execution_summary.csv+json
- **双平台开箱即用** — Windows 预编译包内置 Chromium，macOS 源码运行

## 快速开始（Windows）

### 部署

从 [Release](https://github.com/minliny/BMC-Auto-Capture/releases) 下载 `bmc-auto-capture-vX.X.X-win-x64.zip`，解压：

```
C:\bmc-auto-capture\
├── 启动.bat                  ← 双击运行
├── runtime\                  ← 引擎 + Python 运行时 + Chromium
│   ├── bmc-engine.exe
│   ├── _internal\
│   └── playwright_browsers\
├── app\                      ← 脚本 + 配置 + 示例
│   ├── src\
│   ├── config\
│   ├── examples\
│   └── tasks.json
├── run.py                    ← Python 源码入口（引擎 fallback）
└── output\                   ← 结果输出（自动创建）
```

RC 版本采用 runtime/app 分层构件：

```text
bmc-runtime-${tag}-win-x64.7z
bmc-app-${tag}.zip
```

首次使用需同时解压两个包，并保持 `runtime/`、`app/`、`run.py`、`启动.bat`
位于同一根目录。后续是否可只更新 app 包，以 Release Notes 中的
“依赖包可复用的最早版本 / Minimum reusable runtime package version”为准。

### 配置设备

打开 `app/examples/task_template.xlsx` → 「设备信息」sheet：

| 设备分类 | 设备名称 | 带外管理IP | 是否启用 | 带外管理用户名 | 带外管理密码 | 带内管理IP | 带内管理用户名 | 带内管理密码 |
|----------|----------|-----------|---------|--------------|-------------|-----------|--------------|-------------|
| A3 | node-01 | 10.1.2.3 | 是 | admin | *** | | | |
| L1 | sw-01 | | 是 | | | 10.1.2.4 | root | *** |

### 执行

双击 `启动.bat`，菜单选择：

```
[1] 顺序执行（逐台设备，最稳定）
[2] 并发执行（多 endpoint 并发，更高效）
[3] 网络连通性预检（TCP 端口探测）
[4] 账户密码可用性检测（BMC/SSH 凭证验证）
[5] 仅 BMC 账户检测
[6] 仅 SSH 账户检测
[7] 设定 Excel 文件路径
[8] Debug 模式（详细日志）
[R] 直接命令模式
[0] 启动 API Server
```

### 查看结果

```
output/20260608_120000/
├── result.csv                    ← 每设备每任务一行
├── final_result.csv              ← 同 result，按状态排序
├── summary_pivot.csv             ← 设备 × 任务透视表
├── failure_detail.csv            ← 失败任务详情
├── plan_timing.csv               ← 每个 plan 完整耗时
├── device_timing.csv             ← 每设备耗时汇总
├── endpoint_timing.csv           ← 每 endpoint 耗时汇总
├── execution_summary.csv+json    ← 整次执行总览
├── evidence_audit.csv            ← 证据质量审计
├── auth_check_result.csv         ← 账户密码检测结果（如运行预检）
└── A3/node-01/A3 BMC首页截图/
    ├── node-01_A3 BMC首页截图_*.png
    ├── html/node-01_A3 BMC首页截图_*.html
    ├── html/node-01_A3 BMC首页截图_*.mhtml
    ├── html/node-01_A3 BMC首页截图_*.evidence.html
    ├── html/node-01_A3 BMC首页截图_*.state.json
    └── raw/  (可选)
```

## CLI 命令行

```cmd
:: 顺序执行
bmc-engine.exe --app-dir app --excel tasks.xlsx --mode sequential

:: 并发执行（推荐）
bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full

:: 网络连通性预检
bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-only

:: 账户密码检测（all/bmc/ssh）
bmc-engine.exe --app-dir app --excel tasks.xlsx --preflight-auth all

:: API Server
bmc-engine.exe --app-dir app --server --host 127.0.0.1 --port 8080

:: API Server 真实执行模式（受控网络内使用）
bmc-engine.exe --app-dir app --server --runner real --enable-real-runner

:: 详细日志
bmc-engine.exe --app-dir app --excel tasks.xlsx --mode full --verbose
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--app-dir` | app 目录（含 src/config/tasks.json） | — |
| `--excel` | Excel 配置文件路径 | 自动查找 |
| `--mode` | sequential / full | sequential |
| `--output` | 输出根目录 | YAML 配置 |
| `--max-bmc-workers` | BMC 最大并发 endpoint 数 | 4 |
| `--max-ssh-workers` | SSH 最大并发 endpoint 数 | 8 |
| `--preflight-only` | 仅 TCP 预检，不执行任务 | — |
| `--preflight-target` | 预检对象：all / bmc / ssh | all |
| `--preflight-auth` | 凭证检测：all / bmc / ssh | — |
| `--no-preflight` | 跳过连通性预检 | — |
| `--server` | API Server 模式 | — |
| `--host` | Server 监听地址 | 127.0.0.1 |
| `--port` | Server 端口 | 8080 |
| `--runner` | API runner：fake / real | fake |
| `--enable-real-runner` | 允许 API 触发真实 BMC/SSH 执行 | — |
| `--verbose` | Debug 日志 | — |

## 调度架构

```
Excel → PlanGenerator → Preflight → RouteGuard
  → DynamicScheduler
       ├── BMC pool (max_bmc_workers endpoint groups)
       │     └── BMCEndpointSessionRunner (login once, execute many)
       │           ├── Gate: OPENED
       │           ├── Gate: AUTHENTICATED
       │           ├── Gate: PAGE_BASIC_HEALTH
       │           ├── Gate: READY_FOR_CAPTURE
       │           ├── Gate: SCREENSHOT_VALIDATED
       │           └── evidence_audit
       └── INBAND pool (max_ssh_workers endpoint groups)
             ├── SSH executor (exec_command / interactive_shell)
             └── TELNET executor

  → ResultCollector → result.csv + timing reports + evidence audit
```

- 同一 endpoint_key（如 `BMC:10.0.0.1:443`）串行，不同 endpoint 并发
- BMC 和 INBAND 分池，互不阻塞
- ResourceRegistry（进程内）+ FileLock（跨进程）双重保障
- BMC 同一 endpoint 下多任务共享登录会话（不再每任务重复 login/logout）

## 配置说明

### default_config.yaml

```yaml
browser_headless: true           # true=无头模式 false=可见浏览器
preflight_enabled: true          # 执行前 TCP 端口探测
route_guard_enabled: true        # VPN 路由变更监控
tcp_connect_timeout: 5.0         # 预检超时（秒）
max_bmc_workers: 4               # BMC 并发 endpoint group 数
max_ssh_workers: 8               # INBAND 并发 endpoint group 数
resource_check_interval: 5.0     # CPU/内存采样间隔（秒）
output_root: "./output"
```

## 任务类型

| 模式 | 说明 | 配置位置 |
|------|------|----------|
| BMC_URL | 打开 BMC 页面 → 全页截图+HTML+MHTML+State JSON | tasks.json |
| BMC_ACTIONS | DSL 驱动页面交互：goto/click/fill/press/screenshot/assert | tasks.json |
| SSH_CMD | SSH 登录 → 执行命令 → TXT+终端截图 | tasks.json |
| TELNET_CMD | TELNET 登录 → 执行命令（port 23） | tasks.json |

## 开发

### 环境

```bash
# macOS
pip install -r requirements.txt
python -m playwright install chromium

# 运行
python run.py --app-dir . --excel app/examples/task_template.xlsx --mode full --no-preflight
```

### 离线验证

```bash
# 完整离线验证（无需真实设备）
python scripts/dev_verify_all.py --offline --output ./dev_verify_out

# 快速模式（跳过 subprocess 慢测试）
python scripts/dev_verify_all.py --offline --quick --output ./dev_verify_out

# 源码级验证
python verify_offline.py
```

### 测试

```
tests/
├── test_endpoint_scheduling.py       # 15 tests: endpoint-aware 调度
├── test_bmc_generic_gates.py         # 52 tests: 5-gate 页面生命周期
├── test_file_lock.py                 # 8 tests: 跨进程文件锁
├── test_file_lock_windows.py         # 7 tests: Windows 兼容性
├── test_bmc_session_reuse.py         # 16 tests: session 复用
├── test_bmc_health_check.py          # 20 tests: 原健康检查
├── test_timing_reports.py            # E2E: timing 报表
├── test_resource_registry.py         # 22 tests: Registry
├── test_endpoint_key.py              # 17 tests: endpoint_key 规则
├── test_api_run_with_plans_*.py      # API mock 测试
├── test_preflight_auth_cli.py        # 17 tests: CLI 参数
├── test_128_plans.py                 # 大规模集成测试
└── fakes.py                          # 统一 fake/mock 层
```

## 版本更新

Runtime 层（Python + Chromium + 依赖）很少更新，App 层（脚本+配置）频繁更新：

```
正式版: 下载 bmc-auto-capture-${tag}-win-x64.zip → 解压后直接运行
RC 首次: 解压 bmc-runtime-${tag}-win-x64.7z + bmc-app-${tag}.zip
RC 更新: runtime 满足最早可复用版本时，仅覆盖 app/ run.py 启动.bat
```

## CI/CD

推送 `v*` tag 触发 GitHub Actions Windows x64 构建：

- 正式版发布 `bmc-auto-capture-${tag}-win-x64.zip`
- RC 发布 `bmc-runtime-${tag}-win-x64.7z` 和 `bmc-app-${tag}.zip`
- 每个构件附带同名 `.sha256`
- RC manifest 和 Release Notes 记录最早可复用 runtime 版本

## 安全

- SSH 全部走 Paramiko（Python socket），满足仅 Python 可访问 22 端口的安全策略
- 端口拦截与网络不通独立标记
- `拔插硬盘后清除Foreign配置` 标记为高风险，永久禁用
