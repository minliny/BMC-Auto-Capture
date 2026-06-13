# Plan + Run Dispatch API

## 当前推荐协议

Executor API 以“latest Excel config + planId/runId”为核心：

```
POST /executor/v1/config/excel
POST /executor/v1/plans
POST /executor/v1/plans/{plan_id}:run
GET  /executor/v1/plans/{plan_id}
GET  /executor/v1/plans/{plan_id}/items
GET  /executor/v1/runs/{run_id}
GET  /executor/v1/runs/{run_id}/items
POST /executor/v1/plans/{plan_id}/callbacks:retry
```

`planId` 是业务 plan 标识；`runId` 是一次执行的唯一标识。两者都可查询，同一 plan 多次执行时 `/plans/{plan_id}` 返回该 plan 最新一次 run。

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
  "runId": "plan-1-run-...",
  "status": "ACCEPTED",
  "callbackTransportMode": "http"
}
```

`runner=real` 只有在服务端通过 `--enable-real-runner` 或 `EXECUTOR_ENABLE_REAL_RUNNER=1` 开启后才会接受。

## 查询

```bash
curl "http://127.0.0.1:8080/executor/v1/plans/1?excelHash=<sha256>"
curl "http://127.0.0.1:8080/executor/v1/plans/1/items?excelHash=<sha256>"
curl "http://127.0.0.1:8080/executor/v1/runs/<runId>"
curl "http://127.0.0.1:8080/executor/v1/runs/<runId>/items"
```

Plan summary：

```json
{
  "planId": "1",
  "runId": "plan-1-run-...",
  "status": "COMPLETED",
  "summary": {"total": 20, "success": 20, "failed": 0, "in_progress": 0, "pending": 0},
  "excelHash": "<sha256>",
  "startedAt": "2026-06-13T00:00:00+00:00",
  "finishedAt": "2026-06-13T00:00:03+00:00",
  "errorMessage": null,
  "infoEvents": []
}
```

Items response adds `items[]` with `deviceName`, `taskName`, `status`, `errorMessage`, `startedAt`, `finishedAt`, and `infoEvents`.

## Callback Retry

Callback failures are persisted to outbox. Retry uses a currently supplied or resolved callback URL; the outbox does not persist plaintext callback secrets.

```bash
curl -X POST http://127.0.0.1:8080/executor/v1/plans/1/callbacks:retry \
  -H "Content-Type: application/json" \
  -d '{"callbackUrl":"http://callback.example/items","mode":"batch"}'
```

## Legacy RunDispatchService

`/executor/v1/plans:import` and `/executor/v1/runs` belong to the older isolated `RunDispatchService` path. They are only available when that legacy service is explicitly registered and are not the default `run.py --server` integration path.
