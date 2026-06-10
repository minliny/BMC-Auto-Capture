FINAL_OUTPUT_BEGIN

# Plan + Run Dispatch API

## 工作流

### 推荐服务端协议（基于 excelHash + planId）

```
1. POST /executor/v1/config/excel          → 上传 Excel，得到 excelHash
2. POST /executor/v1/plans with excelHash  → 启动任务批次，得到 planId
3. GET  /executor/v1/plans/{planId}?excelHash=...  → 查询批次汇总
4. GET  /executor/v1/plans/{planId}/items?excelHash=...  → 查询任务明细
```

服务端只需 `excelHash + planId`，无需理解 `runId/jobId` 等内部概念。

### 旧兼容协议（保留，新接入不推荐）

```
服务端 + 执行端：共享同一份 Excel + validation.json
                    ↓
        双方各自运行 PlanCatalogPlanner
                    ↓
        生成相同的 plan_id / plan_hash / TaskCatalog
                    ↓
服务端 → POST /executor/v1/plans:import   (执行端导入)
服务端 → POST /executor/v1/runs           (只发 run_id/plan_id/plan_hash)
                    ↓
        执行端从本地 TaskCatalog 查 task_id → 执行
                    ↓
执行端 → POST callback.task_status_url    (每 task 状态变更)
```

## 外部 Plan API（excelHash + planId）

### POST /executor/v1/plans — 启动外部 Plan

请求：
```json
{
  "excelHash": "d62eaec1deb688f6066e8a4814cbe7a24f57f794b389c1490af6019732399717",
  "callback": {"itemStatusUrl": "http://server/item-status-callback"},
  "runner": "fake",
  "updater": "downstream-system"
}
```

成功响应：
```json
{
  "accepted": true,
  "excelHash": "d62eaec1deb688f6066e8a4814cbe7a24f57f794b389c1490af6019732399717",
  "planId": "plan-d62eaec1-000001",
  "filename": "_test_one_per_group.xlsx",
  "status": "ACCEPTED"
}
```

错误响应：
- `MISSING_EXCEL_HASH` (400)
- `EXCEL_HASH_MISMATCH` (400) — 请求的 excelHash 与 latest Excel 不一致
- `NO_LATEST_EXCEL_CONFIG` (400)

### GET /executor/v1/plans/{planId} — 查询 Plan 汇总

请求需带 query param `excelHash`。

成功：
```json
{
  "excelHash": "d62eaec1deb688f6066e8a4814cbe7a24f57f794b389c1490af6019732399717",
  "planId": "plan-d62eaec1-000001",
  "filename": "_test_one_per_group.xlsx",
  "status": "COMPLETED",
  "summary": {"total": 28, "success": 28, "failed": 0, "running": 0, "pending": 0},
  "startedAt": "2026-06-10T10:00:00",
  "finishedAt": "2026-06-10T10:00:03",
  "errorMessage": null
}
```

错误：
- `MISSING_EXCEL_HASH` (400)
- `PLAN_NOT_FOUND` (404)
- `PLAN_EXCEL_HASH_MISMATCH` (400)

### GET /executor/v1/plans/{planId}/items — 查询 Plan 明细

请求需带 query param `excelHash`。

成功：
```json
{
  "excelHash": "d62eaec1deb688f6066e8a4814cbe7a24f57f794b389c1490af6019732399717",
  "planId": "plan-d62eaec1-000001",
  "filename": "_test_one_per_group.xlsx",
  "status": "COMPLETED",
  "summary": {"total": 28, "success": 28, "failed": 0, "running": 0, "pending": 0},
  "items": [
    {"deviceName": "A3-01", "taskName": "4.1.15 计算节点光模块信息查询测试", "status": "SUCCESS", "errorMessage": null}
  ]
}
```

## 旧兼容接口

## /plans:import

```bash
curl -X POST http://127.0.0.1:18000/executor/v1/plans:import \
  -H "Content-Type: application/json" \
  -d '{"excel_path":"input.xlsx","validation_json_path":"validation.json"}'
```

响应：
```json
{"accepted":true,"plan_id":"cd1afada205368a3","plan_hash":"1f6adeedeb5170d5","task_count":16}
```

## /runs 全量下发

```bash
curl -X POST http://127.0.0.1:18000/executor/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"command_id":"cmd-001","run_id":"run-001","plan_id":"cd1afada205368a3","plan_hash":"1f6adeedeb5170d5","scope":"ALL","callback":{"task_status_url":"http://server/api/tasks/{task_id}/status"}}'
```

服务端只发 run_id/plan_id/plan_hash，**不发送完整任务信息**。

## task_id 查询

```bash
curl http://127.0.0.1:18000/executor/v1/runs/run-001
curl http://127.0.0.1:18000/executor/v1/runs/run-001/tasks
curl http://127.0.0.1:18000/executor/v1/runs/run-001/tasks/{task_id}
```

## callback task_status_url 示例

```json
{
  "run_id": "run-001",
  "plan_id": "cd1afada205368a3",
  "task_id": "a1b2c3d4e5f6a7b8",
  "external_task_id": "a1b2c3d4e5f6a7b8",
  "executor_id": "exec-win-001",
  "status": "SUCCEEDED",
  "duration_ms": 100,
  "result": {"summary": "EXEC_SUCCEEDED"},
  "error": null,
  "artifacts": []
}
```

## 状态枚举分层

| 上下文 | 字段 | 允许值 |
|--------|------|--------|
| Plan Run 级别 | `status` | `ACCEPTED` / `RUNNING` / `COMPLETED` / `FAILED` |
| Run Item 查询 | `items[].status` | `PENDING` / `RUNNING` / `SUCCESS` / `FAILED` |
| Item 状态回调 | `status` | `SUCCESS` / `FAILED` |
| Debug Callback | `payload.status` | `SUCCESS` / `FAILED` |

说明：
- `COMPLETED` 不代表全部成功，是否全成功需检查 `summary.failed`
- `items[].status` 包含中间态（PENDING/RUNNING），查询时可处于任意阶段
- Item 回调（callback）和 debug callback 仅上报终态 SUCCESS / FAILED

## NETWORK_TEST

validation.json 中定义的 network_tests 被 Planner 自动展开为 task_type=NETWORK_TEST 的 PlannedTask，与其他任务一起进入 TaskCatalog。借助 run 全量下发后可执行（FakeRunner 模拟，或 RealRunnerAdapter 以 SSH_CMD 方式执行）。

## 当前不做的能力

- artifact 文件上传
- jobs:poll 拉取
- 多执行端调度
- run 级别 callback（仅 task 级别）
- scope=SELECTIVE 等子集下发

FINAL_OUTPUT_END
