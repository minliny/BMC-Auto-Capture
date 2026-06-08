FINAL_OUTPUT_BEGIN

# Direct Dispatch + Callback Flow v0.1

## 当前能力

```
Server                          Executor
   |                                |
   |-- POST /executor/v1/jobs ---->|  下发 external_task_id + task_snapshot
   |<-- {accepted, job_id} --------|  立即返回 ACCEPTED
   |                                |
   |                     [lock_uri 防并发]
   |                     [FakeRunner 执行]
   |                                |
   |<-- POST {callback.status_url} -|  真实 HTTP 回调 RUNNING
   |<-- POST {callback.status_url} -|  回调 SUCCEEDED/FAILED/TIMEOUT
   |                                |  (失败时 → CALLBACK_FAILED)
   |                                |
   |-- GET /executor/v1/jobs/{id} ->|  查询状态（含 lock_uri）
   |-- GET /executor/v1/status ---->|  查询执行端健康 + active_locks
```

## 启动 Executor API 服务

```bash
# 测试模式（fake callback — 回调不发出真实 HTTP）
python3 scripts/start_executor_api_server.py --host 0.0.0.0 --port 8765

# 生产模式（real HTTP callback）
python3 scripts/start_executor_api_server.py \
  --host 0.0.0.0 --port 8765 \
  --executor-id exec-win-001 \
  --callback-transport http \
  --callback-timeout 30

# Windows PowerShell
python scripts/start_executor_api_server.py --host 0.0.0.0 --port 8765 --executor-id exec-win-001 --callback-transport http
```

## Server 下发任务到执行机

### POST /executor/v1/jobs

```bash
curl -X POST http://<executor-ip>:8765/executor/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "command_id": "cmd-001",
    "command_type": "ASSIGN_JOB",
    "external_task_id": "server-task-123",
    "callback": {
      "status_url": "http://<server-ip>/api/tasks/server-task-123/status",
      "auth_token": "<your-auth-token>"
    },
    "job": {
      "job_id": "job-local-001",
      "run_id": "run-direct-001",
      "attempt": 1,
      "resource_lock": {
        "lock_uri": "bmc://10.146.219.1",
        "lock_exclusive": true
      },
      "device_snapshot": {
        "device_id": "dev-001",
        "device_name": "Switch-A",
        "device_group": "A3",
        "oob_ip": "10.146.219.1",
        "inband_ip": "10.10.10.1",
        "oob_username": "Administrator",
        "oob_password_ref": "secret:bmc-001",
        "inband_username": "root",
        "inband_password_ref": "secret:ssh-001"
      },
      "task_snapshot": {
        "task_id": "task-4.1.8",
        "task_name": "RAID配置测试",
        "task_type": "BMC_URL",
        "execution_mode": "BMC_URL",
        "url": "https://{oob_ip}/UI/Static/#/navigate/system/storage",
        "timeout_seconds": 300,
        "retry_count": 0
      }
    }
  }'
```

响应（成功）：
```json
{"accepted": true, "external_task_id": "server-task-123", "job_id": "job-local-001", "status": "ACCEPTED"}
```

## 回调 payload（服务端 callback.status_url 接收的 JSON）

执行机会向 `callback.status_url` 发送 POST 请求，含以下 header：
- `Content-Type: application/json`
- `Authorization: Bearer <auth_token>`（若提供）
- `X-Idempotency-Key: {external_task_id}-{job_id}-{status}`

### RUNNING 回调
```json
{
  "external_task_id": "server-task-123",
  "job_id": "job-local-001",
  "executor_id": "exec-win-001",
  "status": "RUNNING",
  "reported_at": "2026-06-08T10:00:13Z"
}
```

### SUCCEEDED 回调
```json
{
  "external_task_id": "server-task-123",
  "job_id": "job-local-001",
  "executor_id": "exec-win-001",
  "status": "SUCCEEDED",
  "reported_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "result": {"summary": "EXEC_SUCCEEDED", "steps_total": 3, "steps_success": 3, "steps_failed": 0},
  "error": null,
  "artifacts": []
}
```

### FAILED 回调
```json
{
  "external_task_id": "server-task-123",
  "job_id": "job-local-001",
  "executor_id": "exec-win-001",
  "status": "FAILED",
  "reported_at": "2026-06-08T10:01:30Z",
  "duration_ms": 60000,
  "result": null,
  "error": {"code": "BMC_PAGE_TIMEOUT", "message": "BMC page timeout", "retryable": true, "category": "BMC"},
  "artifacts": []
}
```

## lock_uri 推导规则

若 `resource_lock.lock_uri` 未显式提供，从 `device_snapshot` + `task_snapshot` 自动推导：

| execution_mode | 使用 IP | lock_uri |
|---|---|---|
| BMC_URL / BMC_ACTIONS | oob_ip | `bmc://{oob_ip}` |
| SSH_CMD + ssh_type 空 | inband_ip | `ssh://{inband_ip}` |
| SSH_CMD + ssh_type=SSH_VRP | inband_ip | `ssh-vrp://{inband_ip}` |
| SSH_CMD + ssh_type=SSH_LINUX | inband_ip | `ssh-linux://{inband_ip}` |

若推导失败（缺 IP），返回 MISSING_LOCK_URI 错误。**绝不 fallback 到 device_name**。

## 回调失败处理

- HTTP 非 2xx 或网络异常 → 回调失败
- Job 状态 → CALLBACK_FAILED
- 保留 `result_summary` 和 `duration_ms`（执行结果不丢失）
- Lock 已释放（不阻塞后续任务）
- 记录 `last_callback_status_code` 和 `last_callback_at`

## 当前不做的能力

| 能力 | 状态 |
|---|---|
| executor jobs:poll 主动拉取 | 不做 |
| heartbeat 心跳 | 不做 |
| 大文件/预签名上传 | 不做 |
| 真实 artifact 文件上传 | 不做（仅传 metadata） |
| WebSocket | 不做 |
| 分布式锁（Redis） | 不做 |
| Run pause/resume | 不做 |
| config:update | 不做 |
| BMC_ACTIONS / CUSTOM_SCRIPT | 不做（返回 UNSUPPORTED_TASK_TYPE） |

## FakeRunner vs RealRunnerAdapter

| | FakeRunner | RealRunnerAdapter |
|---|---|---|
| 用途 | 测试/调试 | 生产 |
| 执行 | 模拟 success/failure/timeout | 真实 BMCExecutor / SSHExecutor |
| 默认 | 是 | 否 |
| 开启 | `--runner fake` | `--runner real` |

## 开启真实执行

```bash
python3 scripts/start_executor_api_server.py \
  --host 0.0.0.0 --port 8765 \
  --executor-id exec-win-001 \
  --callback-transport http \
  --runner real \
  --output ./output_api_direct
```

## BMC_URL 下发 JSON 最小示例

```json
{
  "command_id": "cmd-001",
  "command_type": "ASSIGN_JOB",
  "external_task_id": "server-task-123",
  "callback": {"status_url": "http://server/api/tasks/server-task-123/status", "auth_token": "tok"},
  "job": {
    "job_id": "job-001",
    "resource_lock": {"lock_uri": "bmc://10.0.0.1"},
    "device_snapshot": {
      "device_name": "Switch-A", "device_group": "A3",
      "oob_ip": "10.0.0.1",
      "oob_username": "admin",
      "oob_password_ref": "env:BMC_PASSWORD",
      "inband_ip": "", "inband_username": "", "inband_password_ref": ""
    },
    "task_snapshot": {
      "task_name": "BMC Storage Page",
      "task_type": "BMC", "execution_mode": "BMC_URL",
      "url": "https://{oob_ip}/UI/Static/#/navigate/system/storage",
      "timeout_seconds": 300
    }
  }
}
```

## SSH_CMD 下发 JSON 最小示例

```json
{
  "command_id": "cmd-002",
  "command_type": "ASSIGN_JOB",
  "external_task_id": "server-task-456",
  "callback": {"status_url": "http://server/api/tasks/server-task-456/status", "auth_token": "tok"},
  "job": {
    "job_id": "job-002",
    "resource_lock": {"lock_uri": "ssh://10.0.1.1"},
    "device_snapshot": {
      "device_name": "Switch-A", "device_group": "A3",
      "oob_ip": "", "oob_username": "", "oob_password_ref": "",
      "inband_ip": "10.0.1.1",
      "inband_username": "root",
      "inband_password_ref": "env:SSH_PASSWORD"
    },
    "task_snapshot": {
      "task_name": "Show Version",
      "task_type": "SSH", "execution_mode": "SSH_CMD",
      "ssh_cmd": "show version",
      "timeout_seconds": 60
    }
  }
}
```

## secret_ref 使用方法

设置环境变量后，在 device_snapshot 中使用 `env:` 前缀引用：

```bash
export BMC_PASSWORD="your-bmc-password"
export SSH_PASSWORD="your-ssh-password"
```

```json
{
  "oob_password_ref": "env:BMC_PASSWORD",
  "inband_password_ref": "env:SSH_PASSWORD"
}
```

- 空 password_ref → 空密码（用于不使用的协议）
- `env:VAR_NAME` → 读取 `$VAR_NAME`
- 纯字符串（无前缀）→ 作为占位符直接返回（v0.1 兼容模式）

## callback payload 中 artifacts metadata

真实执行后，artifacts 仅传 metadata（不上传文件）：

```json
{
  "artifacts": [
    {"artifact_type": "PNG_SCREENSHOT", "relative_path": "output/Switch-A/Task/screenshot.png", "filename": "screenshot.png"},
    {"artifact_type": "HTML_PAGE", "relative_path": "output/Switch-A/Task/page.html", "filename": "page.html"}
  ]
}
```

## 后续接真实执行器

RealRunnerAdapter 已可用。连接真实设备只需：
1. 设置环境变量 `BMC_PASSWORD` / `SSH_PASSWORD`。
2. 启动时使用 `--runner real --callback-transport http`。
3. 向 `POST /executor/v1/jobs` 下发任务（使用 `env:VAR_NAME` secret_ref）。
4. 真实文件上传（artifact upload）将在后续版本实现。

FINAL_OUTPUT_END
