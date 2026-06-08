FINAL_OUTPUT_BEGIN

# BMC Auto-Capture API 接口设计文档 v0.1

---

## 1. 总体结论

采用**双向调度能力对等设计**。不二选一。

- 服务端可通过 Executor API 主动向执行端下发 Command（ASSIGN_JOB、CANCEL_JOB、PAUSE_RUN 等）。
- 执行端可通过 Server API 主动向服务端注册、心跳、拉取任务、上报状态、上传产物。
- 两种通信路径共用同一套核心模型、同一套状态机、同一套 Command 协议。
- 服务端作为调度中枢维护全局资源锁和任务队列；执行端作为执行单元维护本地 worker 池和防重锁。

---

## 2. 核心模型

### 2.1 Executor（执行端）

```json
{
  "executor_id": "exec-01",
  "hostname": "WIN-PC-001",
  "ip": "10.0.1.100",
  "os": "Windows Server 2022",
  "version": "0.3.0",
  "status": "ONLINE",
  "capabilities": {
    "max_bmc_workers": 4,
    "max_ssh_workers": 8,
    "bmc_worker_slots_free": 2,
    "ssh_worker_slots_free": 5,
    "supported_protocols": ["BMC", "SSH", "SSH_VRP", "SSH_LINUX"]
  },
  "registered_at": "2026-06-08T10:00:00Z",
  "last_heartbeat_at": "2026-06-08T10:05:00Z",
  "tags": ["lab-a", "rack-3"]
}
```

字段说明：
- `executor_id`：执行端唯一标识，注册时由服务端分配或执行端自报。
- `capabilities`：执行端容量声明，服务端调度时必须参考 `*_worker_slots_free`。
- `status`：见 Executor 状态机。

### 2.2 Device（设备）

```json
{
  "device_id": "dev-001",
  "device_name": "Switch-A",
  "device_group": "core-switches",
  "ssh_type": "SSH_VRP",
  "resource_locks": [
    {
      "lock_uri": "bmc://10.0.0.1",
      "lock_type": "BMC",
      "lock_scope": "oob",
      "lock_exclusive": true,
      "oob_ip": "10.0.0.1",
      "oob_port": 443
    },
    {
      "lock_uri": "ssh-vrp://10.0.1.1",
      "lock_type": "SSH_VRP",
      "lock_scope": "inband",
      "lock_exclusive": true,
      "inband_ip": "10.0.1.1",
      "inband_port": 22
    }
  ],
  "credentials": {
    "bmc_username": "admin",
    "bmc_password_ref": "secret://bmc/switch-a",
    "ssh_username": "netadmin",
    "ssh_password_ref": "secret://ssh/switch-a"
  },
  "enabled": true,
  "tags": ["production", "rack-3"]
}
```

字段说明：
- `ssh_type`：显式 SSH 子类型，枚举 `SSH`（通用）、`SSH_LINUX`（Linux）、`SSH_VRP`（VRP 交互式 CLI），用于 lock_uri 推导。
- `resource_locks`：一个设备可有多个资源锁（BMC + SSH），`lock_type` 不同则可并行，相同则互斥（`lock_exclusive: true`）。
- `*_password_ref`：不存明文密码，存 secret 引用，由安全模块解析。
- `lock_uri`：全局唯一的资源标识，调度锁以此为准。严禁使用 `device_name` 作为资源锁 key。

**lock_uri 推导规则（强制）**：

| 任务类型 | lock_uri 格式 | lock_type |
|---------|-------------|-----------|
| BMC_URL / BMC_ACTIONS | `bmc://{oob_ip}` | BMC |
| SSH_CMD（通用 SSH） | `ssh://{inband_ip}` | SSH |
| SSH_CMD（Linux） | `ssh-linux://{inband_ip}` | SSH_LINUX |
| SSH_CMD（VRP 交互式） | `ssh-vrp://{inband_ip}` | SSH_VRP |

推导依据：任务 `execution_mode` 决定 lock_uri 前缀，Device 的 `oob_ip` / `inband_ip` + `ssh_type` 决定具体值。

**严禁行为**：
- 禁止使用 `device_name` 作为资源锁的唯一标识。
- 禁止在调度器、WorkerPool、锁管理器中用 `device_name` 做并发控制 key。
- 所有并发控制 MUST 使用 `lock_uri`。

### 2.3 TaskDefinition（任务定义）

```json
{
  "task_id": "task-login-check",
  "task_name": "BMC 登录检查",
  "task_type": "BMC",
  "execution_mode": "BMC_URL",
  "match_group": "switches",
  "command_or_url": "https://{bmc_ip}/login",
  "actions_json": "",
  "rules": [
    {
      "rule_name": "login_page_visible",
      "rule_type": "advanced",
      "enabled": true,
      "checks": [
        { "type": "text_exists", "target": "", "expect": "登录" },
        { "type": "element_exists", "target": "#username", "expect": "" }
      ]
    }
  ],
  "output_dir_template": "{device_name}/{task_name}",
  "image_name_template": "{device_name}_{task_name}_{step}_{timestamp}",
  "timeout_seconds": 60,
  "retry_count": 2,
  "full_screenshot": false,
  "screenshot_mode": "auto"
}
```

### 2.4 Run（一次执行运行）

```json
{
  "run_id": "run-20260608-001",
  "run_name": "周度全量巡检",
  "status": "RUNNING",
  "devices": ["dev-001", "dev-002"],
  "tasks": ["task-login-check", "task-ssh-version"],
  "job_ids": ["job-001", "job-002", "job-003"],
  "created_by": "admin",
  "created_at": "2026-06-08T10:00:00Z",
  "started_at": "2026-06-08T10:00:05Z",
  "finished_at": null,
  "pause_requested": false,
  "cancel_requested": false,
  "stats": { "...": "见 ExecutionStats" },
  "tags": ["weekly", "production"]
}
```

### 2.5 Job（一次设备 × 任务的执行单元）

一个 Job = 一个 Device × 一个 TaskDefinition × 一次 attempt。

```json
{
  "job_id": "job-001",
  "run_id": "run-20260608-001",
  "device_id": "dev-001",
  "task_id": "task-login-check",
  "attempt": 1,
  "max_attempts": 3,
  "status": "RUNNING",
  "resource_lock_uri": "bmc://10.0.0.1",
  "executor_id": "exec-01",
  "task_snapshot": { "...": "内嵌 TaskDefinition 完整内容，避免执行端再次查询" },
  "device_snapshot": { "...": "内嵌 Device 快照，含 credential ref 和 resource_locks" },
  "created_at": "2026-06-08T10:00:05Z",
  "queued_at": "2026-06-08T10:00:05Z",
  "dispatched_at": "2026-06-08T10:00:10Z",
  "accepted_at": "2026-06-08T10:00:12Z",
  "started_at": "2026-06-08T10:00:13Z",
  "finished_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "queue_wait_ms": 5000,
  "dispatch_latency_ms": 2000,
  "step_results": [ "...": "见 StepResult" ],
  "error": null,
  "retry_reason": null
}
```

### 2.6 Command（统一控制协议）

```json
{
  "command_id": "cmd-abc123",
  "command_type": "ASSIGN_JOB",
  "run_id": "run-20260608-001",
  "job": { "...": "Job 完整对象" },
  "created_at": "2026-06-08T10:00:10Z",
  "expires_at": "2026-06-08T10:05:10Z",
  "signature": "hmac-sha256=..."
}
```

`command_type` 枚举：
- `ASSIGN_JOB` — 下发单个 Job
- `ASSIGN_RUN` — 下发整个 Run（含批量 Job）
- `CANCEL_JOB` — 取消单个 Job
- `CANCEL_RUN` — 取消整个 Run
- `PAUSE_RUN` — 暂停 Run
- `RESUME_RUN` — 恢复 Run
- `UPDATE_CONFIG` — 更新执行端配置
- `REQUEST_ARTIFACT` — 请求补传产物
- `PING` — 心跳应答/健康检查

### 2.7 Event（事件）

```json
{
  "event_id": "evt-001",
  "event_type": "JOB_STATUS_CHANGED",
  "run_id": "run-20260608-001",
  "job_id": "job-001",
  "executor_id": "exec-01",
  "from_status": "RUNNING",
  "to_status": "SUCCEEDED",
  "payload": {},
  "created_at": "2026-06-08T10:02:30Z"
}
```

`event_type` 枚举：
- `JOB_STATUS_CHANGED`
- `RUN_STATUS_CHANGED`
- `EXECUTOR_STATUS_CHANGED`
- `ARTIFACT_UPLOADED`
- `COMMAND_ACK`
- `COMMAND_REJECTED`
- `HEARTBEAT`
- `ERROR`

### 2.8 Artifact（产物）

```json
{
  "artifact_id": "art-001",
  "job_id": "job-001",
  "run_id": "run-20260608-001",
  "artifact_type": "PNG_SCREENSHOT",
  "relative_path": "Switch-A/BMC_登录检查/step_01_login.png",
  "filename_template": "{device_name}_{task_name}_{step}_{timestamp}.png",
  "checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "size_bytes": 245760,
  "content_type": "image/png",
  "status": "UPLOADED",
  "upload_url": null,
  "upload_expires_at": null,
  "created_at": "2026-06-08T10:02:30Z",
  "uploaded_at": "2026-06-08T10:02:35Z"
}
```

`artifact_type` 枚举：
- `PNG_SCREENSHOT`
- `HTML_PAGE`
- `TXT_SSH_OUTPUT`
- `JSON_STRUCTURED`
- `CSV_SUMMARY`
- `LOG`
- `ZIP_BUNDLE`

### 2.9 StepResult（步骤结果）

```json
{
  "step_index": 0,
  "step_name": "打开 BMC 登录页",
  "status": "SUCCEEDED",
  "step_type": "BMC_NAVIGATE",
  "screenshot_artifact_id": "art-001",
  "duration_ms": 3500,
  "details": "页面加载成功，状态码 200",
  "variable_extracted": ""
}
```

### 2.10 ErrorInfo（错误信息）

```json
{
  "code": "BMC_TIMEOUT",
  "message": "BMC 页面加载超时: https://10.0.0.1/login",
  "retryable": true,
  "category": "BMC",
  "details": {
    "url": "https://10.0.0.1/login",
    "timeout_seconds": 60,
    "elapsed_seconds": 60.5
  }
}
```

### 2.11 ResourceLock（资源锁）

```json
{
  "lock_uri": "bmc://10.0.0.1",
  "type": "BMC",
  "holder_job_id": "job-001",
  "holder_executor_id": "exec-01",
  "acquired_at": "2026-06-08T10:00:12Z",
  "expires_at": "2026-06-08T10:05:12Z",
  "exclusive": true
}
```

资源锁类型与 lock_uri 格式：
| 类型 | lock_uri 格式 | 说明 |
|------|-------------|------|
| BMC | `bmc://{oob_ip}` | 浏览器 BMC 访问 |
| SSH | `ssh://{inband_ip}` | 通用 SSH |
| SSH_VRP | `ssh-vrp://{inband_ip}` | VRP 交互式 CLI |
| SSH_LINUX | `ssh-linux://{inband_ip}` | Linux SSH |

### 2.12 ExecutionStats（执行统计）

```json
{
  "run_total_duration_ms": 360000,
  "total_jobs": 100,
  "success_count": 85,
  "failed_count": 8,
  "timeout_count": 3,
  "canceled_count": 2,
  "skipped_count": 2,
  "retry_count": 5,
  "per_device_summary": [
    {
      "device_id": "dev-001",
      "device_name": "Switch-A",
      "job_count": 5,
      "success_count": 4,
      "failed_count": 1,
      "total_duration_ms": 45000,
      "avg_job_duration_ms": 9000
    }
  ],
  "per_task_summary": [
    {
      "task_id": "task-login-check",
      "task_name": "BMC 登录检查",
      "job_count": 50,
      "success_count": 48,
      "failed_count": 2,
      "avg_duration_ms": 12000,
      "p50_duration_ms": 11000,
      "p99_duration_ms": 35000
    }
  ],
  "per_executor_summary": [
    {
      "executor_id": "exec-01",
      "job_count": 50,
      "success_count": 42,
      "failed_count": 5,
      "timeout_count": 2,
      "canceled_count": 1,
      "total_duration_ms": 180000,
      "avg_queue_wait_ms": 3000
    }
  ]
}
```

---

## 3. 状态机

### 3.1 Run 状态机

```
CREATED → QUEUED → DISPATCHING → RUNNING → SUCCEEDED
                  ↘ DISPATCHING → RUNNING → FAILED
                  ↘ DISPATCHING → RUNNING → TIMEOUT
  CREATED → CANCELED
  QUEUED  → CANCELED
  RUNNING → PAUSED → RUNNING (恢复)
  RUNNING → CANCELED
  RUNNING → LOST (所有 executor 失联)
```

- `CREATED`：Run 已创建，尚未入队。
- `QUEUED`：Run 已入调度队列，等待资源。
- `DISPATCHING`：正在向 executor 下发 Job。
- `RUNNING`：至少一个 Job 正在执行。
- `SUCCEEDED`：所有 Job 终态为 SUCCEEDED 或 SKIPPED。
- `FAILED`：至少一个 Job 终态为 FAILED 且不可重试。
- `TIMEOUT`：Run 整体超时。
- `CANCELED`：人工或系统取消。
- `PAUSED`：Run 暂停，不调度新 Job，已在跑的 Job 继续。
- `LOST`：所有持有该 Run Job 的 executor 失联超时。

### 3.2 Job 状态机

```
CREATED → QUEUED → DISPATCHED → ACCEPTED → RUNNING → SUCCEEDED
                                                  → FAILED
                                                  → TIMEOUT
  QUEUED    → CANCELED
  DISPATCHED → CANCELED
  ACCEPTED  → CANCELED
  RUNNING   → CANCELED
  FAILED    → RETRY_PENDING → QUEUED (attempt+1)
  TIMEOUT   → RETRY_PENDING → QUEUED (attempt+1)
  DISPATCHED → LOST (executor 失联)
```

- `CREATED`：Job 已创建（作为 Run 的一部分）。
- `QUEUED`：Job 在服务端调度队列中，等待分配到 executor。
- `DISPATCHED`：Job 已发送到 executor，等待 executor ACK。
- `ACCEPTED`：Executor 已确认接受，等待开始执行。
- `RUNNING`：Executor 正在执行。
- `SUCCEEDED`：执行成功。
- `FAILED`：执行失败。
- `TIMEOUT`：Job 超时（executor 未在期限内上报结果）。
- `CANCELED`：被取消。
- `LOST`：Executor 失联，Job 状态不可知。
- `RETRY_PENDING`：等待重试（attempt < max_attempts）。
- `SKIPPED`：预检跳过（如端口不通、路由不可达）。

### 3.3 Executor 状态机

```
REGISTERING → ONLINE → BUSY → ONLINE
                       ONLINE → DRAINING → OFFLINE
  ONLINE → UNRESPONSIVE → OFFLINE
  ONLINE → OFFLINE (主动下线)
  UNRESPONSIVE → ONLINE (心跳恢复)
```

- `REGISTERING`：执行端首次注册中。
- `ONLINE`：在线，空闲。
- `BUSY`：在线，所有 worker slot 已满。
- `DRAINING`：排空中（不再接受新 Job，等待现有 Job 完成）。
- `UNRESPONSIVE`：心跳超时未响应。
- `OFFLINE`：离线。

### 3.4 Command 状态机

```
PENDING → DELIVERED → ACKED → PROCESSED
  PENDING → EXPIRED
  DELIVERED → REJECTED
  DELIVERED → EXPIRED
```

- `PENDING`：Command 已创建，等待投递。
- `DELIVERED`：已送达 executor。
- `ACKED`：Executor 已确认收到。
- `PROCESSED`：Executor 已处理完成。
- `REJECTED`：Executor 拒绝（如版本不兼容、资源不足）。
- `EXPIRED`：Command 超时未被处理。

### 3.5 Artifact 状态机

```
PENDING → UPLOADING → UPLOADED
  PENDING → UPLOADING → UPLOAD_FAILED → PENDING (重试)
  UPLOADED → DELETED
```

- `PENDING`：产物已记录，等待上传。
- `UPLOADING`：上传中。
- `UPLOADED`：上传完成。
- `UPLOAD_FAILED`：上传失败。
- `DELETED`：已删除。

---

## 4. Server API：执行端主动调用服务端

### 通用约定

- Base URL：`https://{server_host}/api/v1`
- 鉴权：Header `Authorization: Bearer {executor_token}`
- 幂等 key：Header `X-Idempotency-Key: {uuid}`
- Content-Type：`application/json`

### 4.1 POST /api/v1/executors/register

用途：执行端首次注册或重启后重新注册。

请求：
```json
{
  "executor_id": "exec-01",
  "hostname": "WIN-PC-001",
  "ip": "10.0.1.100",
  "os": "Windows Server 2022",
  "version": "0.3.0",
  "capabilities": {
    "max_bmc_workers": 4,
    "max_ssh_workers": 8,
    "supported_protocols": ["BMC", "SSH", "SSH_VRP", "SSH_LINUX"]
  },
  "tags": ["lab-a"]
}
```

响应：
```json
{
  "executor_id": "exec-01",
  "status": "ONLINE",
  "server_time": "2026-06-08T10:00:00Z",
  "heartbeat_interval_seconds": 30,
  "config": {
    "api_poll_interval_seconds": 10,
    "artifact_chunk_size_bytes": 10485760,
    "max_retry_attempts": 3
  }
}
```

幂等要求：相同 `executor_id` 重复注册视为 re-register，更新 ip/version/capabilities，返回 200。
关键错误码：`EXECUTOR_ID_INVALID`(400)、`EXECUTOR_VERSION_TOO_OLD`(400)、`AUTH_TOKEN_INVALID`(401)。

### 4.2 POST /api/v1/executors/{executor_id}/heartbeat

用途：周期性心跳，上报当前状态和容量，拉取待处理 Command。

请求：
```json
{
  "status": "ONLINE",
  "capabilities": {
    "bmc_worker_slots_free": 2,
    "ssh_worker_slots_free": 5
  },
  "active_job_ids": ["job-001", "job-002"],
  "cpu_percent": 45.0,
  "mem_percent": 60.0,
  "local_time": "2026-06-08T10:05:00Z"
}
```

响应：
```json
{
  "server_time": "2026-06-08T10:05:00Z",
  "next_heartbeat_seconds": 30,
  "pending_commands": [
    {
      "command_id": "cmd-xyz",
      "command_type": "CANCEL_JOB",
      "run_id": "run-20260608-001",
      "job_id": "job-003",
      "created_at": "2026-06-08T10:04:00Z",
      "expires_at": "2026-06-08T10:09:00Z"
    }
  ]
}
```

幂等要求：重复心跳覆盖上次状态，按 `active_job_ids` 更新服务端视图。
关键错误码：`EXECUTOR_NOT_FOUND`(404)、`EXECUTOR_OFFLINE`(409)。

### 4.3 POST /api/v1/executors/{executor_id}/jobs:poll

用途：执行端主动拉取待执行的 Job（长轮询）。

请求：
```json
{
  "max_jobs": 3,
  "resource_whitelist": ["bmc://10.0.0.1", "ssh://10.0.1.1"],
  "protocol_preference": ["BMC", "SSH"],
  "poll_timeout_seconds": 30
}
```

响应：
```json
{
  "jobs": [
    {
      "command_id": "cmd-abc123",
      "command_type": "ASSIGN_JOB",
      "job": {
        "job_id": "job-004",
        "run_id": "run-20260608-001",
        "device_id": "dev-001",
        "task_id": "task-login-check",
        "attempt": 1,
        "max_attempts": 3,
        "resource_lock_uri": "bmc://10.0.0.1",
        "task_snapshot": { "...": "TaskDefinition 完整内容" },
        "device_snapshot": {
          "device_id": "dev-001",
          "device_name": "Switch-A",
          "bmc_ip": "10.0.0.1",
          "bmc_username": "admin",
          "bmc_password_ref": "secret://bmc/switch-a",
          "inband_ip": "10.0.1.1",
          "inband_username": "netadmin",
          "inband_password_ref": "secret://ssh/switch-a",
          "ssh_type": "SSH_VRP",
          "resource_locks": [
            {"lock_uri": "bmc://10.0.0.1", "lock_type": "BMC", "lock_scope": "oob", "lock_exclusive": true},
            {"lock_uri": "ssh-vrp://10.0.1.1", "lock_type": "SSH_VRP", "lock_scope": "inband", "lock_exclusive": true}
          ]
        },
        "timeout_seconds": 60
      }
    }
  ],
  "server_time": "2026-06-08T10:00:10Z"
}
```

幂等要求：服务端同一 Job 不会分发给两个不同 executor（通过 resource lock 保证）。执行端收到后必须调用 `jobs:accept` 确认。
关键错误码：`EXECUTOR_NOT_FOUND`(404)、`DISPATCH_NO_ELIGIBLE_JOB`(204)。

### 4.4 POST /api/v1/jobs/{job_id}:accept

用途：执行端确认接受 Job，触发服务端锁定资源。

请求：
```json
{
  "executor_id": "exec-01",
  "accepted_at": "2026-06-08T10:00:12Z"
}
```

响应：
```json
{
  "job_id": "job-004",
  "status": "ACCEPTED",
  "lock_acquired": true,
  "lock_uri": "bmc://10.0.0.1",
  "lock_expires_at": "2026-06-08T10:05:12Z"
}
```

幂等要求：同一 `job_id` + `executor_id` 重复 accept 返回当前状态，不重复加锁。
关键错误码：`JOB_NOT_FOUND`(404)、`LOCK_CONFLICT`(409)、`JOB_ALREADY_ACCEPTED_BY_OTHER`(409)。

### 4.5 POST /api/v1/jobs/{job_id}:progress

用途：执行过程中上报进度和 step 结果（可选中间状态）。

请求：
```json
{
  "executor_id": "exec-01",
  "job_id": "job-004",
  "status": "RUNNING",
  "current_step": 2,
  "total_steps": 5,
  "latest_step_result": {
    "step_index": 1,
    "step_name": "输入用户名",
    "status": "SUCCEEDED",
    "step_type": "BMC_FILL",
    "duration_ms": 1200
  },
  "reported_at": "2026-06-08T10:01:00Z"
}
```

响应：
```json
{
  "job_id": "job-004",
  "acknowledged": true,
  "command": null
}
```

幂等要求：后到的 progress 覆盖先到的（以 `reported_at` 或 step_index 为准）。
关键错误码：`JOB_NOT_FOUND`(404)。

### 4.6 POST /api/v1/jobs/{job_id}:finish

用途：上报 Job 最终执行结果。

请求：
```json
{
  "executor_id": "exec-01",
  "job_id": "job-004",
  "status": "SUCCEEDED",
  "started_at": "2026-06-08T10:00:13Z",
  "finished_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "step_results": [
    {
      "step_index": 0,
      "step_name": "打开 BMC 登录页",
      "status": "SUCCEEDED",
      "step_type": "BMC_NAVIGATE",
      "duration_ms": 3500
    }
  ],
  "error": null,
  "artifacts_summary": {
    "png_count": 2,
    "html_count": 0,
    "txt_count": 0,
    "total_size_bytes": 491520
  },
  "resource_released": true
}
```

响应：
```json
{
  "job_id": "job-004",
  "status": "SUCCEEDED",
  "lock_released": true,
  "next_action": null
}
```

幂等要求：`job_id` + `attempt` 相同则忽略重复提交（返回已记录的状态）。
关键错误码：`JOB_NOT_FOUND`(404)、`JOB_ATTEMPT_MISMATCH`(409)、`JOB_ALREADY_FINISHED`(409)。

### 4.7 POST /api/v1/jobs/{job_id}/events

用途：上报事件（可批量）。

请求：
```json
{
  "executor_id": "exec-01",
  "events": [
    {
      "event_id": "evt-001",
      "event_type": "JOB_STATUS_CHANGED",
      "job_id": "job-004",
      "from_status": "ACCEPTED",
      "to_status": "RUNNING",
      "created_at": "2026-06-08T10:00:13Z"
    },
    {
      "event_id": "evt-002",
      "event_type": "ARTIFACT_UPLOADED",
      "job_id": "job-004",
      "payload": { "artifact_id": "art-001", "artifact_type": "PNG_SCREENSHOT" },
      "created_at": "2026-06-08T10:02:35Z"
    }
  ]
}
```

响应：
```json
{
  "accepted": 2,
  "duplicated": 0,
  "rejected": 0
}
```

幂等要求：`event_id` 全局去重。
关键错误码：`JOB_NOT_FOUND`(404)、`EVENT_DUPLICATE`(409)。

### 4.8 POST /api/v1/jobs/{job_id}/artifacts

用途：小文件产物直接上传（multipart/form-data，≤ 10MB）。

请求：multipart/form-data
```
artifact_type: PNG_SCREENSHOT
relative_path: Switch-A/BMC_登录检查/step_01_login.png
checksum_sha256: e3b0c44298fc...
file: <binary>
```

响应：
```json
{
  "artifact_id": "art-001",
  "status": "UPLOADED",
  "url": "/api/v1/artifacts/art-001/download",
  "size_bytes": 245760
}
```

幂等要求：相同 `job_id` + `relative_path` + `checksum_sha256` 重复上传返回已有 `artifact_id`。
关键错误码：`ARTIFACT_TOO_LARGE`(413)、`ARTIFACT_CHECKSUM_MISMATCH`(400)、`ARTIFACT_DUPLICATE`(409)。

### 4.9 POST /api/v1/artifacts:prepare-upload

用途：大文件预签名上传准备（> 10MB）。

请求：
```json
{
  "job_id": "job-004",
  "artifact_type": "ZIP_BUNDLE",
  "relative_path": "Switch-A/BMC_登录检查/bundle.zip",
  "checksum_sha256": "abc123...",
  "size_bytes": 52428800,
  "content_type": "application/zip"
}
```

响应：
```json
{
  "artifact_id": "art-002",
  "upload_url": "https://storage.example.com/upload?sign=xxx",
  "upload_method": "PUT",
  "upload_headers": {
    "Content-Type": "application/zip",
    "X-Amz-ACL": "private"
  },
  "upload_expires_at": "2026-06-08T10:15:00Z",
  "chunk_size_bytes": 10485760
}
```

幂等要求：相同 `job_id` + `relative_path` + `checksum_sha256` 返回已有预签名 URL（如未过期）。
关键错误码：`ARTIFACT_DUPLICATE`(409)、`STORAGE_UNAVAILABLE`(503)。

### 4.10 POST /api/v1/artifacts/{artifact_id}:complete

用途：确认大文件上传完成。

请求：
```json
{
  "job_id": "job-004",
  "uploaded_size_bytes": 52428800,
  "final_checksum_sha256": "abc123..."
}
```

响应：
```json
{
  "artifact_id": "art-002",
  "status": "UPLOADED",
  "verified": true
}
```

幂等要求：重复 complete 返回已确认状态。
关键错误码：`ARTIFACT_NOT_FOUND`(404)、`ARTIFACT_CHECKSUM_MISMATCH`(400)。

### 4.11 POST /api/v1/runs

用途：执行端创建新 Run（适用于执行端主动发起任务的场景）。

请求：
```json
{
  "run_name": "手动巡检 20260608",
  "devices": ["dev-001", "dev-002"],
  "tasks": ["task-login-check", "task-ssh-version"],
  "priority": 5,
  "tags": ["manual"]
}
```

响应：
```json
{
  "run_id": "run-20260608-002",
  "status": "CREATED",
  "job_count": 4
}
```

幂等要求：使用 `X-Idempotency-Key`。
关键错误码：`DEVICE_NOT_FOUND`(400)、`TASK_NOT_FOUND`(400)。

### 4.12 POST /api/v1/runs/{run_id}:start

用途：启动 Run（使其进入 QUEUED 状态，开始调度）。

请求：`{}`
响应：
```json
{
  "run_id": "run-20260608-002",
  "status": "QUEUED"
}
```

### 4.13 GET /api/v1/runs/{run_id}

用途：查询 Run 详情和统计。

响应：Run 完整对象（见 2.4），包含 `stats` 字段。

### 4.14 POST /api/v1/runs/{run_id}:cancel

用途：取消 Run。

请求：
```json
{
  "reason": "人工取消"
}
```

响应：
```json
{
  "run_id": "run-20260608-002",
  "status": "CANCELED",
  "jobs_canceled": 2,
  "jobs_running_to_be_canceled": 1
}
```

### 4.15 POST /api/v1/runs/{run_id}:pause

用途：暂停 Run。

请求：`{}`
响应：
```json
{
  "run_id": "run-20260608-002",
  "status": "PAUSED",
  "active_jobs_will_complete": 3
}
```

### 4.16 POST /api/v1/runs/{run_id}:resume

用途：恢复 Run。

请求：`{}`
响应：
```json
{
  "run_id": "run-20260608-002",
  "status": "RUNNING"
}
```

---

## 5. Executor API：服务端主动调用执行端

### 通用约定

- Base URL：`https://{executor_ip}:{api_port}/executor/v1`
- 鉴权：Header `Authorization: Bearer {server_token}`
- 幂等 key：Header `X-Idempotency-Key: {uuid}`
- 请求签名：Header `X-Signature: hmac-sha256={signature}`

### 5.1 GET /executor/v1/status

用途：查询执行端当前状态。

响应：
```json
{
  "executor_id": "exec-01",
  "status": "BUSY",
  "version": "0.3.0",
  "hostname": "WIN-PC-001",
  "os": "Windows Server 2022",
  "uptime_seconds": 86400,
  "capabilities": {
    "max_bmc_workers": 4,
    "max_ssh_workers": 8,
    "bmc_worker_slots_used": 4,
    "ssh_worker_slots_used": 3,
    "bmc_worker_slots_free": 0,
    "ssh_worker_slots_free": 5
  },
  "cpu_percent": 65.0,
  "mem_percent": 70.0,
  "active_jobs": [
    { "job_id": "job-001", "status": "RUNNING", "resource_lock_uri": "bmc://10.0.0.1" },
    { "job_id": "job-002", "status": "RUNNING", "resource_lock_uri": "ssh://10.0.1.1" }
  ],
  "active_runs": ["run-20260608-001"],
  "local_time": "2026-06-08T10:05:00Z"
}
```

### 5.2 POST /executor/v1/commands

用途：服务端向执行端发送 Command（Push 模式核心接口）。

请求：
```json
{
  "commands": [
    {
      "command_id": "cmd-abc123",
      "command_type": "ASSIGN_JOB",
      "run_id": "run-20260608-001",
      "job": { "...": "Job 完整对象" },
      "created_at": "2026-06-08T10:00:10Z",
      "expires_at": "2026-06-08T10:05:10Z"
    }
  ]
}
```

响应：
```json
{
  "results": [
    {
      "command_id": "cmd-abc123",
      "status": "ACCEPTED",
      "detail": null
    }
  ]
}
```

status 可能值：`ACCEPTED`、`REJECTED`、`DUPLICATE`。

幂等要求：执行端按 `command_id` 去重。相同 `command_id` 返回 `DUPLICATE`。
关键错误码：`COMMAND_REJECTED_SLOTS_FULL`(429)、`COMMAND_REJECTED_LOCK_CONFLICT`(409)、`COMMAND_EXPIRED`(410)。

### 5.3 POST /executor/v1/jobs

用途：批量下发 Job（简化版，等价于 ASSIGN_JOB Command 数组）。

请求：与 5.2 相同格式，`command_type` 固定为 `ASSIGN_JOB`。
响应：与 5.2 相同格式。

### 5.4 GET /executor/v1/jobs/{job_id}

用途：查询执行端上单个 Job 状态。

响应：
```json
{
  "job_id": "job-001",
  "status": "RUNNING",
  "current_step": 2,
  "total_steps": 5,
  "started_at": "2026-06-08T10:00:13Z",
  "elapsed_seconds": 47
}
```

### 5.5 POST /executor/v1/jobs/{job_id}:cancel

用途：取消执行端上正在运行的 Job。

请求：
```json
{
  "reason": "Run 被取消",
  "command_id": "cmd-cancel-001"
}
```

响应：
```json
{
  "job_id": "job-001",
  "previous_status": "RUNNING",
  "new_status": "CANCELED",
  "acknowledged": true
}
```

幂等要求：`command_id` 去重。Job 已是终态则返回当前状态。
关键错误码：`JOB_NOT_FOUND`(404)、`JOB_ALREADY_TERMINAL`(409)。

### 5.6 POST /executor/v1/runs/{run_id}:pause

用途：暂停执行端上某 Run 的所有 Job 调度。

请求：
```json
{
  "command_id": "cmd-pause-001"
}
```

响应：
```json
{
  "run_id": "run-20260608-001",
  "active_jobs_continuing": 3,
  "pending_jobs_paused": 10,
  "acknowledged": true
}
```

说明：已 RUNNING 的 Job 会继续完成；QUEUED 的 Job 暂停派发。

### 5.7 POST /executor/v1/runs/{run_id}:resume

用途：恢复执行端上被暂停的 Run。

请求：
```json
{
  "command_id": "cmd-resume-001"
}
```

响应：
```json
{
  "run_id": "run-20260608-001",
  "resumed_jobs_count": 10,
  "acknowledged": true
}
```

### 5.8 POST /executor/v1/jobs/{job_id}/artifacts:request

用途：请求执行端补传某 Job 的产物。

请求：
```json
{
  "command_id": "cmd-reupload-001",
  "artifact_types": ["PNG_SCREENSHOT", "JSON_STRUCTURED"],
  "relative_paths": ["Switch-A/BMC_登录检查/step_01_login.png"]
}
```

响应：
```json
{
  "job_id": "job-001",
  "acknowledged": true,
  "message": "补传请求已接收，将通过 Server API 逐文件上传"
}
```

### 5.9 POST /executor/v1/config:update

用途：更新执行端运行时配置。

请求：
```json
{
  "command_id": "cmd-config-001",
  "config_updates": {
    "max_bmc_workers": 6,
    "max_ssh_workers": 10,
    "bmc_page_timeout": 90,
    "heartbeat_interval_seconds": 20
  }
}
```

响应：
```json
{
  "acknowledged": true,
  "applied": ["max_bmc_workers", "max_ssh_workers", "bmc_page_timeout", "heartbeat_interval_seconds"],
  "requires_restart": false
}
```

### 5.10 POST /executor/v1/ping

用途：服务端验证执行端可达性（轻量探活）。

请求：
```json
{
  "timestamp": "2026-06-08T10:05:00Z"
}
```

响应：
```json
{
  "pong": true,
  "executor_id": "exec-01",
  "server_timestamp": "2026-06-08T10:05:00Z",
  "executor_timestamp": "2026-06-08T10:05:01Z"
}
```

---

## 6. Command 统一协议

### 6.1 设计原则

- 无论 Push 还是 Pull 路径，传递给执行端的任务载体都是 `Command` 对象。
- Push 路径：服务端 POST `/executor/v1/commands`，请求体含 `commands[]`。
- Pull 路径：执行端 POST `/api/v1/executors/{id}/jobs:poll`，响应体 `jobs[]` 中每个元素是 Command。
- 心跳响应：`pending_commands[]` 携带非 ASSIGN 类 Command（CANCEL_JOB、PAUSE_RUN、RESUME_RUN 等）。

### 6.2 Command 结构

```json
{
  "command_id": "cmd-{uuid}",
  "command_type": "ASSIGN_JOB",
  "run_id": "run-xxx",
  "job_id": "job-xxx",
  "job": { "...": "仅 ASSIGN_JOB/ASSIGN_RUN 携带" },
  "config_updates": { "...": "仅 UPDATE_CONFIG 携带" },
  "artifact_request": { "...": "仅 REQUEST_ARTIFACT 携带" },
  "reason": "仅 CANCEL_*/PAUSE 携带",
  "created_at": "2026-06-08T10:00:00Z",
  "expires_at": "2026-06-08T10:05:00Z",
  "signature": "hmac-sha256=..."
}
```

### 6.3 Command 类型及使用场景

| command_type | Push(5.2) | Pull(4.3) | Heartbeat(4.2) | 说明 |
|---|---|---|---|---|
| ASSIGN_JOB | ✓ | ✓ | | 下发单个 Job |
| ASSIGN_RUN | ✓ | ✓ | | 下发整个 Run |
| CANCEL_JOB | ✓ | | ✓ | 取消 Job |
| CANCEL_RUN | ✓ | | ✓ | 取消 Run |
| PAUSE_RUN | ✓ | | ✓ | 暂停 Run |
| RESUME_RUN | ✓ | | ✓ | 恢复 Run |
| UPDATE_CONFIG | ✓ | | ✓ | 更新配置 |
| REQUEST_ARTIFACT | ✓ | | ✓ | 补传产物 |
| PING | ✓ | | ✓ | 探活 |

### 6.4 重复 command_id 处理

执行端维护已处理 `command_id` 集合（LRU 淘汰，容量 >= 10000）。收到 `command_id` 已存在时：

- `ASSIGN_JOB`/`ASSIGN_RUN`：返回 `DUPLICATE`，不重复创建 Job。
- `CANCEL_JOB`/`CANCEL_RUN`：返回 `DUPLICATE`，并附带当前 Job/Run 状态。
- `UPDATE_CONFIG`：返回 `DUPLICATE`，附带当前配置值。
- 其他：返回 `DUPLICATE`。

### 6.5 Command 过期

执行端收到 `expires_at` 已过期的 Command 时，返回 `COMMAND_EXPIRED`(410)。服务端应将过期 Command 标记为 `EXPIRED`。

---

## 7. 资源锁与并发调度

### 7.1 服务端调度层

服务端维护全局资源锁表（Redis/DB），字段见 2.11 ResourceLock。

调度流程：
1. Run 进入 QUEUED，遍历其 Job。
2. 对每个 Job，检查其 `resource_lock_uri` 是否已被锁。
3. 若未锁，查找有空闲 slot 的 executor（`bmc_worker_slots_free > 0` 或 `ssh_worker_slots_free > 0`）。
4. 若找到 executor，分配 Job，设置资源锁（TTL = job timeout + buffer）。
5. 若未找到 executor 或资源被锁，Job 保留在 QUEUED，等待下一轮调度。
6. 资源锁在 Job finish/timeout/cancel 时释放。

### 7.2 执行端本地防重

执行端在 accept Job 后：
1. 检查本地是否已有同名 `resource_lock_uri` 正在运行。
2. 若有，返回 `LOCK_CONFLICT`。
3. 若无，在本地内存中标记该 `resource_lock_uri` 为占用。
4. Job 完成/失败/取消后释放。

### 7.3 资源类型区分与并发规则

| 资源类型 | lock_uri 前缀 | 独占 | 可与其他类型并行 |
|---|---|---|---|
| BMC | `bmc://` | 是 | 可与 SSH/SSH_VRP/SSH_LINUX 并行 |
| SSH | `ssh://` | 是 | 可与 BMC 并行，不与 SSH_VRP/SSH_LINUX 并行（同 IP） |
| SSH_VRP | `ssh-vrp://` | 是 | 可与 BMC 并行 |
| SSH_LINUX | `ssh-linux://` | 是 | 可与 BMC 并行 |

**核心规则**：
- 同一 `lock_uri` 在同一时刻只能被一个 Job 持有（exclusive）。
- 不同 `lock_uri` 可并发，即使指向同一物理设备（如 BMC 和 SSH 可同时操作同一设备）。
- 同一 IP 的不同 SSH 类型（ssh://、ssh-vrp://、ssh-linux://）视为不同资源锁，但执行端应确保实际 SSH 连接不冲突（建议不同类型使用不同连接）。

### 7.4 max_bmc_workers / max_ssh_workers 参与调度

- 服务端维护每个 executor 的 `bmc_worker_slots_free` 和 `ssh_worker_slots_free`。
- 下发 BMC 类 Job 时消耗 `bmc_worker_slots_free -= 1`。
- 下发 SSH 类 Job 时消耗 `ssh_worker_slots_free -= 1`。
- Job 完成时恢复。
- 调度算法优先填满 worker slots，避免 executor 空转。

### 7.5 大批量设备避免资源空转

- 服务端按 `resource_lock_uri` 分组 Job，同一 lock_uri 的 Job 串行排队。
- 未被锁的设备 Job 优先调度。
- 执行端上报 `cpu_percent`/`mem_percent`，服务端避免向高负载 executor 继续派发。
- 服务端心跳超时（> 3 × heartbeat_interval）时将 executor 标记为 UNRESPONSIVE，其持有的资源锁强制释放，Job 标记为 LOST 并触发重试或重新调度。

---

## 8. 执行统计设计

### 8.1 统计字段

| 字段 | 来源 | 说明 |
|---|---|---|
| `run_total_duration_ms` | run.finished_at - run.started_at | Run 总耗时 |
| `job_duration_ms` | job.finished_at - job.started_at | 单 Job 执行耗时 |
| `step_duration_ms` | step_result.duration_ms | 单 step 耗时 |
| `queue_wait_ms` | job.dispatched_at - job.queued_at | 排队等待时间 |
| `dispatch_latency_ms` | job.accepted_at - job.dispatched_at | 网络下发延迟 |
| `executor_runtime_ms` | job.finished_at - job.accepted_at | 执行端实际耗时 |
| `artifact_upload_duration_ms` | artifact.uploaded_at - artifact.created_at | 产物上传耗时 |
| `retry_count` | 统计 job.attempt > 1 的次数 | 总重试次数 |
| `success_count` | status=SUCCEEDED 的 Job 数 | 成功数 |
| `failed_count` | status=FAILED 的 Job 数 | 失败数 |
| `timeout_count` | status=TIMEOUT 的 Job 数 | 超时数 |
| `canceled_count` | status=CANCELED 的 Job 数 | 取消数 |
| `skipped_count` | status=SKIPPED 的 Job 数 | 跳过数 |
| `per_device_summary` | 按 device_id 聚合 | 每设备统计 |
| `per_task_summary` | 按 task_id 聚合 | 每任务统计 |
| `per_executor_summary` | 按 executor_id 聚合 | 每执行端统计 |

### 8.2 计算时机

- Job 级统计：在 `jobs:finish` 时写入。
- Run 级统计：在 Run 变为终态（SUCCEEDED/FAILED/TIMEOUT/CANCELED）时聚合计算。
- 实时统计：通过 `GET /api/v1/runs/{run_id}` 返回当前快照。

### 8.3 per_device_summary 示例

```json
{
  "device_id": "dev-001",
  "device_name": "Switch-A",
  "job_count": 5,
  "success_count": 4,
  "failed_count": 1,
  "timeout_count": 0,
  "canceled_count": 0,
  "skipped_count": 0,
  "total_duration_ms": 45000,
  "avg_job_duration_ms": 9000,
  "max_job_duration_ms": 15000,
  "min_job_duration_ms": 5000,
  "queue_wait_total_ms": 12000
}
```

### 8.4 per_task_summary 示例

```json
{
  "task_id": "task-login-check",
  "task_name": "BMC 登录检查",
  "job_count": 50,
  "success_count": 48,
  "failed_count": 2,
  "avg_duration_ms": 12000,
  "p50_duration_ms": 11000,
  "p95_duration_ms": 25000,
  "p99_duration_ms": 35000
}
```

### 8.5 per_executor_summary 示例

```json
{
  "executor_id": "exec-01",
  "hostname": "WIN-PC-001",
  "job_count": 50,
  "success_count": 42,
  "failed_count": 5,
  "timeout_count": 2,
  "canceled_count": 1,
  "total_duration_ms": 180000,
  "avg_queue_wait_ms": 3000,
  "max_concurrent_jobs": 8
}
```

---

## 9. 产物上传设计

### 9.1 artifact_type 枚举

| 值 | 说明 | 扩展名 |
|---|---|---|
| `PNG_SCREENSHOT` | 网页截图 | .png |
| `HTML_PAGE` | 保存的 HTML | .html |
| `TXT_SSH_OUTPUT` | SSH 命令文本输出 | .txt |
| `JSON_STRUCTURED` | 结构化结果 | .json |
| `CSV_SUMMARY` | CSV 汇总 | .csv |
| `LOG` | 执行日志 | .log |
| `ZIP_BUNDLE` | 汇总压缩包 | .zip |

### 9.2 小文件直接上传（≤ 10MB）

- 接口：`POST /api/v1/jobs/{job_id}/artifacts`
- 方式：multipart/form-data
- 单文件直接附带二进制内容
- 服务端校验 checksum 后存储

### 9.3 大文件预签名上传（> 10MB）

流程：
1. 执行端调用 `POST /api/v1/artifacts:prepare-upload` 获取预签名 URL。
2. 执行端直接 PUT 文件到对象存储（或分片上传）。
3. 执行端调用 `POST /api/v1/artifacts/{artifact_id}:complete` 确认上传。
4. 服务端校验文件存在性和 checksum。

### 9.4 分片上传预留

在 `prepare-upload` 响应中返回 `chunk_size_bytes`，执行端可按此大小分片，使用对象存储的分片上传 API（如 S3 Multipart Upload）。

### 9.5 产物补传

- 服务端主动：通过 Executor API `POST /executor/v1/jobs/{job_id}/artifacts:request` 下发补传命令。
- 执行端收到后，扫描本地产物目录，通过 Server API 逐个上传缺失的产物。
- 执行端可指定 `artifact_types` 过滤或 `relative_paths` 精确指定。

### 9.6 产物 checksum

- 每个产物的 `checksum_sha256` 在上传时由执行端计算并提供。
- 服务端在上传完成后校验。
- 小文件：上传时即校验。
- 大文件：complete 时校验。
- 校验失败返回 `ARTIFACT_CHECKSUM_MISMATCH`。

### 9.7 relative_path

- 路径相对于 Run/Job 的产物根目录。
- 格式：`{device_name}/{task_name}/{filename}`
- 示例：`Switch-A/BMC_登录检查/step_01_login.png`

### 9.8 filename_template

- 由 TaskDefinition 的 `image_name_template` 决定。
- 变量：`{device_name}`、`{task_name}`、`{step}`、`{timestamp}`、`{attempt}`。
- 执行端渲染后生成实际文件名。

---

## 10. 错误码设计

### 10.1 BMC 类

| code | message | retryable | category |
|---|---|---|---|
| BMC_CONNECT_FAILED | BMC 连接失败: {ip}:{port} | true | BMC |
| BMC_TIMEOUT | BMC 页面加载超时: {url} | true | BMC |
| BMC_AUTH_FAILED | BMC 登录认证失败 | false | BMC |
| BMC_ELEMENT_NOT_FOUND | BMC 页面元素未找到: {selector} | false | BMC |
| BMC_SCREENSHOT_FAILED | BMC 截图失败: {reason} | true | BMC |
| BMC_NAVIGATION_FAILED | BMC 页面导航失败: {url} | true | BMC |
| BMC_BROWSER_CRASH | BMC 浏览器进程崩溃 | true | BMC |

### 10.2 SSH 类

| code | message | retryable | category |
|---|---|---|---|
| SSH_CONNECT_FAILED | SSH 连接失败: {ip}:{port} | true | SSH |
| SSH_AUTH_FAILED | SSH 认证失败 | false | SSH |
| SSH_TIMEOUT | SSH 命令执行超时: {command} | true | SSH |
| SSH_COMMAND_FAILED | SSH 命令执行失败: exit_code={code} | false | SSH |
| SSH_CONNECTION_LOST | SSH 连接意外断开 | true | SSH |
| SSH_CHANNEL_ERROR | SSH 通道错误 | true | SSH |
| SSH_VRP_INTERACTIVE_TIMEOUT | VRP 交互式会话超时 | true | SSH |

### 10.3 Executor 类

| code | message | retryable | category |
|---|---|---|---|
| EXECUTOR_NOT_FOUND | 执行端未注册: {executor_id} | false | EXECUTOR |
| EXECUTOR_OFFLINE | 执行端离线: {executor_id} | true | EXECUTOR |
| EXECUTOR_UNRESPONSIVE | 执行端心跳超时: {executor_id} | true | EXECUTOR |
| EXECUTOR_SLOTS_FULL | 执行端 worker slot 已满: {executor_id} | true | EXECUTOR |
| EXECUTOR_VERSION_TOO_OLD | 执行端版本过低: {version}, 最低要求: {min_version} | false | EXECUTOR |
| EXECUTOR_REGISTER_DENIED | 执行端注册被拒绝: {reason} | false | EXECUTOR |
| EXECUTOR_ID_INVALID | executor_id 格式无效 | false | EXECUTOR |

### 10.4 Dispatch 类

| code | message | retryable | category |
|---|---|---|---|
| DISPATCH_NO_ELIGIBLE_JOB | 当前无可分派的 Job | true | DISPATCH |
| DISPATCH_LOCK_CONFLICT | 资源锁冲突: {lock_uri} 已被 {holder_job_id} 持有 | true | DISPATCH |
| DISPATCH_NO_AVAILABLE_EXECUTOR | 无可用执行端 | true | DISPATCH |
| DISPATCH_COMMAND_EXPIRED | Command 已过期: {command_id} | false | DISPATCH |
| DISPATCH_JOB_ALREADY_DISPATCHED | Job 已分派给其他执行端 | false | DISPATCH |
| DISPATCH_MAX_RETRY_EXCEEDED | Job 重试次数已达上限: {max_attempts} | false | DISPATCH |

### 10.5 Artifact 类

| code | message | retryable | category |
|---|---|---|---|
| ARTIFACT_NOT_FOUND | 产物不存在: {artifact_id} | false | ARTIFACT |
| ARTIFACT_TOO_LARGE | 产物大小超出限制: {size_bytes} > {max_bytes} | false | ARTIFACT |
| ARTIFACT_CHECKSUM_MISMATCH | 产物 checksum 不匹配 | true | ARTIFACT |
| ARTIFACT_DUPLICATE | 产物重复: job={job_id} path={relative_path} | false | ARTIFACT |
| ARTIFACT_UPLOAD_FAILED | 产物上传失败: {reason} | true | ARTIFACT |
| ARTIFACT_STORAGE_FULL | 存储空间不足 | false | ARTIFACT |
| ARTIFACT_TYPE_INVALID | 无效的产物类型: {type} | false | ARTIFACT |

### 10.6 Config 类

| code | message | retryable | category |
|---|---|---|---|
| CONFIG_KEY_INVALID | 无效的配置项: {key} | false | CONFIG |
| CONFIG_VALUE_INVALID | 配置值无效: {key}={value} | false | CONFIG |
| CONFIG_APPLY_FAILED | 配置应用失败: {reason} | false | CONFIG |
| CONFIG_REQUIRES_RESTART | 配置变更需要重启生效 | false | CONFIG |

### 10.7 Auth 类

| code | message | retryable | category |
|---|---|---|---|
| AUTH_TOKEN_MISSING | 缺少认证 Token | false | AUTH |
| AUTH_TOKEN_INVALID | 认证 Token 无效 | false | AUTH |
| AUTH_TOKEN_EXPIRED | 认证 Token 已过期 | false | AUTH |
| AUTH_SIGNATURE_INVALID | 请求签名无效 | false | AUTH |
| AUTH_PERMISSION_DENIED | 权限不足: {action} | false | AUTH |
| AUTH_SECRET_NOT_FOUND | Secret 引用未找到: {secret_ref} | false | AUTH |

### 10.8 System 类

| code | message | retryable | category |
|---|---|---|---|
| SYSTEM_INTERNAL_ERROR | 系统内部错误 | true | SYSTEM |
| SYSTEM_DATABASE_ERROR | 数据库错误 | true | SYSTEM |
| SYSTEM_RATE_LIMITED | 请求频率超限，请稍后重试 | true | SYSTEM |
| SYSTEM_SERVICE_UNAVAILABLE | 服务暂时不可用 | true | SYSTEM |
| SYSTEM_TIMEOUT | 系统处理超时 | true | SYSTEM |
| SYSTEM_REDIS_ERROR | Redis 错误 | true | SYSTEM |

---

## 11. API v0.1 最小落地范围

以下接口即可组成完整的最小闭环（同时包含 Push 和 Pull 两种能力）：

### Server API（执行端 → 服务端）

| 接口 | 用途 |
|---|---|
| `POST /api/v1/executors/register` | 执行端注册 |
| `POST /api/v1/executors/{id}/heartbeat` | 心跳 + 拉取控制命令 |
| `POST /api/v1/executors/{id}/jobs:poll` | 主动拉取任务 |
| `POST /api/v1/jobs/{job_id}:accept` | 确认接受任务 |
| `POST /api/v1/jobs/{job_id}:finish` | 上报执行结果 |
| `POST /api/v1/jobs/{job_id}/artifacts` | 上传小文件产物 |
| `POST /api/v1/artifacts:prepare-upload` | 大文件预签名 |
| `POST /api/v1/artifacts/{id}:complete` | 大文件确认上传 |
| `POST /api/v1/jobs/{job_id}/events` | 上报事件 |

### Executor API（服务端 → 执行端）

| 接口 | 用途 |
|---|---|
| `POST /executor/v1/commands` | 主动下发任务/命令 |
| `GET /executor/v1/status` | 查询执行端状态 |
| `POST /executor/v1/jobs/{job_id}:cancel` | 取消任务 |
| `POST /executor/v1/ping` | 探活 |

### 最小状态机覆盖

- Run: CREATED → QUEUED → RUNNING → SUCCEEDED / FAILED / CANCELED
- Job: CREATED → QUEUED → DISPATCHED → ACCEPTED → RUNNING → SUCCEEDED / FAILED / TIMEOUT / CANCELED
- Executor: REGISTERING → ONLINE → OFFLINE / UNRESPONSIVE

### 最小 Command 覆盖

- ASSIGN_JOB、CANCEL_JOB、CANCEL_RUN、PING

---

## 12. 安全设计要点

1. **密码不落盘传输**：接口中只传 `password_ref` / `secret_ref`，执行端通过本地安全模块解析为实际密码。
2. **双向 TLS**：生产环境 Server API 和 Executor API 均使用 HTTPS + 客户端证书。
3. **Token 鉴权**：执行端持 `executor_token` 访问 Server API；服务端持 `server_token` 访问 Executor API。
4. **请求签名**：Executor API 使用 `X-Signature: hmac-sha256={body_hash}` 头，防止重放。
5. **幂等 Key**：写操作使用 `X-Idempotency-Key` 头，服务端缓存至少 24 小时。
6. **Token 轮换**：`POST /api/v1/executors/register` 响应中返回新 token。

---

FINAL_OUTPUT_END
