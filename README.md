# BMC Auto-Capture

面向服务器 BMC/iBMC、交换设备和 SSH 场景的自动化测试证据采集平台。

项目把设备配置、任务定义、协议执行、质量判断和证据输出拆开管理：使用 Excel 选择设备和任务，使用 tasks.json 描述页面操作或命令，由执行引擎按管理 endpoint 安全调度，最后输出可复核的结果、原始证据和时序报告。

> 本 README 的功能口径对应 GitHub main 分支的 v0.2.6。它只描述已合入主线的实现，不把实验分支或本地未提交文件表述为发布能力。

## 解决什么问题

- 批量登录 BMC/iBMC 管理页面，完成页面导航、交互和截图取证。
- 批量通过 SSH 采集服务器、网络设备的命令输出。
- 避免同一个 BMC/SSH endpoint 被并发任务抢占会话。
- 将截图、页面快照、命令输出、失败原因和耗时统一沉淀为可审计结果。
- 允许上层平台通过 Executor API 上传 Excel、启动计划、查询状态和接收回调。

## 核心能力

| 能力 | 当前主线实现 |
| --- | --- |
| BMC 浏览器自动化 | Playwright 驱动 Chromium，处理登录、证书警告页、弹窗、页面跳转和任务动作；按任务与 artifact profile 输出 PNG、HTML、best-effort MHTML、State JSON 等证据。 |
| SSH 命令采集 | 使用 Paramiko 连接设备，执行命令或交互 shell，保存文本与终端式截图。 |
| endpoint 感知调度 | 同一个 BMC 或 SSH endpoint 串行，不同 endpoint 并行；BMC 与带内 SSH 使用独立 worker pool。 |
| BMC 会话复用 | 在 --mode full 下，同一 BMC endpoint 的多个任务组成会话组：登录一次、按顺序执行、结束后注销。 |
| 页面质量检查 | BMC 经过 OPENED、AUTHENTICATED、PAGE_BASIC_HEALTH、READY_FOR_CAPTURE、SCREENSHOT_VALIDATED 五阶段检查。 |
| 预检与控制 | TCP 连通性预检、路由变化监控、暂停、停止、超时和失败重试。 |
| 结果与验收材料 | 输出 CSV/JSON/页面证据/时序/证据审计；可从已有运行结果导出验收 DOCX、证据 ZIP 和回填报告。 |
| 外部调用 | Executor API 支持 Excel 上传、Plan-Run、状态查询和回调重试。 |

### 当前边界

- 主线任务库已验证的是 BMC_URL、BMC_ACTIONS 和 SSH_CMD。当前 tasks.json 有 19 个 BMC 任务和 10 个 SSH 任务。
- 任务模型和调度器识别 TELNET_CMD 与端口 23，但 v0.2.6 执行器仍调用 Paramiko SSHClient，未发现 telnetlib3 的运行时调用，也没有当前 TELNET 任务定义。因此不要将 TELNET 视为已验证的原生协议能力。
- TCP 预检只探测 BMC 443 和 SSH 22；它不是 ICMP Ping、应用健康检查，也不预检 TELNET 23。

## 架构设计

    Excel（设备、启用状态、任务选择）       tasks.json（页面动作、命令与条件）
                      │                              │
                      └──────── 加载、校验、计划生成 ─┘
                                      │
                         TCP 预检与 RouteGuard
                                      │
                    endpoint FIFO 队列与动态调度器
                         │                         │
               BMC worker pool                 SSH worker pool
                         │                         │
        Playwright / Chromium               Paramiko / SSH
        登录、页面动作、取证               命令、交互 shell、取证
                         └───────────────┬─────────┘
                                         │
                         统一结果判定与证据审计
                                         │
        CSV / JSON / PNG / HTML / MHTML / TXT / DOCX / 回调

### 核心执行链路

1. 从 Excel 读取设备信息和启用任务，从 tasks.json 读取任务细节。
2. 校验表头、任务匹配、设备分组和配置字段。
3. 将每个“设备 × 任务”展开为 TaskPlan。
4. 默认执行 TCP 连通性预检；不可达的协议任务标记为跳过。
5. 按 endpoint key 排队：
   - BMC：BMC:<IP>:443
   - SSH：INBAND:<IP>:22
   - TELNET 模型路径：INBAND:<IP>:23，当前不作为已验证协议执行能力。
6. 不同 endpoint 被分配到 BMC 或 SSH worker pool；同 endpoint 保持 FIFO 串行。
7. BMC 进入浏览器会话组，SSH 进入协议执行器。
8. 汇总执行状态、页面检查、工件状态和最终 verdict，再写入证据和报表。

### 并发和会话模型

- 同 endpoint 的任务串行，避免同一 BMC 被多浏览器会话抢占，或同一 SSH endpoint 被并发命令污染。
- 不同 endpoint 可以并行。默认最大值由 config/default_config.yaml 控制：BMC 4，SSH 8。
- BMC 和 SSH 使用独立 worker pool；慢 BMC 页面不会阻塞 SSH 命令。
- 系统根据 CPU 和内存采样调整 worker 数，但不会超过配置上限。
- 在 --mode full 下，BMC 同 endpoint 复用一次登录会话；会话失效时会新建页面并尝试恢复性重新登录。
- 进程内 ResourceRegistry 为 endpoint 加锁，防止同一 Python 进程内多个 Scheduler/API 执行撞到同一 endpoint。

## 仓库结构

    .
    ├── run.py                         统一 Python / PyInstaller 入口
    ├── 启动.bat                        Windows 交互式启动入口
    ├── bmc_auto_capture/              命令行包入口
    ├── src/
    │   ├── app.py                     组合根：加载到结果输出的主流程
    │   ├── cli/                       参数解析、菜单、API Server 启动
    │   ├── loader/                    Excel 与任务定义加载、校验
    │   ├── models/                    设备、任务、计划、结果与 verdict 模型
    │   ├── connectivity/              TCP 预检与路由监控
    │   ├── scheduler/                 endpoint 调度、worker pool、会话组
    │   ├── executor/                  BMC、SSH、浏览器与重试
    │   ├── rules/                     条件、检查点和规则执行
    │   ├── out/                       结果、工件、时序、审计与 DOCX 输出
    │   ├── plan_catalog/              确定性计划与计划项标识
    │   ├── plan_run_service/          Plan-Run、状态、查询与回调
    │   └── executor_api_server/       FastAPI Executor API
    ├── config/                        默认运行配置
    ├── examples/task_template.xlsx    Excel 模板
    ├── tasks.json                     任务定义与 BMC 动作 DSL
    ├── templates/acceptance/          验收 DOCX 模板
    ├── scripts/                       构建、离线验证、API 冒烟工具
    ├── tests/                         自动化测试
    └── .github/workflows/release.yml  Windows 构建与 Release 流水线

## 使用的开源组件

项目要求 Python 3.11+。完整版本约束以 pyproject.toml 和 requirements.txt 为准。

| 组件 | 项目中的职责 |
| --- | --- |
| [Playwright for Python](https://playwright.dev/python/) | 自动化 Chromium：BMC 页面打开、登录、交互、元素等待、截图和页面状态采集。 |
| [Paramiko](https://docs.paramiko.org/) | SSHv2 连接、认证、命令执行和交互式 Channel。 |
| [openpyxl](https://openpyxl.readthedocs.io/) | 读取和校验 Excel 设备/任务配置。 |
| [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | Executor API、OpenAPI 与本地服务运行。 |
| [Pydantic](https://docs.pydantic.dev/) | API 请求、响应和领域数据模型校验。 |
| [psutil](https://psutil.readthedocs.io/) | CPU、内存采样，用于动态 worker 调整。 |
| [PyYAML](https://pyyaml.org/) | YAML 默认配置加载。 |
| [Textual](https://textual.textualize.io/) + [Rich](https://rich.readthedocs.io/) | 可选终端界面与可读控制台输出。 |
| [Pillow](https://pillow.readthedocs.io/) | 截图和终端证据图像处理。 |
| [python-docx](https://python-docx.readthedocs.io/) | 验收 DOCX 回填与导出。 |
| [aiofiles](https://github.com/Tinche/aiofiles) + [python-multipart](https://github.com/Kludex/python-multipart) | API 异步文件与 multipart Excel 上传支持。 |
| [PyInstaller](https://pyinstaller.org/) | Windows 发布包的执行引擎打包。 |

requirements.txt 还声明了 telnetlib3；当前主线没有找到其运行时调用，因此它不在上表的已实现协议能力之内。

### BMC 网页交互

BMC 不依赖某一家厂商的私有 SDK，而是由 Playwright 操作管理页面：

- BrowserManager 管理 Chromium、Browser Context 和页面生命周期。
- BMCExecutor 使用页面定位器完成 goto、click、fill、press、wait、wait_for_selector 和 intermediate_screenshot 等动作。
- tasks.json 中的 BMC_URL 和 BMC_ACTIONS 定义页面地址、动作序列与页面就绪条件。
- 对受控内网中的自签名 BMC 证书，浏览器会话配置了 HTTPS 错误忽略，并保留“高级 → 继续访问”的页面级兜底逻辑。
- 证书绕过只适用于受控管理网络；它不验证服务端证书身份，不应将 BMC 管理入口暴露到公网。

### SSH 命令执行

- SSHExecutor 使用 Paramiko 建立 SSH 连接，执行单命令或交互 shell。
- 对网络设备的分页、命令回显、长输出、空闲等待与超时做了协议层处理。
- 原始输出、解析后的结果和终端式证据会写入任务输出目录。

## 安装与运行

### Windows 发布包

1. 从 [GitHub Releases](https://github.com/minliny/BMC-Auto-Capture/releases) 下载对应 Windows x64 完整包及同名 SHA256 文件。
2. 解压到固定目录，例如 C:/bmc-auto-capture。
3. 目录应至少包含 runtime/、app/、run.py、启动.bat。
4. 如 Windows 阻止运行，在 PowerShell 中执行：

    Get-ChildItem "C:/bmc-auto-capture" -Recurse -File | Unblock-File

完整包内含执行引擎、Python 运行时和 Playwright Chromium；现场使用不需要额外安装 Python、pip 或浏览器。

双击 启动.bat 可使用交互式菜单。菜单提供顺序/并发运行、TCP 预检、IP:port 测试、Excel 选择、worker 配置、Executor API 与手动验收 DOCX 导出。对于批处理或受控自动化，推荐使用下方显式命令，便于复现和审计。

启动器的常规执行路径默认会加 --no-preflight；只有在交互提示中选择执行前预检时才保留 TCP 预检。对需要确定预检行为的现场流程，优先使用下方显式命令。

### 源码环境

macOS/Linux：

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python -m playwright install chromium

Windows PowerShell：

    py -3.11 -m venv .venv
    .venv/Scripts/Activate.ps1
    python -m pip install -r requirements.txt
    python -m playwright install chromium

源码根目录的 Excel 模板位于 examples/task_template.xlsx；不要将源码示例写成 app/examples/task_template.xlsx。

### 本地执行

顺序执行：

    python run.py --app-dir . --excel examples/task_template.xlsx --mode sequential

按 endpoint 动态并发：

    python run.py --app-dir . --excel examples/task_template.xlsx --mode full --max-bmc-workers 4 --max-ssh-workers 8

跳过执行前 TCP 预检：

    python run.py --app-dir . --excel examples/task_template.xlsx --mode full --no-preflight

BMC 快速证据模式，只保存 PNG 和 HTML：

    python run.py --app-dir . --excel examples/task_template.xlsx --mode full --bmc-artifact-profile fast

Windows 发布包对应命令：

    runtime/bmc-engine.exe --app-dir app --excel app/examples/task_template.xlsx --mode sequential
    runtime/bmc-engine.exe --app-dir app --excel app/examples/task_template.xlsx --mode full

### 常用 CLI 参数

| 参数 | 用途 |
| --- | --- |
| `--config <yaml>`、`--output <dir>` | 覆盖默认 YAML 配置和输出根目录。 |
| `--mode <mode>` | `sequential` 为逐项执行，`full` 为 endpoint 感知的动态调度。 |
| `--max-bmc-workers N`、`--max-ssh-workers N` | 分别覆盖两个协议 worker pool 的上限。 |
| `--concurrency N` | 兼容旧脚本；`N > 1` 且未显式指定 `--mode` 时会切到 `full`，并把未单独设置的两个 worker 上限都映射为 `N`。新调用应使用两个 worker 参数。 |
| `--ssh-command-timeout S`、`--ssh-idle-timeout S`、`--bmc-page-timeout S` | 覆盖 SSH 命令、SSH 空闲读取和 BMC 页面/选择器等待的超时。 |
| `--bmc-artifact-profile <profile>` | `full` 保存完整 BMC 证据；`fast` 只保存 PNG 和 HTML。 |
| `--no-preflight` | 跳过常规执行前的 TCP 连通性预检。 |
| `--verbose` | 打开 debug 级别日志。 |

服务端相关参数见 [Executor API](#executor-api)；验收导出相关参数见 [结果、证据与验收材料](#结果证据与验收材料)。以 `python run.py --help` 或发布包的 `runtime/bmc-engine.exe --help` 为最终可执行参数口径。

### 预检

仅检查 TCP 连通性：

    python run.py --app-dir . --excel examples/task_template.xlsx --preflight-only --preflight-target all

仅检查 SSH 凭证：

    python run.py --app-dir . --excel examples/task_template.xlsx --preflight-only --preflight-auth ssh

重要：在当前 v0.2.6 入口中，凭证检查必须同时传入 --preflight-only。单独传入 --preflight-auth 不会进入预检分支，可能继续执行任务。

当前 启动.bat 的“凭证检查”子菜单只传入 --preflight-auth，缺少 --preflight-only；不要把该菜单项当作 auth-only 操作，应使用上述显式 CLI 命令。

BMC 的 --preflight-auth 当前只发起未认证 HTTPS 请求，并以 HTTP 200/302 作为成功条件；它没有提交 Excel 中的 BMC 用户名和密码，且自签名证书可能导致该探测失败。因此它不能作为 BMC 凭证有效性的证明。BMC 用户名密码的真实验证发生在实际 Playwright 登录流程中。

预检命令输出的是终端摘要；当前 run.py/frozen engine 路径不应被用来承诺 auth_check_result.csv 或通过非零退出码作自动化放行判断。

## Excel 与任务配置

### Excel

模板包含两个工作表：

| 工作表 | 作用 |
| --- | --- |
| 设备信息 | 设备分组、名称、启用状态、带外/带内地址与登录信息。 |
| 任务列表 | 任务序号、任务名称、协议类型、匹配设备分组和启用状态。 |

设备信息常用列：

| 列 | 说明 |
| --- | --- |
| 设备分类 | 设备分组，例如 A3、L1、L2、RM211。 |
| 设备名称 | 输出目录、报表和任务定位使用的名称。 |
| 是否启用 | 是/否。 |
| 带外管理IP、用户名、密码 | BMC/iBMC 连接信息。 |
| 带内管理IP、用户名、密码 | SSH 连接信息。 |

任务列表中的任务名称需要匹配 tasks.json 中的任务定义；设备分组用于把任务限制到匹配设备。

### tasks.json

| 执行模式 | 说明 |
| --- | --- |
| BMC_URL | 打开目标 BMC 页面，检查页面状态后保存证据。 |
| BMC_ACTIONS | 执行页面动作序列后保存证据，例如 goto、click、fill、press、wait、wait_for_selector。 |
| SSH_CMD | SSH 登录并执行命令，保存文本输出和终端证据。 |
| TELNET_CMD | 数据模型可识别，但 v0.2.6 未验证原生 Telnet 执行；不要在生产任务中启用。 |

新增任务时：

1. 在 tasks.json 添加或修改任务定义。
2. 在 Excel 任务列表启用同名任务。
3. 为复杂 BMC 页面配置明确动作与就绪条件，避免页面未稳定就截图。
4. 先使用离线验证和小范围设备验证，再扩大到并发批量运行。

详情参见 [任务添加指南](docs/TASK_ADDING_GUIDE.md)。

## 结果、证据与验收材料

每次执行会创建带时间戳的输出目录：

    output/<timestamp>/
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
    └── <设备分组>/<设备名称>/<任务名称>/
        ├── PNG、TXT 和原始工件
        └── html/ 下的 HTML、evidence.html、best-effort MHTML、state.json 等

常用文件：

| 文件 | 用途 |
| --- | --- |
| final_result.csv | 可按状态筛选的最终任务结果。 |
| failure_detail.csv | 失败原因、异常信息和任务定位。 |
| summary_pivot.csv | 设备 × 任务汇总。 |
| plan_timing.csv、device_timing.csv、endpoint_timing.csv | 任务、设备和 endpoint 三个维度的耗时与排队分析。 |
| execution_summary.json | 整次运行摘要和并发效率信息。 |
| evidence_audit.csv | 证据缺失、回到登录页、会话冲突等审计结果。 |

从已有运行结果导出验收 DOCX、证据 ZIP 与回填报告：

    python run.py --app-dir . --acceptance-docx --acceptance-run-output output/<timestamp>

也可用 --acceptance-evidence-dirs 指定一个或多个既有证据目录。模板默认位于 templates/acceptance/。

## Executor API

### 安全边界

Executor API 默认没有应用层认证，并允许跨域请求。只应部署在受信任的内网、主机防火墙或上层网关保护之后；不要暴露到公网。

推荐先绑定回环地址：

    runtime/bmc-engine.exe --app-dir app --server --host 127.0.0.1 --port 18000 --runner fake

真实 BMC/SSH 执行需要两层显式授权：

1. 服务启动时传入 --runner real --enable-real-runner。
2. Plan-Run 请求体中传入 "runner": "real"。

仅设置服务端 real runner 不会让每个请求自动触达真实设备。

### 推荐的外部 Plan-Run 协议

    POST /executor/v1/config/excel
    POST /executor/v1/plans
    GET  /executor/v1/plans/{planId}?excelHash=<hash>
    GET  /executor/v1/plans/{planId}/items?excelHash=<hash>
    POST /executor/v1/plans/{planId}/callbacks:retry

上传 Excel：

    curl -X POST http://127.0.0.1:18000/executor/v1/config/excel -F "file=@config.xlsx"

启动外部计划。callback.planId 是外部系统的计划标识，excelHash 必须来自上传响应：

    curl -X POST http://127.0.0.1:18000/executor/v1/plans -H "Content-Type: application/json" -d '{"excelHash":"<excel-sha256>","callback":{"planId":"plan-20260625-001","itemStatusUrl":"http://callback.example/items","mode":"batch"},"runner":"fake","updater":"downstream-system"}'

查询计划与计划项：

    curl "http://127.0.0.1:18000/executor/v1/plans/plan-20260625-001?excelHash=<excel-sha256>"
    curl "http://127.0.0.1:18000/executor/v1/plans/plan-20260625-001/items?excelHash=<excel-sha256>"

API 会在 callback.itemStatusUrl 非空时自动使用 HTTP 回调；否则使用 fake transport。--callback-transport 是兼容参数，不应作为新 Plan-Run 集成的回调开关。

可在运行中的服务查看当前契约：

    GET http://127.0.0.1:18000/openapi.json
    GET http://127.0.0.1:18000/executor/v1/contracts

POST /executor/v1/config/excel:path 只适合执行机本地路径调试；远程调用应上传文件，不要将调用方文件系统路径传给执行机。

历史 direct-job、plans:import 和 runs 路由处于 deprecated/兼容状态，并从 OpenAPI 隐藏。新的外部集成应使用上述 Plan-Run 协议，而不是依赖这些历史接口。

更多字段、回调与状态语义见 [Plan-Run API](docs/PLAN_RUN_DISPATCH_API.md)。

## 安全与已知限制

- Excel 可包含 BMC/SSH 明文账号密码；请将输入文件、输出目录和备份纳入受控存储、最小权限和清理流程。
- 为兼容受控内网中的自签名 BMC，Chromium 以 `--ignore-certificate-errors` 启动，Browser Context 设置 `ignore_https_errors=True`；若仍落入 Chrome 证书警告页，执行器会尝试点击“高级/Advanced”与“继续/Proceed”。这是兼容路径，不是证书信任或身份校验。
- SSH 连接当前使用 Paramiko `AutoAddPolicy()` 接受未知主机密钥；若环境要求严格 host-key 校验或密钥固定，应在接入前补充相应策略。
- 日志和回调会对常见敏感字段脱敏，但截图、HTML、命令输出和状态工件仍可能包含设备或会话敏感信息；不要将整个 output/ 目录当作可公开数据。
- Executor API 当前未提供入站认证或 TLS 配置，且 CORS 允许跨域；默认仅绑定 `127.0.0.1`，需要远程访问时应由反向代理、TLS、认证、ACL 和网络隔离共同保护。
- Headless BMC 流程不能自动解决 CAPTCHA；BMC 页面、选择器和登录逻辑也需要在目标机型与固件版本上验证。
- 原生 TELNET、独立 BMC 凭证验证和跨进程 endpoint 互斥均不属于 v0.2.6 的交付承诺。

## 配置

默认配置文件为 config/default_config.yaml，常用项如下：

| 配置项 | 默认值 | 含义 |
| --- | ---: | --- |
| max_bmc_workers | 4 | BMC endpoint 最大并发数。 |
| max_ssh_workers | 8 | SSH endpoint 最大并发数。 |
| base_bmc_workers | 2 | BMC worker 基础数。 |
| base_ssh_workers | 4 | SSH worker 基础数。 |
| tcp_connect_timeout | 5 秒 | TCP 预检超时。 |
| ssh_command_timeout | 60 秒 | 单条 SSH 命令超时。 |
| ssh_idle_timeout | 5 秒 | SSH 输出空闲等待时间。 |
| bmc_page_timeout | 60 秒 | BMC 页面加载与选择器等待上限。 |
| bmc_artifact_profile | full | full 保存完整证据；fast 仅保存 PNG、HTML。 |
| preflight_enabled | true | 执行前是否做 TCP 预检。 |
| route_guard_enabled | true | 是否监控执行期间的系统路由变化。 |
| output_root | ./output | 输出根目录。 |

命令行参数可覆盖配置。使用以下命令查看当前实际支持的参数：

    python run.py --help
    runtime/bmc-engine.exe --help

## 验证与发布

离线开发验证：

    python scripts/dev_verify_all.py --offline --quick --output ./dev_verify_out
    python scripts/dev_verify_all.py --offline --output ./dev_verify_out

测试环境中可执行：

    python -m pip install pytest pytest-asyncio
    python -m pytest -q

上述验证不等同于真实 BMC/SSH 设备验证；真实设备、网络策略、账户权限、页面版本和证书策略仍需单独验收。

发布流程由 .github/workflows/release.yml 驱动：

- 推送 v* tag 触发 Windows x64 构建。
- 构建通过 PyInstaller 生成 bmc-engine，并打包 Playwright Chromium。
- 稳定版发布完整 Windows zip；RC 同时提供 runtime 与 app 分层构件。
- 构件附带 SHA256 文件；更新 runtime 与 app 时应遵循对应 Release Notes 的兼容要求。

## 常见问题

### BMC 页面截图失败或回到登录页

先确认 TCP 预检、账号权限、同账号会话冲突和页面超时。BMC 页面完成加载后仍可能异步渲染，因此为复杂页面配置动作与就绪条件，并保留 full 证据模式做复核。

### SSH 输出不完整

确认设备命令会返回可识别的提示符。对于分页或长输出，调大 ssh-command-timeout 与 ssh-idle-timeout，并按设备类型调整命令。

### 找不到 Chromium

源码模式先执行：

    python -m playwright install chromium

Windows 发布包应确认 runtime/playwright_browsers 目录存在；不要混用不同发布包的 runtime 与 app，除非 Release Notes 明确允许。

### 为什么没有把 BMC 的 HTTPS 预检当作登录验证

当前 BMC 预检不提交用户名和密码。它只能反映未经认证的 HTTPS 响应，不能证明账户可登录；请以实际 Playwright 登录结果为准。

## 相关文档

- [任务添加指南](docs/TASK_ADDING_GUIDE.md)
- [Plan-Run API](docs/PLAN_RUN_DISPATCH_API.md)
- [Plan Catalog 设计](docs/PLAN_CATALOG_DESIGN.md)
- [规则说明](docs/rules_guide.md)
