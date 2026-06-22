# Plan + Run Dispatch API

## 当前推荐协议

Executor API 以“latest Excel config + planId”为核心：

```
POST /executor/v1/config/excel
POST /executor/v1/plans
POST /executor/v1/plans/{plan_id}:run
GET  /executor/v1/plans/{plan_id}
GET  /executor/v1/plans/{plan_id}/items
POST /executor/v1/plans/{plan_id}/callbacks:retry
```

`planId` 是服务端下发的执行批次 ID，也是调度端和执行端共同使用的主键。`taskId` 是 Excel 任务定义 ID，`planItemId` 是批次内“一台设备 + 一个任务”的执行项 ID。执行端内部可保留调试用 run id，但公开主契约不要求服务端理解、保存或回传 `runId`。

## 上传 Excel

```bash
curl -X POST http://127.0.0.1:8080/executor/v1/config/excel \
  -F "file=@config.xlsx"
```

成功响应不包含本地 `storedPath`：

```json
{
  "accepted": true,
  "excelHash": "<sha256>",
  "sha256": "<sha256>",
  "filename": "config.xlsx",
  "deviceCount": 4,
  "enabledDeviceCount": 4,
  "taskCount": 20,
  "enabledTaskCount": 20,
  "message": "excel config uploaded and accepted as latest"
}
```

## 启动外部 Plan

```bash
curl -X POST http://127.0.0.1:8080/executor/v1/plans \
  -H "Content-Type: application/json" \
  -d '{
    "excelHash":"<sha256>",
    "callback":{"planId":"1","itemStatusUrl":"http://callback.example/items","mode":"batch"},
    "runner":"fake",
    "updater":"downstream-system"
  }'
```

成功响应：

```json
{
  "accepted": true,
  "excelHash": "<sha256>",
  "planId": "1",
  "status": "ACCEPTED",
  "callbackTransportMode": "http"
}
```

`runner=real` 只有在服务端通过 `--enable-real-runner` 或 `EXECUTOR_ENABLE_REAL_RUNNER=1` 开启后才会接受。

## 查询

```bash
curl "http://127.0.0.1:8080/executor/v1/plans/1?excelHash=<sha256>"
curl "http://127.0.0.1:8080/executor/v1/plans/1/items?excelHash=<sha256>"
```

Plan summary：

```json
{
  "planId": "1",
  "status": "COMPLETED",
  "summary": {
    "total": 20,
    "success": 20,
    "failed": 0,
    "in_progress": 0,
    "pending": 0,
    "failureSummary": [],
    "outputRoot": "executor_state/outputs/1/20260613T000000Z"
  },
  "outputRoot": "executor_state/outputs/1/20260613T000000Z",
  "excelHash": "<sha256>",
  "startedAt": "2026-06-13T00:00:00+00:00",
  "finishedAt": "2026-06-13T00:00:03+00:00",
  "errorMessage": null,
  "infoEvents": []
}
```

Items response adds `items[]` with `taskId`, `planItemId`, `deviceGroup`, `deviceName`, `taskName`, `status`, `errorMessage`, `startedAt`, `finishedAt`, and `infoEvents`.

When a real execution result is available, each item may also include structured status fields:

```json
{
  "executionStatus": "EXEC_SUCCESS",
  "ruleStatus": "RULE_PASSED",
  "artifactStatus": "ARTIFACT_SAVED",
  "readyStatus": "READY_OK",
  "checkpointStatus": "CHECK_PASS",
  "finalVerdict": "PASS",
  "checkResults": [
    {
      "stage": "RESULT_CHECK",
      "checkId": "ssh.result_rules",
      "status": "PASS",
      "severity": "ERROR",
      "message": "SSH result rules passed",
      "details": {}
    }
  ]
}
```

`checkResults` 是统一检查输出；callback item payload 暂不包含该字段，避免外部回调契约扩散。

## Callback Retry

Callback failures are persisted to outbox. Retry uses a currently supplied or resolved callback URL; the outbox does not persist plaintext callback secrets.

```bash
curl -X POST http://127.0.0.1:8080/executor/v1/plans/1/callbacks:retry \
  -H "Content-Type: application/json" \
  -d '{"callbackUrl":"http://callback.example/items","mode":"batch"}'
```

## Plan Query

服务端主链路统一使用 `planId` 查询：

```bash
curl http://127.0.0.1:8080/executor/v1/plans/1
curl http://127.0.0.1:8080/executor/v1/plans/1/items
```
