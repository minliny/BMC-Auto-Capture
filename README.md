# BMC Auto-Capture v2.0

面向服务器、BMC/iBMC、交换设备、SSH 命令采集场景的自动化测试证据采集平台。

## 核心能力

- **BMC 浏览器自动化** — Playwright 驱动，自动处理 HTTPS 证书/登录/弹窗/验证码，全页长截图 + HTML 保存
- **SSH/Telnet 命令采集** — Paramiko 纯 Python 实现，满足安全策略（仅 Python 可访问 22 端口）
- **Excel 驱动配置** — 设备信息 + 任务列表两个 Sheet，支持分组/多标签匹配
- **动态并发调度** — 根据实时 CPU/内存调整 BMC/SSH 工作池大小，同设备任务串行
- **规则引擎** — 基础规则（证据采集）+ 高级规则（结果验证），执行状态与规则状态分离
- **网络预检 + 路由保护** — TCP 端口探测，区分网络不通/端口拦截/超时；VPN 路由变更监控
- **双平台开箱即用** — macOS / Windows 预编译包，内置 Chromium，解压即用

## 快速开始（Windows 用户）

### 部署

从 [Release](https://github.com/minliny/BMC-Auto-Capture/releases) 下载两个包，解压到同一目录：

```
C:\bmc-auto-capture\
├── 启动.bat                ← 双击这个
├── bmc-auto-capture.exe    ← 引擎（不要直接双击）
├── config\                 ← 全局配置
├── examples\
│   └── 任务模板.xlsx        ← 修改设备信息
└── tasks.json              ← 任务执行定义
```

### 配置设备

打开 `examples/任务模板.xlsx` → 「设备信息」sheet，填入你的设备：

| 设备分类 | 设备名称 | 带外管理IP | 是否启用 | 带外管理用户名 | 带外管理密码 | 带内管理IP | 带内管理用户名 | 带内管理密码 | 标签 |
|----------|----------|-----------|---------|--------------|-------------|-----------|--------------|-------------|------|
| A3 | node-01 | 10.1.2.3 | 是 | admin | *** | | | | A3 |
| L1 | sw-01 | | 是 | | | 10.1.2.4 | root | *** | L1,交换机 |

任务列表已有 30 条预配置任务，一般不需要修改。

### 执行

双击 `启动.bat`，选择操作：

```
[1] 开始执行（顺序模式）     ← 设备逐个执行，最稳定
[2] 开始执行（并发模式）     ← 多设备并发执行，更高效
[3] 仅网络预检              ← 检测连通性，不执行任务
[4] 指定 Excel 文件         ← 切换配置文件
[5] 查看最近结果            ← 显示 result.csv
```

### 查看结果

```
output/
├── result.csv              ← 每设备每任务一行，含状态/原因/耗时
├── final_result.csv        ← 同上，按状态排序
├── summary_pivot.csv       ← 设备×任务 透视表
├── A3/
│   └── node-01/
│       └── A3 BMC首页截图/
│           ├── screenshot.png      ← 全页截图（含设备信息水印）
│           ├── page.html           ← 页面 HTML
│           └── task.log
└── L1/
    └── sw-01/
        └── uname查询/
            ├── output.txt          ← SSH 命令输出
            ├── terminal.png        ← 终端截图
            └── task.log
```

## 执行模式

| 模式 | 用途 | 配置位置 |
|------|------|----------|
| `BMC_URL` | 打开 BMC 页面 → 全页截图 + 保存 HTML | tasks.json |
| `SSH_CMD` | SSH 登录 → 执行命令 → 保存 TXT + 截图 | tasks.json |
| `BMC_ACTIONS` | 复杂页面交互：点击/填写/等待/断言 | tasks.json (JSON DSL) |
| `CUSTOM_SCRIPT` | 遗留脚本，默认禁用 | tasks.json (`_legacy` 标记) |

BMC_ACTIONS 支持的动作：

```
goto  click  fill  press  wait_for_selector  wait  screenshot  save_html  assert_visible
```

## 架构

```
Excel V2
  → PlanGenerator (设备 × 任务匹配，按分组/标签过滤)
  → ConnectivityPreflight (TCP 443/22 探测)
  → RouteGuard (VPN 路由变更监控)
  → DynamicScheduler (BMC/SSH 并发池，资源自适应)
  → Executor (BMC_URL / SSH_CMD / BMC_ACTIONS)
  → RuleEngine (可选规则检查)
  → ResultCollector (result.csv + 透视表)
```

```
src/
├── app.py                # 流水线编排 + EventBus
├── models/               # 数据模型 (Device/Task/TaskPlan/ExecutionResult)
├── loader/               # Excel 读取 + schema 校验
├── scheduler/            # 计划生成 + 动态调度 + 资源监控
├── executor/             # BMC/SSH 执行器 + 浏览器生命周期管理
├── connectivity/         # 网络预检 + Windows 路由保护
├── rules/                # 规则引擎（可插拔 Action 注册表）
└── output/               # 截图叠加/文件写入/结果收集/透视表
api/                       # FastAPI REST 服务（Agent 集成）
tui/                       # Textual 终端仪表盘
```

## 配置说明

### Excel — 任务列表（简化 8 列格式）

| 列 | 说明 |
|----|------|
| 任务序号 | 执行顺序 |
| 任务名称 | 与 tasks.json 匹配的关键字段 |
| 任务类型 | BMC / SSH |
| 设备分组 | A3 / L1 / L2 / RM211 |
| 标签 | 逗号分隔，设备须具备全部标签才匹配 |
| 截图保存目录 | 模板，支持 `{device_group}` `{device_name}` `{task_name}` |
| 图片命名格式 | 模板，支持 `{timestamp}` `{step}` 等 |
| 是否启用 | 是/否 |

任务的 URL、命令、超时、规则等执行细节在 `tasks.json` 中定义，对普通用户透明。

### default_config.yaml

```yaml
browser_headless: true          # true=无头模式(性能高) false=可见浏览器
preflight_enabled: true         # 执行前探测网络连通性
route_guard_enabled: true       # 监控 VPN 路由变化
tcp_connect_timeout: 5.0        # 预检超时(秒)
max_bmc_workers: 4              # BMC 最大并发数
max_ssh_workers: 8              # SSH 最大并发数
resource_check_interval: 5.0    # 资源采样间隔(秒)
output_root: "./output"
```

### tasks.json

Agent 可远程推送更新此文件，无需修改 Excel：

```json
{
  "tasks": {
    "A3 BMC首页截图": {
      "task_type": "BMC",
      "execution_mode": "BMC_URL",
      "command_or_url": "/UI/Static/#/navigate/home",
      "timeout_seconds": 60
    }
  }
}
```

## 高级用法

### CLI 命令行

```batch
# 顺序执行（默认）
bmc-auto-capture.exe --excel 任务模板.xlsx

# 动态并发
bmc-auto-capture.exe --excel 任务模板.xlsx --mode full

# 仅网络预检
bmc-auto-capture.exe --excel 任务模板.xlsx --preflight-only

# 详细日志
bmc-auto-capture.exe --excel 任务模板.xlsx --verbose

# 使用自定义配置
bmc-auto-capture.exe --excel 任务模板.xlsx --config my_config.yaml
```

Excel 文件放在当前目录或 `examples/` 下时可省略 `--excel` 参数。

### API 模式（Agent 集成）

```batch
uvicorn api.server:app --host 0.0.0.0 --port 8080
```

| 端点 | 用途 |
|------|------|
| `POST /config/upload-excel` | 上传并校验 Excel |
| `POST /execute/start` | 启动执行，返回 execution_id |
| `GET /execute/{id}/stream` | SSE 实时推送执行状态 |
| `GET /execute/{id}/results` | 下载合并后的 result.csv |
| `GET /execute/{id}/screenshots?task=xxx&limit=2` | 获取每用例 1-2 台设备截图 |
| `POST /execute/{id}/stop` | 停止执行 |

## 版本更新

Runtime 包（Python + Chromium + 依赖）很少更新，App 包（脚本 + 配置）频繁更新：

```
首次: 下载 runtime-*.7z + app-*.zip → 解压到同一目录
更新: 下载最新 app-*.zip → 覆盖 src/ config/ tasks.json 启动.bat
```

## CI/CD

推送 `v*` tag 自动触发 GitHub Actions：

```
macOS arm64  → bmc-runtime-vX.X.X-macos-arm64.tar.gz
Windows x64  → bmc-runtime-vX.X.X-win-x64.7z
跨平台       → bmc-app-vX.X.X.zip
```

## 已收录任务

| 模式 | 数量 | 内容 |
|------|------|------|
| BMC_URL | 10 | 首页截图、资产摘要、网络适配器、门限传感器、电源/风扇信息、RAID 配置、上电/下电、RM211 网络 |
| SSH_CMD | 10 | uname、npu-smi、电子标签、CPLD 版本、光口状态、光模块、温度、端口查询 |
| CUSTOM_SCRIPT | 10 | 上电测试(内存/CPU)、iBMC IP 设置、部件信息(CPU/NPU/内存)、冗余测试、ClearForeign(高风险) |

## 安全约束

- SSH 全部走 Paramiko（Python socket），满足仅 Python 可访问 22 端口的安全策略
- 端口拦截与网络不通独立标记：`EXEC_SKIPPED_PORT_BLOCKED` vs `EXEC_SKIPPED_PRECHECK_FAILED`
- `拔插硬盘后清除Foreign配置` 标记为高风险，永久禁用
- Agent 导入不直接覆盖正式 Excel，须先预览确认
