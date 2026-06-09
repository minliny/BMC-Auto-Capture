# Plan Run Item Status Callback

## 调用流程

```
1. POST /executor/v1/config/excel:path → 设置最新 Excel
2. POST /executor/v1/plans/{planId}:run    → 启动全量执行
3. 每 pair 完成 → POST itemStatusUrl       → 状态回调
4. GET /executor/v1/plans/{planId}/runs/{runId} → 查询进度
```

## 客户执行端开箱即用（推荐）

客户执行端**不需要安装 Python**。`python scripts/*.py` 只是开发态命令。
正式执行端使用 `runtime\bmc-engine.exe` 或 `启动.bat`。

### 方式 1：使用内置 debug callback receiver（推荐）

Executor API 内置调试回调接收器，不需要额外启动 Python mock 服务器：

```powershell
# Terminal: 启动 Executor API，自带 debug callback receiver
.\runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --runner fake --enable-debug-callback-receiver
```

然后将 `itemStatusUrl` 指向本机 Executor API 的 `/debug/plan-item-statuses`：

```bash
curl -X POST http://127.0.0.1:18000/executor/v1/config/excel:path \
  -H "Content-Type: application/json" \
  -d '{"excelPath": "C:\\path\\to\\config.xlsx"}'

curl -X POST http://127.0.0.1:18000/executor/v1/plans/1:run \
  -H "Content-Type: application/json" \
  -d '{"callback":{"itemStatusUrl":"http://127.0.0.1:18000/debug/plan-item-statuses"},"updater":"downstream-system","runner":"fake"}'

# 查询收到的回调
curl http://127.0.0.1:18000/debug/plan-item-statuses
```

内置 debug callback receiver 提供三个路由：

| 方法 | 路由 | 描述 |
|------|------|------|
| POST | `/debug/plan-item-statuses` | 接收 plan item 状态回调 |
| GET | `/debug/plan-item-statuses` | 查询已收到的所有回调 |
| DELETE | `/debug/plan-item-statuses` | 清空已收到的回调 |

### 方式 2：旧版 Python mock 服务器（仅开发态，已废弃）

```powershell
python scripts/mock_plan_status_server.py --port 18080
```

## 启动方式选择

| 启动方式 | 命令 | 需要 Python | 适用场景 |
|----------|------|------------|----------|
| **Recommended** | `.\runtime\bmc-engine.exe --server ...` | 否 | 客户执行端 |
| **Recommended** | `启动.bat --server` | 否 | 客户执行端 |
| 开发调试 | `python run.py --server ...` | 是 | 开发者 |
| 已废弃 | `python scripts/start_executor_api_server.py` | 是 | 兼容旧脚本 |

## 上传 Excel / 设置 latest Excel

```bash
curl -X POST http://127.0.0.1:18000/executor/v1/config/excel:path \
  -H "Content-Type: application/json" \
  -d '{"excelPath": "C:\\path\\to\\config.xlsx"}'
```

响应：
```json
{"accepted":true,"configVersion":"excel-20260609-152009","deviceCount":10,"enabledDeviceCount":10,"taskCount":29,"enabledTaskCount":29}
```

## 启动 planId 全量执行

```bash
curl -X POST http://127.0.0.1:18000/executor/v1/plans/1:run \
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

### 使用内置 debug callback receiver（推荐）

```powershell
# Terminal 1: Executor API server with debug callback receiver
.\runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --runner fake --callback-transport fake --enable-debug-callback-receiver

# Terminal 2: Set Excel + submit plan run + verify
python3 -c "
import urllib.request, json
# Set latest Excel
urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:18000/executor/v1/config/excel:path',
    data=json.dumps({'excelPath':'examples/task_template.xlsx'}).encode(),
    headers={'Content-Type':'application/json'}, method='POST'
))
# Start plan run — use built-in debug callback URL
urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:18000/executor/v1/plans/1:run',
    data=json.dumps({'callback':{'itemStatusUrl':'http://127.0.0.1:18000/debug/plan-item-statuses'}}).encode(),
    headers={'Content-Type':'application/json'}, method='POST'
))
"
# Verify
curl http://127.0.0.1:18000/debug/plan-item-statuses
```

### 使用旧版 Python mock server（仅开发态）

```bash
# Terminal 1: Mock plan status server
python3 scripts/mock_plan_status_server.py --port 18080

# Terminal 2: Executor API server
.\runtime\bmc-engine.exe --server --host 0.0.0.0 --port 18000 --runner fake --callback-transport fake

# Terminal 3: Set Excel + submit plan run
curl -X POST http://127.0.0.1:18000/executor/v1/config/excel:path \
  -H "Content-Type: application/json" \
  -d '{"excelPath":"examples/task_template.xlsx"}'

curl -X POST http://127.0.0.1:18000/executor/v1/plans/1:run \
  -H "Content-Type: application/json" \
  -d '{"callback":{"itemStatusUrl":"http://127.0.0.1:18080/api/plans/items/status"}}'

# Verify
curl http://127.0.0.1:18080/plan-item-statuses
```

## 开箱即用验证脚本

```powershell
.\scripts\smoke_executor_runtime.ps1 -ExcelPath "C:\path\to\_test_one_per_group.xlsx"
```

该脚本使用 `runtime\bmc-engine.exe`，不调用 `python`。验证流程：
1. 启动 Executor API（带 debug callback receiver）
2. 检查 `/executor/v1/status`
3. 检查 openapi routes
4. 设置 latest Excel
5. 下发 PlanId=1 fake run
6. 查询 run status 和 callback received
7. 验证 total/success/failed/callback count

## 当前不做的能力

- real runner（仅 fake runner）
- artifact 上传
- 并发执行（串行）
- jobs:poll / WebSocket
- plan_catalog 复杂规划

## 后续 real runner 接入

PlanRunService 的 `_execute_run` 中替换 `time.sleep(0.001)` 为 `RealRunnerAdapter.run_job()` 调用。