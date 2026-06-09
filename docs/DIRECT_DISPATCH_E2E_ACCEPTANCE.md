FINAL_OUTPUT_BEGIN

# Direct Dispatch Callback — 端到端验收指南

## 当前能力边界

| 能力 | 状态 |
|---|---|
| POST /executor/v1/jobs 接收任务 | 完成 |
| FakeRunner 模拟执行 | 完成 |
| RealRunnerAdapter 真实 BMC_URL / SSH_CMD | 完成 |
| HttpCallbackTransport 真实 POST callback.status_url | 完成 |
| secret_ref env:ENV_NAME 环境变量密码 | 完成 |
| lock_uri 本地资源锁防并发 | 完成 |
| mock callback server | 完成 |
| submit job CLI | 完成 |
| BMC_ACTIONS / CUSTOM_SCRIPT | 未支持 |

## Fake runner E2E 验收（不需要真实设备）

### 终端 1：启动 Mock Callback Server

```bash
python3 scripts/mock_callback_server.py --host 127.0.0.1 --port 18080
```

输出：
```
Mock Callback Server starting on http://127.0.0.1:18080
  POST /api/tasks/{external_task_id}/status  — receive callback
  GET  /callbacks                              — list received callbacks
```

### 终端 2：启动 Executor API（fake runner）

```bash
python3 scripts/start_executor_api_server.py \
  --host 127.0.0.1 --port 18000 \
  --runner fake \
  --callback-transport http \
  --executor-id exec-test-001
```

### 终端 3：下发任务

```bash
python3 scripts/submit_direct_job.py \
  --type SSH_CMD \
  --external-task-id t-e2e-001 \
  --job-id j-e2e-001 \
  --callback-url http://127.0.0.1:18080/api/tasks/t-e2e-001/status \
  --inband-ip 127.0.0.1 \
  --inband-username root \
  --inband-password-ref env:SSH_PASS \
  --ssh-cmd "echo ok"
```

### 验证回调

```bash
curl -s http://127.0.0.1:18080/callbacks | python3 -m json.tool
```

预期看到 RUNNING 和 SUCCEEDED 两次回调。

## Real BMC_URL 验收模板（需要真实 BMC 设备）

```bash
# 终端 1：Mock callback server
python3 scripts/mock_callback_server.py --host 0.0.0.0 --port 18080

# 终端 2：Executor with real runner
export BMC_PASS="真实BMC密码"
python3 scripts/start_executor_api_server.py \
  --host 0.0.0.0 --port 18000 \
  --runner real \
  --callback-transport http \
  --executor-id exec-win-001 \
  --output ./output_api_direct

# 终端 3：Submit BMC_URL job
python3 scripts/submit_direct_job.py \
  --type BMC_URL \
  --external-task-id t-bmc-001 \
  --job-id j-bmc-001 \
  --callback-url http://127.0.0.1:18080/api/tasks/t-bmc-001/status \
  --oob-ip 10.x.x.x \
  --oob-username Administrator \
  --oob-password-ref env:BMC_PASS \
  --url "https://10.x.x.x/UI/Static/#/navigate/home" \
  --timeout 120

# 验证
curl -s http://127.0.0.1:18080/callbacks | python3 -m json.tool
```

## Real SSH_CMD 验收模板（需要真实 SSH 设备）

```bash
export SSH_PASS="真实SSH密码"

python3 scripts/submit_direct_job.py \
  --type SSH_CMD \
  --external-task-id t-ssh-001 \
  --job-id j-ssh-001 \
  --callback-url http://127.0.0.1:18080/api/tasks/t-ssh-001/status \
  --inband-ip 10.x.x.x \
  --inband-username root \
  --inband-password-ref env:SSH_PASS \
  --ssh-cmd "uname -a" \
  --timeout 60
```

## Callback Payload 示例

### RUNNING
```json
{
  "external_task_id": "t-e2e-001",
  "job_id": "j-e2e-001",
  "executor_id": "exec-test-001",
  "status": "RUNNING",
  "reported_at": "2026-06-08T10:00:13Z"
}
```

### SUCCEEDED
```json
{
  "external_task_id": "t-e2e-001",
  "job_id": "j-e2e-001",
  "executor_id": "exec-test-001",
  "status": "SUCCEEDED",
  "reported_at": "2026-06-08T10:02:30Z",
  "duration_ms": 137000,
  "result": {"summary": "EXEC_SUCCEEDED", "steps_total": 1, "steps_success": 1, "steps_failed": 0},
  "error": null,
  "artifacts": [{"artifact_type": "PNG_SCREENSHOT", "filename": "screenshot.png", "relative_path": "..."}]
}
```

### FAILED
```json
{
  "external_task_id": "t-ssh-001",
  "job_id": "j-ssh-001",
  "executor_id": "exec-test-001",
  "status": "FAILED",
  "reported_at": "2026-06-08T10:01:30Z",
  "duration_ms": 15000,
  "result": null,
  "error": {"code": "SSH_AUTH_FAILED", "message": "Authentication failed", "retryable": false, "category": "SSH"},
  "artifacts": []
}
```

## 常见错误排查

### callback server 不通
```
Connection error: [Errno 61] Connection refused
Is the executor API server running at http://127.0.0.1:18000?
```
→ 确认终端 2 已启动。

### env secret 未设置
```
Error: Environment variable 'SSH_PASS' is not set
```
→ 执行 `export SSH_PASS="your-password"`。

### lock_uri 冲突
```
MISSING_LOCK_URI: Cannot derive lock_uri from job payload
```
→ 提供 `resource_lock.lock_uri` 或确保 `oob_ip`/`inband_ip` 已填写。

### callback 返回非 2xx
→ Job 状态变为 CALLBACK_FAILED。确认 callback URL 正确，mock server 在运行。

### runner 未显式设置 real
→ 默认 runner=fake。使用 `--runner real` 显式启用真实执行。

## 后续建议

1. 真实 artifact 文件上传到服务端。
2. BMC_ACTIONS / CUSTOM_SCRIPT 支持。
3. 多执行端调度接入。
4. heartbeat + jobs:poll 拉取模式。

FINAL_OUTPUT_END
