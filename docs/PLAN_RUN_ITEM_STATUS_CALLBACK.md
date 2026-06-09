FINAL_OUTPUT_BEGIN

# Plan Run Item Status Callback

## 调用流程

```
1. POST /executor/v1/config/excel:path → 设置最新 Excel
2. POST /executor/v1/plans/{planId}:run    → 启动全量执行
3. 每 pair 完成 → POST itemStatusUrl       → 状态回调
4. GET /executor/v1/plans/{planId}/runs/{runId} → 查询进度
```

## 上传 Excel / 设置 latest Excel

```bash
curl -X POST http://127.0.0.1:8765/executor/v1/config/excel:path \
  -H "Content-Type: application/json" \
  -d '{"excelPath": "C:\\path\\to\\config.xlsx"}'
```

响应：
```json
{"accepted":true,"configVersion":"excel-20260609-152009","deviceCount":10,"enabledDeviceCount":10,"taskCount":29,"enabledTaskCount":29}
```

## 启动 planId 全量执行

```bash
curl -X POST http://127.0.0.1:8765/executor/v1/plans/1:run \
  -H "Content-Type: application/json" \
  -d '{"callback":{"itemStatusUrl":"http://server/api/plans/items/status"},"updater":"downstream-system","runner":"fake"}'
```

## 每任务状态回调 payload

```json
{
  "planId": 1,
  "deviceName": "Switch-A",
  "taskName": "BMC 登录检查",
  "status": "SUCCESS",
  "updater": "downstream-system",
  "errorMessage": null
}
```

严格 6 字段，不包含 job_id/external_task_id/executor_id/duration_ms/artifacts。

## fake E2E 验收命令

```bash
# Terminal 1: Mock plan status server
python3 scripts/mock_plan_status_server.py --port 18080

# Terminal 2: Executor API server
python3 scripts/start_executor_api_server.py --port 8765 --runner fake --callback-transport http

# Terminal 3: Set Excel + submit plan run
python3 -c "
import urllib.request, json
# Set latest Excel
urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:8765/executor/v1/config/excel:path',
    data=json.dumps({'excelPath':'examples/task_template.xlsx'}).encode(),
    headers={'Content-Type':'application/json'}, method='POST'
))
# Start plan run
urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:8765/executor/v1/plans/1:run',
    data=json.dumps({'callback':{'itemStatusUrl':'http://127.0.0.1:18080/api/plans/items/status'}}).encode(),
    headers={'Content-Type':'application/json'}, method='POST'
))
"

# Verify
curl http://127.0.0.1:18080/plan-item-statuses
```

## 当前不做的能力

- real runner（仅 fake runner）
- artifact 上传
- 并发执行（串行）
- jobs:poll / WebSocket
- plan_catalog 复杂规划

## 后续 real runner 接入

PlanRunService 的 `_execute_run` 中替换 `time.sleep(0.001)` 为 `RealRunnerAdapter.run_job()` 调用。

FINAL_OUTPUT_END
