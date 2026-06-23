FINAL_OUTPUT_BEGIN

# Plan Catalog Design — Deterministic Planner

## 为什么需要共享 Excel + validation.json

服务端和执行端导入同一份 Excel + validation.json，用同一套确定性 planner 生成完全一样的 task_id 和 plan_hash。此后服务端只需下发 `run_id / plan_id / plan_hash / task_id`，执行端即可根据本地 TaskCatalog 找到完整任务信息并执行。

## task_id 生成规则

基于 SHA256(key_fields) 取前 16 位 hex：

```
planner_version | excel_sha256 | validation_json_sha256 | device_group | device_key | task_no | task_name | task_type | execution_mode | source_row_ref
```

- 确定性：同一输入永远同一 task_id
- 无 UUID
- 16 字符 hex 字符串

## plan_hash 规则

规范化 PlanManifest 的 tasks[]（只含 task_id/task_no/task_type/execution_mode/device_key/device_group/lock_uri/enabled/source_row_ref），JSON sort_keys 后 SHA256 前 16 位。

- 不受 generated_at 影响
- 不受文件路径影响
- server 和 executor 同输入同 hash

## PlanManifest 示例

```json
{
  "plan_id": "cd1afada205368a3",
  "plan_hash": "1f6adeedeb5170d5",
  "planner_version": "0.1.0",
  "excel_sha256": "abc...",
  "validation_json_sha256": "def...",
  "generated_at": "2026-06-08T10:00:00Z",
  "task_count": 16,
  "tasks": [
    {
      "task_id": "a1b2c3d4e5f6a7b8",
      "task_no": "1",
      "task_name": "BMC 登录检查",
      "task_type": "BMC",
      "execution_mode": "BMC_URL",
      "device_group": "A3",
      "device_key": "250cf9bfec0d89ec",
      "lock_uri": "bmc://10.0.0.1",
      "enabled": true,
      "source_row_ref": "excel:Sheet=device=Switch-A:task=BMC 登录检查"
    }
  ]
}
```

## TaskCatalog 示例

```json
{
  "a1b2c3d4e5f6a7b8": {
    "task_id": "a1b2c3d4e5f6a7b8",
    "plan_id": "cd1afada205368a3",
    "device_snapshot": {"device_name": "Switch-A", "oob_ip": "10.0.0.1", "oob_password_ref": "secret://bmc/Switch-A", ...},
    "task_snapshot": {"task_name": "BMC 登录检查", "url": "https://{oob_ip}/", "timeout_seconds": 60, ...},
    "resource_lock": {"lock_uri": "bmc://10.0.0.1", "lock_exclusive": true, "lock_type": "BMC"},
    "output": {"output_dir_template": "{任务序号}_{任务名称}/{设备分类}"},
    "source_row_ref": "excel:Sheet=device=Switch-A:task=BMC 登录检查"
  }
}
```

## network_tests 示例

validation.json:
```json
{
  "network_tests": [
    {
      "network_test_id": "net-ping-default",
      "name": "ping 网关测试",
      "device_groups": ["A3"],
      "execution_mode": "SSH_CMD",
      "command": "ping -c 4 {inband_ip}",
      "timeout_seconds": 30
    }
  ]
}
```

生成 task_type=NETWORK_TEST 的 PlannedTask，lock_uri=ssh://{inband_ip}。

## 后续 POST /executor/v1/runs 如何使用

P1-PLAN-CATALOG-002 将实现：

```json
POST /executor/v1/runs
{
  "run_id": "run-001",
  "plan_id": "cd1afada205368a3",
  "plan_hash": "1f6adeedeb5170d5",
  "task_ids": ["a1b2c3d4e5f6a7b8", "b2c3d4e5f6a7b8c9"]
}
```

执行端收到后：
1. 校验 plan_hash 匹配本地
2. 从 TaskCatalog 按 task_id 查出完整任务
3. 按 lock_uri 串行调度
4. 执行完成后回调 callback.status_url

FINAL_OUTPUT_END
