FINAL_OUTPUT_BEGIN

# API 设计审计与落地规划

---

## 1. 审计结论

**API_DESIGN.md 整体可用，双向调度设计正确，但存在 4 个 P0 问题和若干 P1/P2 问题需要在实现前修正。**

设计文档与现有代码之间存在较大的"模型鸿沟"——API 设计中的 Executor/Job/Run/Command/Artifact/ResourceLock 在现有代码中均无对应实现。这不是设计问题，而是 v0.1 需要新建的核心模块。

---

## 2. P0 问题（阻塞 v0.1，必须修正）

### P0-1：Device 模型缺少 lock_uri 推导逻辑

**现状**：`src/models/device.py` 的 `Device` 只有 `bmc_ip`、`inband_ip` 字段，没有 `resource_locks` 或 `lock_uri` 属性。

**问题**：API 设计第 7 条要求"不能继续用 device_name 作为唯一资源标识"，但现有 `DynamicScheduler._device_queues` 和 `WorkerPool._running_devices` 全部使用 `device.device_name` 作为 key。

**修正**：在 Device 模型上新增方法：

```
def lock_uris(self) -> dict[str, str]:
    """返回 {protocol_type: lock_uri} 映射"""
    result = {}
    if self.bmc_ip:
        result["BMC"] = f"bmc://{self.bmc_ip}"
    if self.inband_ip:
        # 根据 device_group 判断 SSH 子类型
        ...
        result["SSH_VRP"] = f"ssh-vrp://{self.inband_ip}"
    return result

def lock_uri_for_task(self, task_type: str) -> str:
    """根据任务类型返回对应的 lock_uri"""
    ...
```

同时将 `DynamicScheduler` 和 `WorkerPool` 中所有 `device_id`/`device_name` 改为 `lock_uri`。

### P0-2：API 模型层完全缺失

**现状**：`api/schemas.py` 只有 `ExecuteStartRequest`、`ExecutionStatus`、`UploadResponse` 等本地 API schema，没有 API_DESIGN 中定义的核心模型。

**问题**：没有 Executor、Job、Run、Command、Artifact、ResourceLock、ExecutionStats 的 Pydantic 或 dataclass 定义，所有 API 接口都无从落地。

**修正**：新建 `src/api_models/` 包，包含：

| 模块 | 内容 |
|---|---|
| `executor.py` | Executor dataclass + ExecutorStatus enum |
| `job.py` | Job dataclass + JobStatus enum |
| `run.py` | Run dataclass + RunStatus enum |
| `command.py` | Command dataclass + CommandType enum + CommandStatus enum |
| `artifact.py` | Artifact dataclass + ArtifactType enum + ArtifactStatus enum |
| `event.py` | Event dataclass + EventType enum |
| `resource_lock.py` | ResourceLock dataclass + LockType enum |
| `stats.py` | ExecutionStats + PerDeviceSummary + PerTaskSummary + PerExecutorSummary |
| `error_info.py` | ErrorInfo dataclass |
| `step_result.py` | StepResult（与现有 ExecutionResult.StepResult 对齐） |

### P0-3：Job 模型中 `command` 字段命名冲突

**问题**：API_DESIGN 中 Job 对象的 `"command": { "...": "TaskDefinition 完整内容" }` 字段名与 Command 协议中的 `command_id`/`command_type` 极易混淆。JSON 中 `job.command` 是 TaskDefinition 快照，而 `command.command_type` 是协议指令类型。

**修正**：将 Job 中的 `command` 字段改名为 `task_snapshot`，明确语义：

```json
{
  "job_id": "job-004",
  "task_snapshot": { "...": "TaskDefinition 完整内容" },
  "device_snapshot": { "...": "设备快照" }
}
```

### P0-4：心跳响应中 pending_commands 结构不完整

**问题**：API_DESIGN 4.2 心跳响应中 `pending_commands` 只有 `command_id`、`command_type`、`job_id`、`created_at`，缺少 `expires_at` 字段。执行端无法判断命令是否已过期。

**修正**：补全字段：

```json
{
  "pending_commands": [
    {
      "command_id": "cmd-xyz",
      "command_type": "CANCEL_JOB",
      "job_id": "job-003",
      "run_id": "run-xxx",
      "created_at": "2026-06-08T10:04:00Z",
      "expires_at": "2026-06-08T10:09:00Z"
    }
  ]
}
```

---

## 3. P1 问题（重要，但可在 v0.1 中简化处理）

### P1-1：SSH 类型区分依赖 device_group 硬编码

**现状**：`SSHExecutor._get_ssh_strategy()` 通过 `device.device_group in {"L1", "L2"}` 判断 interactive_shell。

**问题**：lock_uri 的类型推导需要更可靠的方式。不能仅靠 device_group 名称猜测。

**建议**：Device 模型增加显式 `ssh_type` 字段：`"SSH"` | `"SSH_VRP"` | `"SSH_LINUX"`。Excel/YAML 输入层增加此列，lock_uri 直接从此字段推导。

### P1-2：ExecutionResult 状态值与 Job 状态机不匹配

**现状**：`ExecutionResult.execution_status` 值为 `EXEC_SUCCESS`、`EXEC_FAILED`、`EXEC_SUCCESS_RULE_FAILED`、`EXEC_PARTIAL`、`EXEC_TIMEOUT`、`EXEC_ERROR`、`EXEC_SKIPPED_*`。

**问题**：与 API Job 状态机（SUCCEEDED/FAILED/TIMEOUT/CANCELED/SKIPPED）不一一对应。

**建议**：v0.1 在 `job_runner_adapter` 中做映射层，无需修改现有 executor：

| ExecutionResult.execution_status | Job status |
|---|---|
| EXEC_SUCCESS | SUCCEEDED |
| EXEC_FAILED | FAILED |
| EXEC_SUCCESS_RULE_FAILED | FAILED |
| EXEC_PARTIAL | FAILED |
| EXEC_TIMEOUT | TIMEOUT |
| EXEC_ERROR | FAILED |
| EXEC_SKIPPED_PORT_BLOCKED | SKIPPED |
| EXEC_SKIPPED_PRECHECK_FAILED | SKIPPED |

### P1-3：密码以明文存在 Device 和 executor 中

**现状**：`Device` 直接存 `bmc_password`、`inband_password` 明文，`BMCExecutor._bmc_login()` 直接 `await password_el.fill(device.bmc_password)`。

**问题**：API 设计的 `password_ref` / `secret_ref` 机制与现有执行器不兼容。

**建议**：v0.1 不做真实 secret 解析——Device 仍存明文密码，但在 API 传输层（Job 的 device_snapshot）中替换为 `password_ref`。执行端收到后从本地 Device 缓存中还原。v0.2 再引入真实 secret manager。

### P1-4：缺少 Executor 本地 Device 注册表

**现状**：设备信息从 Excel 文件加载，一次性使用。没有持久化的"本执行端能访问哪些设备"的注册表。

**问题**：API 需要执行端知道自己能访问哪些 `lock_uri`（用于 register 上报、jobs:poll 过滤）。

**建议**：v0.1 执行端启动时从 Excel/YAML 加载 Device 列表到内存注册表 `DeviceRegistry`，用于：
- register 时上报可访问的 lock_uri 列表
- jobs:poll 时服务端按 lock_uri 匹配下发
- 本地 lock_uri → Device 查找（获取密码连接设备）

### P1-5：Run 生命周期管理缺失

**现状**：当前 `_executions` 字典直接管理 execution 生命周期（starting → running → complete/error）。

**问题**：没有 Run 对象、没有 Run 状态机、没有 Run → Job 的关联。

**建议**：v0.1 在服务端实现 Run 管理（创建 Run → 展开 Job → 调度 → 聚合结果）。执行端侧只需理解 Job，不需要管理 Run 生命周期。

---

## 4. P2 问题（优化项，可延后）

### P2-1：ASSIGN_RUN 批量下发可延后

v0.1 只做 `ASSIGN_JOB` 单 Job 下发。`ASSIGN_RUN` 在服务端做 Job 展开后逐个下发。

### P2-2：PAUSE_RUN / RESUME_RUN 可延后

当前 `DynamicScheduler` 已有 `pause()`/`resume()` 方法。v0.1 先支持 CANCEL_JOB/CANCEL_RUN。PAUSE/RESUME 在 v0.2 接入。

### P2-3：大文件预签名上传可延后

v0.1 只做小文件 multipart 上传（PNG/TXT/JSON 通常 < 10MB）。CSV/ZIP 大文件在 v0.2 支持 `prepare-upload` + `complete` 流程。

### P2-4：DRAINING 状态可延后

Executor 状态机的 `DRAINING` 状态（优雅下线）在 v0.2 实现。v0.1 只做 ONLINE/BUSY/UNRESPONSIVE/OFFLINE。

### P2-5：EXECUTOR_STATUS_CHANGED / COMMAND_ACK / COMMAND_REJECTED 事件可延后

v0.1 只发 `JOB_STATUS_CHANGED` 和 `ARTIFACT_UPLOADED` 事件。

---

## 5. v0.1 最小闭环接口（修正后）

### Server API（执行端 → 服务端）6 个接口

| 接口 | 用途 | 优先级 |
|---|---|---|
| `POST /api/v1/executors/register` | 注册 + 上报 lock_uri 列表 + 获取 token | P0 |
| `POST /api/v1/executors/{id}/heartbeat` | 心跳 + 拉取 pending_commands（含 expires_at） | P0 |
| `POST /api/v1/executors/{id}/jobs:poll` | 主动拉取 Job（含 task_snapshot + device_snapshot） | P0 |
| `POST /api/v1/jobs/{job_id}:accept` | 确认接受，触发资源锁 | P0 |
| `POST /api/v1/jobs/{job_id}:finish` | 上报最终结果 + step_results + artifact 摘要 | P0 |
| `POST /api/v1/jobs/{job_id}/artifacts` | 小文件 multipart 上传 | P0 |

### Executor API（服务端 → 执行端）4 个接口

| 接口 | 用途 | 优先级 |
|---|---|---|
| `POST /executor/v1/commands` | Push 模式核心：下发 ASSIGN_JOB / CANCEL_JOB 等 Command | P0 |
| `GET /executor/v1/status` | 查询执行端状态 + 当前 active_jobs | P0 |
| `POST /executor/v1/jobs/{job_id}:cancel` | 取消正在运行的 Job | P0 |
| `POST /executor/v1/ping` | 探活 | P1 |

### v0.2 延后接口

`jobs:progress`、`jobs:events`、`artifacts:prepare-upload`、`artifacts:complete`、`POST /api/v1/runs`、`runs:start`、`GET runs/{id}`、`runs:cancel`、`runs:pause`、`runs:resume`、`GET /executor/v1/jobs/{id}`、`POST /executor/v1/runs/{id}:pause`、`POST /executor/v1/runs/{id}:resume`、`POST /executor/v1/jobs/{id}/artifacts:request`、`POST /executor/v1/config:update`

---

## 6. 当前代码模块改造点

### 6.1 新建模块（全部在 `bmc-auto-capture/` 下）

```
src/
├── api_models/                  # 新建 — 统一 API 数据模型
│   ├── __init__.py
│   ├── executor.py              # Executor, ExecutorStatus
│   ├── job.py                   # Job, JobStatus
│   ├── run.py                   # Run, RunStatus
│   ├── command.py               # Command, CommandType, CommandStatus
│   ├── artifact.py              # Artifact, ArtifactType, ArtifactStatus
│   ├── event.py                 # Event, EventType
│   ├── resource_lock.py         # ResourceLock, LockType
│   ├── stats.py                 # ExecutionStats
│   └── error_info.py            # ErrorInfo
│
├── server_api_client/           # 新建 — 执行端调用服务端
│   ├── __init__.py
│   ├── client.py                # HTTP client with retry/auth/idempotency
│   ├── register.py              # register()
│   ├── heartbeat.py             # heartbeat()
│   ├── job_poller.py            # poll_jobs()
│   ├── job_reporter.py          # accept_job() / finish_job()
│   └── artifact_uploader.py     # upload_artifact()
│
├── executor_api_server/         # 新建 — 执行端本地 HTTP 服务（Executor API）
│   ├── __init__.py
│   ├── server.py                # FastAPI/uvicorn app, auth middleware
│   ├── routes.py                # /status, /commands, /jobs/:cancel, /ping
│   └── schemas.py               # Request/response Pydantic models
│
├── command_handler/             # 新建 — 统一 Command 分发
│   ├── __init__.py
│   └── dispatcher.py            # 接收 Command → 路由到对应 handler
│
├── job_runner_adapter/          # 新建 — API Job → 现有执行器的桥接
│   ├── __init__.py
│   └── adapter.py               # Job → TaskPlan, 结果 → finish payload
│
├── resource_lock_manager/       # 新建 — 执行端本地资源锁
│   ├── __init__.py
│   └── lock_manager.py          # acquire/release lock_uri, 防重检查
│
├── stats_collector/             # 新建 — 执行统计收集
│   ├── __init__.py
│   └── collector.py             # 记录 timing, 聚合统计
│
└── device_registry/             # 新建 — 执行端本地 Device 注册表
    ├── __init__.py
    └── registry.py              # 从 YAML 加载, lock_uri → Device 查找
```

### 6.2 修改现有模块

| 模块 | 改动内容 | 影响范围 |
|---|---|---|
| `src/models/device.py` | 新增 `lock_uris()`、`lock_uri_for_task()` 方法；新增 `ssh_type` 字段 | 低 — 新增方法，不改现有字段 |
| `src/models/task_plan.py` | `retry_attempt` 重命名为 `attempt`；`status` 增加枚举值 | 中 — 字段重命名需全局替换 |
| `src/models/execution_result.py` | 不变（通过 adapter 映射） | 无 |
| `src/scheduler/dynamic_scheduler.py` | `_device_queues` key 从 device_name 改为 lock_uri；accept 取消确认后等待 accept | 高 — 核心调度逻辑变更 |
| `src/scheduler/worker_pool.py` | `_running_devices` 从 `set[str]` of device_name 改为 `set[str]` of lock_uri | 中 |
| `src/models/app_config.py` | 新增 `server_url`、`executor_id`、`executor_token`、`server_token`、`heartbeat_interval`、`api_poll_interval` | 低 |
| `api/server.py` | 不变（保留现有本地 API 供 TUI 使用） | 无 |

### 6.3 配置扩展（config.yaml 新增项）

```yaml
# --- Server connection ---
server:
  url: "https://server.example.com"
  executor_id: "exec-01"           # 留空则自动生成
  executor_token: ""               # 注册后由服务端下发
  server_token: ""                 # 服务端调用执行端的 token
  tls_cert_path: ""
  tls_key_path: ""

# --- Executor API server ---
executor_api:
  enabled: true
  host: "0.0.0.0"
  port: 8443
  tls_enabled: false               # v0.2 启用

# --- Heartbeat / Poll ---
heartbeat_interval_seconds: 30
api_poll_interval_seconds: 10
api_poll_timeout_seconds: 30

# --- Resource locks ---
device_registry_path: "./devices.yaml"  # 替代 Excel 作为设备注册表

# --- Artifact ---
artifact_upload_enabled: true
artifact_max_multipart_bytes: 10485760  # 10MB
```

---

## 7. 实施顺序

### 阶段 1：模型层（1-2 天）

1. 新建 `src/api_models/`，实现所有 9 个数据模型（dataclass + enum）。
2. 修改 `src/models/device.py`，新增 `lock_uris()`、`lock_uri_for_task()`、`ssh_type`。
3. 修改 `src/models/task_plan.py`，`retry_attempt` → `attempt`，扩展 status 枚举。
4. 实现 `src/api_models/` 与现有 `src/models/` 的双向转换函数。

### 阶段 2：通信层（2-3 天）

5. 实现 `src/server_api_client/` — HTTP client + register/heartbeat/poll/accept/finish/upload。
6. 实现 `src/executor_api_server/` — FastAPI server + auth + routes。
7. 实现 `src/command_handler/` — Command 去重 + 分发 + 过期检查。

### 阶段 3：调度改造（2-3 天）

8. 实现 `src/device_registry/` — YAML 加载 + lock_uri 索引。
9. 实现 `src/resource_lock_manager/` — 本地 lock_uri acquire/release。
10. 修改 `src/scheduler/dynamic_scheduler.py` — key 改为 lock_uri，集成 lock_manager。
11. 修改 `src/scheduler/worker_pool.py` — `_running_devices` 改为 lock_uri。

### 阶段 4：桥接与集成（2-3 天）

12. 实现 `src/job_runner_adapter/` — API Job → TaskPlan → executor → finish payload。
13. 实现 `src/stats_collector/` — timing 记录 + 聚合。
14. 实现 `src/artifact_uploader/` — 小文件 multipart 上传 + checksum。

### 阶段 5：端到端联调（2-3 天）

15. 本地 mock server 联调（register → heartbeat → poll → accept → execute → finish → upload）。
16. Push 模式联调（server → executor commands → accept → execute → finish）。
17. 异常场景测试（心跳超时、重复下发、cancel、executor 掉线恢复）。

**总计预估：10-14 天（单人全职）**

---

## 8. 不建议现在做的内容

| 内容 | 原因 | 建议版本 |
|---|---|---|
| ASSIGN_RUN 批量下发 | v0.1 服务端逐个展开 Job 即可 | v0.2 |
| 大文件预签名上传 | v0.1 产物都是小文件（PNG/TXT/JSON < 10MB） | v0.2 |
| PAUSE_RUN / RESUME_RUN | 先做 CANCEL，暂停/恢复复杂度高 | v0.2 |
| 分片上传 | v0.1 不需要 | v0.3 (plan) |
| mTLS 双向证书 | v0.1 用 Bearer Token + HTTPS 即可 | v0.3 (plan) |
| HMAC 请求签名 | v0.1 用 TLS + Token 足够 | v0.2 |
| secret_ref 真实解析 | v0.1 密码在 Device 缓存中明文，API 传输时替换为 ref 占位 | v0.2 |
| Redis 资源锁 | v0.1 服务端用内存锁 + DB 持久化 | v0.2 |
| 执行端配置热更新 | 需要改动 AppConfig 运行时行为 | v0.2 |
| DRAINING 状态 | 优雅下线逻辑复杂 | v0.2 |
| 批量事件上报 | v0.1 单事件随 finish 上报 | v0.2 |
| per_device/per_task/per_executor 聚合统计 | v0.1 先收集 raw timing，聚合在 v0.2 | v0.2 |

---

## 9. 对 API_DESIGN.md 的具体修正清单

以下修改建议需要在 API_DESIGN.md 中应用：

1. **Job 模型**：`"command"` → `"task_snapshot"`
2. **心跳响应 pending_commands**：补齐 `expires_at`、`run_id` 字段
3. **jobs:poll 的 resource_whitelist**：改为由 register 时上报（作为 executor 的 known_lock_uris），而不是每次 poll 时传
4. **Device 模型**：新增 `ssh_type` 字段（`"SSH"` / `"SSH_VRP"` / `"SSH_LINUX"`），用于 lock_uri 推导
5. **v0.1 最小范围**：按本文第 5 节修正为 6+4 个接口

---

FINAL_OUTPUT_END
