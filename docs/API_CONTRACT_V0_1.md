FINAL_OUTPUT_BEGIN

# API Contract v0.1 — 最小闭环

---

## 1. v0.1 范围说明

v0.1 只覆盖**一个执行端与一个服务端之间的最小任务闭环**。不涉及多执行端调度、Run 管理、大文件分片、配置热更新、mTLS。

双向能力：
- **Pull 路径**：执行端通过 Server API 注册 → 心跳 → 拉取 Job → 确认 → 执行 → 上报结果 → 上传产物。
- **Push 路径**：服务端通过 Executor API 主动下发 Job / 取消 Job / 查询状态。
- 两种路径共用同一套 Job、Command、Artifact 模型和同一套 Job 状态机。

---

## 2. 核心模型（仅 v0.1 字段）

### 2.1 Executor

```json
{
  "executor_id": "exec-01",
  "hostname": "WIN-PC-001",
  "ip": "10.0.1.100",
  "os": "Windows Server 2022",
  "version": "0.2.5rc1",
  "status": "ONLINE",
  "capabilities": {
    "max_bmc_workers": 4,
    "max_ssh_workers": 8,
    "bmc_worker_slots_free": 2,
    "ssh_worker_slots_free": 5,
    "supported_protocols": ["BMC", "SSH", "SSH_VRP", "SSH_LINUX"],
    "known_lock_uris": ["bmc://10.0.0.1", "ssh-vrp://10.0.1.1"]
  },
  "registered_at": "2026-06-08T10:00:00Z",
  "last_heartbeat_at": "2026-06-08T10:05:00Z"
}
```

### 2.2 Device

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
      "lock_exclusive": true
    },
    {
      "lock_uri": "ssh-vrp://10.0.1.1",
      "lock_type": "SSH_VRP",
      "lock_scope": "inband",
      "lock_exclusive": true
    }
  ],
  "credentials": {
    "bmc_username": "admin",
    "bmc_password_ref": "secret://bmc/switch-a",
    "ssh_username": "netadmin",
    "ssh_password_ref": "secret://ssh/switch-a"
  },
  "enabled": true
}
```

**lock_uri 推导（强制规则）**：

| 任务 execution_mode | lock_uri | lock_type |
|---|---|---|
| BMC_URL / BMC_ACTIONS | `bmc://{oob_ip}` | BMC |
| SSH_CMD + ssh_type=SSH | `ssh://{inband_ip}` | SSH |
| SSH_CMD + ssh_type=SSH_LINUX | `ssh-linux://{inband_ip}` | SSH_LINUX |
| SSH_CMD + ssh_type=SSH_VRP | `ssh-vrp://{inband_ip}` | SSH_VRP |

**严禁使用 `device_name` 作为资源锁 key。**

### 2.3 TaskSnapshot

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
        {"type": "text_exists", "target": "", "expect": "登录"},
        {"type": "element_exists", "target": "#username", "expect": ""}
      ]
    }
  ],
  "output_dir_template": "{任务序号}.{任务名称}/{设备分类}",
  "image_name_template": "{TaskIP}-{任务名称}",
  "timeout_seconds": 60,
  "per_group_timeout_seconds": {
    "A3": 900,
    "L1": 60,
    "L2": 60
  },
  "retry_count": 2,
  "full_screenshot": false,
  "screenshot_mode": "auto"
}
```

`per_group_timeout_seconds` 为可选字段，用于同一任务覆盖多个设备分组时按分组覆盖 `timeout_seconds`。

### 2.4 Job

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
  "task_snapshot": { "...": "TaskSnapshot 完整内容" },
  "device_snapshot": { "...": "Device 快照，含 resource_locks 和 credential ref" },
  "created_at": "2026-06-08T10:00:05Z",
  "queued_at": "2026-06-08T10:00:05Z",
  "dispatched_at": "2026-06-08T10:00:10Z",
  "accepted_at": "2026-06-08T10:00:12Z",
  "started_at": "2026-06-08T10:00:13Z",
  "finished_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "step_results": [],
  "error": null
}
```

### 2.5 Command

```json
{
  "command_id": "cmd-abc123",
  "command_type": "ASSIGN_JOB",
  "run_id": "run-20260608-001",
  "job_id": "job-001",
  "job": { "...": "仅 ASSIGN_JOB 携带，含 task_snapshot" },
  "reason": "仅 CANCEL_JOB 携带",
  "created_at": "2026-06-08T10:00:10Z",
  "expires_at": "2026-06-08T10:05:10Z"
}
```

v0.1 command_type 枚举：
- `ASSIGN_JOB` — 下发 Job
- `CANCEL_JOB` — 取消 Job
- `PING` — 探活

### 2.6 Artifact

```json
{
  "artifact_id": "art-001",
  "job_id": "job-001",
  "artifact_type": "PNG_SCREENSHOT",
  "relative_path": "Switch-A/BMC_登录检查/step_01_login.png",
  "checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "size_bytes": 245760,
  "content_type": "image/png",
  "status": "STORED",
  "created_at": "2026-06-08T10:02:30Z"
}
```

v0.1 artifact_type 枚举：`PNG_SCREENSHOT`、`HTML_PAGE`、`TXT_SSH_OUTPUT`、`JSON_STRUCTURED`、`LOG`。

v0.1 只支持小文件 multipart 上传（≤ 10MB）。大文件/分片/预签名在 v0.2。

### 2.7 ErrorInfo

```json
{
  "code": "BMC_TIMEOUT",
  "message": "BMC 页面加载超时: https://10.0.0.1/login",
  "retryable": true,
  "category": "BMC",
  "details": {
    "url": "https://10.0.0.1/login",
    "timeout_seconds": 60
  }
}
```

### 2.8 ResourceLock

```json
{
  "lock_uri": "bmc://10.0.0.1",
  "lock_type": "BMC",
  "lock_exclusive": true,
  "holder_job_id": "job-001",
  "holder_executor_id": "exec-01",
  "acquired_at": "2026-06-08T10:00:12Z",
  "expires_at": "2026-06-08T10:05:12Z"
}
```

---

## 3. 最小状态机

### 3.1 Job 状态机

```
QUEUED → DISPATCHED → ACCEPTED → RUNNING → SUCCEEDED
                                          → FAILED
                                          → TIMEOUT
  QUEUED     → CANCELED
  DISPATCHED → CANCELED
  ACCEPTED   → CANCELED
  RUNNING    → CANCELED
  DISPATCHED → LOST
  FAILED     → QUEUED (retry, attempt+1)
  TIMEOUT    → QUEUED (retry, attempt+1)
```

- `QUEUED`：Job 在服务端等待调度。
- `DISPATCHED`：Job 已发送给执行端，等待 ACK。
- `ACCEPTED`：执行端确认接受，资源锁已获取。
- `RUNNING`：执行端正在执行。
- `SUCCEEDED`：执行成功。
- `FAILED`：执行失败。
- `TIMEOUT`：Job 超时未完成。
- `CANCELED`：被取消。
- `LOST`：执行端失联。

### 3.2 Command 状态机

```
CREATED → SENT → ACKED
  CREATED → SENT → REJECTED
  CREATED → EXPIRED
```

- `CREATED`：服务端创建 Command。
- `SENT`：Command 已发送（Push 或心跳带带）。
- `ACKED`：执行端确认处理。
- `REJECTED`：执行端拒绝（slot 满、锁冲突、版本不兼容）。
- `EXPIRED`：超过 `expires_at` 未被处理。

### 3.3 Artifact 状态机

```
PENDING → UPLOADING → STORED
  PENDING → UPLOADING → FAILED → PENDING (retry)
```

- `PENDING`：产物已记录，等待上传。
- `UPLOADING`：上传中。
- `STORED`：上传完成并校验通过。
- `FAILED`：上传失败。

---

## 4. Server API（执行端 → 服务端）

### 通用约定

- Base URL：`https://{server_host}/api/v1`
- 鉴权：`Authorization: Bearer {executor_token}`
- 幂等键：`X-Idempotency-Key: {uuid}`（写操作必带）
- Content-Type：`application/json`（产物上传除外）

---

### 4.1 POST /api/v1/executors/register

用途：执行端注册，上报身份和容量。

请求：
```json
{
  "executor_id": "exec-01",
  "hostname": "WIN-PC-001",
  "ip": "10.0.1.100",
  "os": "Windows Server 2022",
  "version": "0.2.5rc1",
  "capabilities": {
    "max_bmc_workers": 4,
    "max_ssh_workers": 8,
    "supported_protocols": ["BMC", "SSH", "SSH_VRP", "SSH_LINUX"],
    "known_lock_uris": ["bmc://10.0.0.1", "ssh-vrp://10.0.1.1"]
  }
}
```

响应：
```json
{
  "executor_id": "exec-01",
  "status": "ONLINE",
  "executor_token": "eyJ...",
  "server_time": "2026-06-08T10:00:00Z",
  "heartbeat_interval_seconds": 30,
  "api_poll_interval_seconds": 10
}
```

幂等键：`X-Idempotency-Key` 基于 `executor_id`。重复注册视为 re-register。
错误码：`EXECUTOR_ID_INVALID`(400)、`AUTH_TOKEN_INVALID`(401)。

---

### 4.2 POST /api/v1/executors/{executor_id}/heartbeat

用途：周期心跳，上报容量和 active_jobs，拉取 control command。

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
      "reason": "Run 被取消",
      "created_at": "2026-06-08T10:04:00Z",
      "expires_at": "2026-06-08T10:09:00Z"
    }
  ]
}
```

幂等：每次心跳覆盖上次状态。
错误码：`EXECUTOR_NOT_FOUND`(404)。

---

### 4.3 POST /api/v1/executors/{executor_id}/jobs:poll

用途：主动拉取待执行 Job（长轮询）。

请求：
```json
{
  "max_jobs": 3,
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
        "task_snapshot": { "...": "TaskSnapshot 完整内容" },
        "device_snapshot": { "...": "Device 快照，含 resource_locks 和 credential ref" },
        "timeout_seconds": 60
      }
    }
  ],
  "server_time": "2026-06-08T10:00:10Z"
}
```

幂等：服务端通过 resource_lock 保证同一 Job 不发给两个执行端。执行端收到后须调 `jobs:accept` 确认。
错误码：`EXECUTOR_NOT_FOUND`(404)、空列表返回 200 + `"jobs": []`。

---

### 4.4 POST /api/v1/jobs/{job_id}:accept

用途：确认接受 Job，服务端锁定资源。

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

幂等键：`X-Idempotency-Key: {job_id}-{executor_id}-accept`。重复 accept 返回当前状态。
错误码：`JOB_NOT_FOUND`(404)、`LOCK_CONFLICT`(409)、`JOB_ALREADY_ACCEPTED_BY_OTHER`(409)。

---

### 4.5 POST /api/v1/jobs/{job_id}:finish

用途：上报最终执行结果。

请求：
```json
{
  "executor_id": "exec-01",
  "job_id": "job-004",
  "status": "SUCCEEDED",
  "attempt": 1,
  "started_at": "2026-06-08T10:00:13Z",
  "finished_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "step_results": [
    {
      "step_index": 0,
      "step_name": "打开 BMC 登录页",
      "status": "SUCCEEDED",
      "step_type": "BMC_NAVIGATE",
      "duration_ms": 3500,
      "details": ""
    }
  ],
  "error": null,
  "artifacts_summary": {
    "png_count": 2,
    "html_count": 1,
    "txt_count": 0,
    "total_size_bytes": 491520
  }
}
```

响应：
```json
{
  "job_id": "job-004",
  "status": "SUCCEEDED",
  "lock_released": true
}
```

幂等键：`X-Idempotency-Key: {job_id}-{attempt}-finish`。相同 attempt 的重复 finish 返回已记录状态。
错误码：`JOB_NOT_FOUND`(404)、`JOB_ATTEMPT_MISMATCH`(409)、`JOB_ALREADY_FINISHED`(409)。

---

### 4.6 POST /api/v1/jobs/{job_id}/artifacts

用途：小文件产物上传（multipart/form-data，≤ 10MB）。

请求（multipart/form-data）：
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
  "status": "STORED",
  "size_bytes": 245760
}
```

幂等：相同 `job_id` + `relative_path` + `checksum_sha256` 返回已有 artifact_id。
错误码：`ARTIFACT_TOO_LARGE`(413)、`ARTIFACT_CHECKSUM_MISMATCH`(400)、`ARTIFACT_DUPLICATE`(409)。

---

## 5. Executor API（服务端 → 执行端）

### 通用约定

- Base URL：`https://{executor_ip}:{executor_api_port}/executor/v1`
- 鉴权：`Authorization: Bearer {server_token}`
- 幂等键：`X-Idempotency-Key: {uuid}`
- Content-Type：`application/json`

---

### 5.1 GET /executor/v1/status

用途：查询执行端当前状态。

响应：
```json
{
  "executor_id": "exec-01",
  "status": "BUSY",
  "version": "0.2.5rc1",
  "hostname": "WIN-PC-001",
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
    {
      "job_id": "job-001",
      "status": "RUNNING",
      "resource_lock_uri": "bmc://10.0.0.1",
      "current_step": 2,
      "total_steps": 5
    }
  ],
  "local_time": "2026-06-08T10:05:00Z"
}
```

错误码：`AUTH_TOKEN_INVALID`(401)。

---

### 5.2 POST /executor/v1/commands

用途：Push 模式核心接口 — 服务端向执行端发送 Command 数组。

请求：
```json
{
  "commands": [
    {
      "command_id": "cmd-abc123",
      "command_type": "ASSIGN_JOB",
      "run_id": "run-20260608-001",
      "job_id": "job-004",
      "job": {
        "job_id": "job-004",
        "run_id": "run-20260608-001",
        "device_id": "dev-001",
        "task_id": "task-login-check",
        "attempt": 1,
        "max_attempts": 3,
        "resource_lock_uri": "bmc://10.0.0.1",
        "task_snapshot": { "...": "TaskSnapshot 完整内容" },
        "device_snapshot": { "...": "Device 快照" },
        "timeout_seconds": 60
      },
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

幂等：执行端按 `command_id` 去重。相同 command_id 返回 `DUPLICATE`。
错误码：`COMMAND_REJECTED_SLOTS_FULL`(429)、`COMMAND_REJECTED_LOCK_CONFLICT`(409)、`COMMAND_EXPIRED`(410)。

---

### 5.3 GET /executor/v1/jobs/{job_id}

用途：查询执行端上单个 Job 状态。

响应：
```json
{
  "job_id": "job-001",
  "status": "RUNNING",
  "current_step": 2,
  "total_steps": 5,
  "resource_lock_uri": "bmc://10.0.0.1",
  "started_at": "2026-06-08T10:00:13Z",
  "elapsed_seconds": 47
}
```

错误码：`JOB_NOT_FOUND`(404)。

---

### 5.4 POST /executor/v1/jobs/{job_id}:cancel

用途：取消执行端上正在运行的 Job。

请求：
```json
{
  "command_id": "cmd-cancel-001",
  "reason": "Run 被取消"
}
```

响应：
```json
{
  "job_id": "job-001",
  "previous_status": "RUNNING",
  "new_status": "CANCELED"
}
```

幂等：`command_id` 去重。Job 已是终态则返回当前状态。
错误码：`JOB_NOT_FOUND`(404)、`JOB_ALREADY_TERMINAL`(409)。

---

## 6. 双向能力说明

### Pull 路径（执行端主动）

```
Executor                          Server
   |                                |
   |-- register ──────────────────>|  注册
   |<─ token + config ────────────| 
   |                                |
   |-- heartbeat ─────────────────>|  心跳 + 拉控制命令
   |<─ pending_commands[] ────────|  (CANCEL_JOB / PING)
   |                                |
   |-- jobs:poll ─────────────────>|  拉取任务
   |<─ jobs[] (ASSIGN_JOB cmd) ───| 
   |                                |
   |-- jobs:accept ───────────────>|  确认接受
   |<─ lock_acquired ─────────────|
   |                                |
   |  [... 执行 ...]                | 
   |                                |
   |-- jobs:finish ───────────────>|  上报结果
   |<─ lock_released ─────────────|
   |                                |
   |-- jobs:artifacts ────────────>|  上传产物
   |<─ artifact_id ───────────────|
```

### Push 路径（服务端主动）

```
Server                           Executor
   |                                |
   |-- GET /status ───────────────>|  查询状态
   |<─ active_jobs[] ─────────────|
   |                                |
   |-- POST /commands ────────────>|  下发 ASSIGN_JOB / CANCEL_JOB
   |<─ results[] (ACCEPTED/REJECT) |
   |                                |
   |-- GET /jobs/{id} ────────────>|  查询单个 Job
   |<─ job status ────────────────|
   |                                |
   |-- POST /jobs/{id}:cancel ────>|  取消 Job
   |<─ new_status ────────────────|
```

### 模型统一

两条路径共用同一套：
- Job 结构（含 `task_snapshot` 和 `device_snapshot`）
- Command 结构（`command_id` + `command_type` + `job`）
- Job 状态机（QUEUED → DISPATCHED → ACCEPTED → RUNNING → SUCCEEDED/FAILED/TIMEOUT/CANCELED/LOST）
- 幂等规则（`command_id`、`job_id + attempt`、`job_id + relative_path + checksum`）

---

## 7. v0.1 错误码（最小集）

| code | message | retryable | category |
|---|---|---|---|
| BMC_CONNECT_FAILED | BMC 连接失败: {ip} | true | BMC |
| BMC_TIMEOUT | BMC 页面加载超时 | true | BMC |
| BMC_AUTH_FAILED | BMC 登录认证失败 | false | BMC |
| SSH_CONNECT_FAILED | SSH 连接失败: {ip} | true | SSH |
| SSH_AUTH_FAILED | SSH 认证失败 | false | SSH |
| SSH_TIMEOUT | SSH 命令超时 | true | SSH |
| EXECUTOR_NOT_FOUND | 执行端未注册 | false | EXECUTOR |
| EXECUTOR_OFFLINE | 执行端离线 | true | EXECUTOR |
| EXECUTOR_SLOTS_FULL | Worker slot 已满 | true | EXECUTOR |
| DISPATCH_NO_ELIGIBLE_JOB | 无可分配 Job | true | DISPATCH |
| DISPATCH_LOCK_CONFLICT | 资源锁冲突: {lock_uri} | true | DISPATCH |
| DISPATCH_COMMAND_EXPIRED | Command 已过期 | false | DISPATCH |
| JOB_NOT_FOUND | Job 不存在 | false | DISPATCH |
| JOB_ALREADY_FINISHED | Job 已终态 | false | DISPATCH |
| JOB_ALREADY_ACCEPTED_BY_OTHER | Job 已被其他执行端接受 | false | DISPATCH |
| JOB_ATTEMPT_MISMATCH | attempt 号不匹配 | false | DISPATCH |
| JOB_ALREADY_TERMINAL | Job 已是终态 | false | DISPATCH |
| LOCK_CONFLICT | 资源锁冲突 | true | DISPATCH |
| ARTIFACT_TOO_LARGE | 产物超过 10MB | false | ARTIFACT |
| ARTIFACT_CHECKSUM_MISMATCH | Checksum 不匹配 | true | ARTIFACT |
| ARTIFACT_DUPLICATE | 产物重复 | false | ARTIFACT |
| COMMAND_REJECTED_SLOTS_FULL | 执行端 slot 满 | true | EXECUTOR |
| COMMAND_REJECTED_LOCK_CONFLICT | 执行端本地锁冲突 | true | EXECUTOR |
| COMMAND_EXPIRED | Command 已过期 | false | DISPATCH |
| AUTH_TOKEN_INVALID | Token 无效 | false | AUTH |
| AUTH_TOKEN_MISSING | 缺少 Token | false | AUTH |

---

FINAL_OUTPUT_END
