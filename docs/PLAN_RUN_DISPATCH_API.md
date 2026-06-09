FINAL_OUTPUT_BEGIN

# Plan + Run Dispatch API

## 工作流

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

## /plans:import

```bash
curl -X POST http://127.0.0.1:8765/executor/v1/plans:import \
  -H "Content-Type: application/json" \
  -d '{"excel_path":"input.xlsx","validation_json_path":"validation.json"}'
```

响应：
```json
{"accepted":true,"plan_id":"cd1afada205368a3","plan_hash":"1f6adeedeb5170d5","task_count":16}
```

## /runs 全量下发

```bash
curl -X POST http://127.0.0.1:8765/executor/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"command_id":"cmd-001","run_id":"run-001","plan_id":"cd1afada205368a3","plan_hash":"1f6adeedeb5170d5","scope":"ALL","callback":{"task_status_url":"http://server/api/tasks/{task_id}/status"}}'
```

服务端只发 run_id/plan_id/plan_hash，**不发送完整任务信息**。

## task_id 查询

```bash
curl http://127.0.0.1:8765/executor/v1/runs/run-001
curl http://127.0.0.1:8765/executor/v1/runs/run-001/tasks
curl http://127.0.0.1:8765/executor/v1/runs/run-001/tasks/{task_id}
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

## NETWORK_TEST

validation.json 中定义的 network_tests 被 Planner 自动展开为 task_type=NETWORK_TEST 的 PlannedTask，与其他任务一起进入 TaskCatalog。借助 run 全量下发后可执行（FakeRunner 模拟，或 RealRunnerAdapter 以 SSH_CMD 方式执行）。

## 当前不做的能力

- artifact 文件上传
- jobs:poll 拉取
- 多执行端调度
- run 级别 callback（仅 task 级别）
- scope=SELECTIVE 等子集下发

FINAL_OUTPUT_END
