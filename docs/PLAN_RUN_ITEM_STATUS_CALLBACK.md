# Plan Run Item Status Callback

## 推荐流程

```
1. POST /executor/v1/config/excel          -> 上传 Excel，得到 excelHash
2. POST /executor/v1/plans                 -> 使用 excelHash + callback.planId 启动外部 plan
3. POST callback.itemStatusUrl             -> executor 在 item 状态变化时立即发送状态回调
4. GET  /executor/v1/plans/{planId}?excelHash=...
5. GET  /executor/v1/plans/{planId}/items?excelHash=...
```

兼容入口仍可使用：

```
POST /executor/v1/config/excel:path
POST /executor/v1/plans/{plan_id}:run
```

## 启动 Executor API

默认绑定 loopback，端口 8080：

```powershell
.\runtime\bmc-engine.exe --server --host 127.0.0.1 --port 8080 --runner fake --enable-debug-callback-receiver
```

真实 BMC/SSH 执行需要服务端显式开闸：

```powershell
.\runtime\bmc-engine.exe --server --runner real --enable-real-runner
```

仅在受控网络和受信任 Excel/tasks.json 配置下使用 real runner。

## Config 响应

`/executor/v1/config/excel` 和 `/executor/v1/config/excel:path` 的响应不会暴露 executor 本地 `storedPath`。

```json
{
  "accepted": true,
  "deviceCount": 10,
  "enabledDeviceCount": 10,
  "taskCount": 29,
  "enabledTaskCount": 29,
  "filename": "config.xlsx",
  "excelHash": "<sha256>",
  "sha256": "<sha256>",
  "message": "excel config uploaded and accepted as latest"
}
```

## Callback Payload

Batch mode payload：

```json
{
  "planId": "1",
  "items": [
    {
      "planId": "1",
      "taskId": "task.bmc.login",
      "planItemId": "1:Switch-A:task.bmc.login",
      "deviceGroup": "A3",
      "deviceName": "Switch-A",
      "taskName": "BMC login check",
      "status": "SUCCESS",
      "updater": "downstream-system",
      "errorMessage": null,
      "startedAt": "2026-06-13T00:00:00+00:00",
      "finishedAt": "2026-06-13T00:00:01+00:00"
    }
  ]
}
```

Single mode sends the same allowed item fields directly. Per-item payloads never include `excelHash`, `storedPath`, password, token, `runId`, job id, artifacts, executor id, or output directory.

Plan 完成后发送批次总结 payload：

```json
{
  "planId": "1",
  "summary": {
    "total": 2,
    "success": 2,
    "failed": 0,
    "in_progress": 0,
    "pending": 0,
    "failureSummary": [],
    "outputRoot": "executor_state/outputs/1/20260613T000000Z"
  }
}
```

`outputRoot` 只出现在批次总结中。任务状态更新包只表达任务状态，不携带单任务输出目录。

## Callback URL Policy

Callback URL must be `http` or `https`, must not contain URL userinfo, and private/link-local literal IPs are rejected unless explicitly allow-listed:

```bash
EXECUTOR_CALLBACK_ALLOWED_HOSTS=10.0.99.1,callback.internal
```

Loopback is allowed for local debug receiver.

## Query And Retry

```bash
curl http://127.0.0.1:8080/executor/v1/plans/1
curl http://127.0.0.1:8080/executor/v1/plans/1/items

curl -X POST http://127.0.0.1:8080/executor/v1/plans/1/callbacks:retry \
  -H "Content-Type: application/json" \
  -d '{"callbackUrl":"http://127.0.0.1:8080/debug/plan-item-statuses","mode":"batch"}'
```

Plan/item query state is persisted under executor state storage and restored for querying after process restart. Interrupted in-flight plans are restored as interrupted query records; execution is not resumed automatically.

调度端主链路按 `planId` 查询和归档；runId 查询别名不属于当前 Executor API HTTP 面。
