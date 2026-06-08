# Resource-Aware Scheduling 修复设计

## 一、问题诊断

### 1.1 当前错误的调度模型

当前 `DynamicScheduler`（`src/scheduler/dynamic_scheduler.py:247`）的调度逻辑：

```
all_plans -> group_by(device_name)
每个 device_name 一个 FIFO queue
ready devices 轮询 -> 匹配 protocol -> ThreadPoolExecutor.submit()
```

**核心缺陷：**

| 问题 | 影响 |
|------|------|
| `endpoint_key` = `device_name`，而非实际连接 IP | 同一 BMC IP 被两台设备共用时，仍会并发争抢 |
| `max_bmc_workers` 控制的是"同时运行的 BMC plan 数" | 无法表达"同时运行的 BMC endpoint group 数" |
| TELNET 被错误地归入 SSH 协议池 | TELNET session 与 SSH session 资源互相干扰 |
| 无 endpoint 级别的资源等待/执行耗时统计 | 无法分析性能瓶颈 |
| 无 parallel_efficiency 指标 | 无法评估调度优化效果 |

### 1.2 问题根因

`WorkerPool` 只跟踪 `device_id` 级别的运行状态（`_running_devices: set[str]`），不理解"不同 device 可能共享同一物理 endpoint"的情况。同时 `max_workers` 的语义是"线程池最大线程数"，即"同时运行的最大 plan 数"，而非"同时运行的最大 endpoint group 数"。

---

## 二、调度模型重新设计

### 2.1 新调度模型

```
plans -> group_by(endpoint_key)
每个 endpoint_key 一个 FIFO queue（同 endpoint 内保持原始顺序串行）
ready endpoint groups 进入统一调度队列
worker pool 中的 worker 从 ready groups 中取一个 group，执行其队列头部的一个 plan
同 group 同时最多一个 plan running（endpoint 串行保证）
不同 group 可并发（只要 worker 有空闲）
```

### 2.2 endpoint_key 定义

```python
def endpoint_key(plan: TaskPlan) -> str:
    """返回唯一标识一个物理连接端点的 key。

    BMC  → bmc_ip（同一 BMC IP 不能同时跑多个 plan）
    SSH  → inband_ip（同一 SSH host 不能同时跑多个 session）
    TELNET → inband_ip（同 SSH，因为 TELNET 也是连到同一台设备的带内 IP）
    VRP  → inband_ip
    """
    protocol = plan.protocol  # "BMC" | "SSH" | "TELNET" | "VRP"
    if protocol == "BMC":
        return f"BMC:{plan.device.bmc_ip}"
    elif protocol in ("SSH", "TELNET", "VRP"):
        return f"INBAND:{plan.device.inband_ip}"
    return f"UNKNOWN:{plan.device.device_name}"
```

**选择理由：**
- 按实际网络端点（IP）而非逻辑设备名分组，是资源安全的正确做法
- 同一 BMC IP 被多台逻辑设备共用时不会并发抢占
- `BMC:` / `INBAND:` 前缀区分不同协议类型的资源池，防止跨协议阻塞

### 2.3 资源池设计选择

**选择：按 resource_type 分池（BMC 池 vs INBAND 池）**

理由：
1. BMC 连接使用浏览器（Playwright），消耗 GPU/内存，资源特征与 SSH/TELNET 完全不同
2. SSH/TELNET/VRP 都是基于 TCP socket 的命令行连接，资源消耗相似，可共用一个池
3. 分池避免 BMC 浏览器任务与轻量 CLI 任务互相抢占 worker
4. 用户可以通过 `max_bmc_workers` / `max_inband_workers` 独立控制两类资源的并发度

不选择统一池的理由：
- 如果 SSH 任务占满所有 worker，BMC 任务会饥饿
- 如果 BMC 任务占满所有 worker，SSH 任务的大量设备会排队，wall clock 严重膨胀

### 2.4 max_workers 新语义

| 配置项 | 旧语义（错误） | 新语义（正确） |
|--------|---------------|---------------|
| `max_bmc_workers` | 同时运行的 BMC plan 数 | 同时运行的 BMC endpoint group 数 |
| `max_ssh_workers` | 同时运行的 SSH plan 数 | 同时运行的 INBAND endpoint group 数（含 SSH/TELNET/VRP） |

建议重命名以消除歧义：
- `max_bmc_workers` → `max_bmc_endpoint_groups`
- `max_ssh_workers` → `max_inband_endpoint_groups`

### 2.5 调度伪代码

```python
class EndpointAwareScheduler:
    def run(self, plans: list[TaskPlan]) -> ExecutionReport:
        # 1. Group by endpoint_key
        groups: dict[str, deque[TaskPlan]] = group_by_endpoint_key(plans)
        
        # 2. Classify groups by resource_type
        bmc_groups = {k: v for k, v in groups.items() if k.startswith("BMC:")}
        inband_groups = {k: v for k, v in groups.items() if k.startswith("INBAND:")}
        
        # 3. Per-group running state
        running_groups: set[str] = set()
        
        # 4. Main loop
        while unfinished(groups, running_groups):
            # Dispatch BMC
            _dispatch_pool(bmc_groups, running_groups, max_bmc_workers, BMCExecutor)
            # Dispatch INBAND
            _dispatch_pool(inband_groups, running_groups, max_inband_workers, SSHExecutor)
            time.sleep(0.1)
        
        return build_report()
    
    def _dispatch_pool(self, groups, running, max_workers, executor_factory):
        available = max_workers - len(running ∩ groups.keys())
        if available <= 0:
            return
        
        ready = [k for k in groups if k not in running and groups[k]]
        for endpoint_key in ready[:available]:
            running.add(endpoint_key)
            plan = groups[endpoint_key].popleft()
            submit(plan, endpoint_key, on_done=lambda: self._on_plan_done(...))
    
    def _on_plan_done(self, plan, endpoint_key, result):
        running.discard(endpoint_key)
        record_timing(plan, endpoint_key, result)
        if groups[endpoint_key]:
            # Re-add to ready pool for next dispatch
            requeue(endpoint_key)
```

### 2.6 Worker 空闲调度

- 主循环每次迭代检查是否有空闲 worker 和 ready group
- 有空闲就立即 dispatch，不等待下一个周期
- 防止资源空转：任何时候只要有可用 worker 和 ready endpoint，就下发任务

### 2.7 长队列防护

- 同一个 endpoint group 内任务串行，不影响其他 group
- 不同 endpoint 之间完全解耦
- 后续可选策略：
  - **longest-queue-first**：优先调度队列最长的 group，让长队列尽早开始消化
  - **estimated-duration-first**：根据历史同类任务耗时排序
  - 当前阶段先实现简单 FIFO，预留策略接口

---

## 三、执行效率设计

### 3.1 如何避免全局串行

**旧模型问题：** 如果 `max_bmc_workers=1`，所有 BMC 任务全局串行，即使它们连到不同 endpoint。

**新模型保证：**
- `max_bmc_workers=3` 表示同时最多 3 个不同的 BMC endpoint 在运行
- 3 台不同 BMC IP 的设备可以同时执行，wall clock ≈ 各自耗时取 max 而非 sum
- 只有同一 BMC IP 的任务才强制串行（这是资源安全约束，无法绕过）

### 3.2 最大化不同 endpoint 并发

```
场景：100 台设备，每台 1 个 BMC 任务（100 个不同 BMC IP）
max_bmc_workers = 4

旧行为（语义错误时）：4 个 worker 并发，wall_clock ≈ 100/4 = 25 倍单任务时间
新行为（语义正确时）：4 个 endpoint group 并发，wall_clock ≈ 100/4 = 25 倍单任务时间

场景：100 台设备，每台 1 个 BMC 任务（只有 2 个 BMC IP，各 50 台）
max_bmc_workers = 4

旧行为（语义正确时也应如此）：只有 2 个 endpoint group，实际并发度 = 2
新行为：同上，因为资源约束就是 2 个 BMC IP
```

**关键：** 新模型在资源安全的前提下自动最大化并发度 — 有多少个不同的 endpoint，就能并发多少（受 `max_workers` 上限约束）。

### 3.3 协议间不互相阻塞

```
BMC 池 (max_bmc_workers=3)  ← 独立的 worker 线程池
INBAND 池 (max_inband_workers=5)  ← 独立的 worker 线程池
```

- BMC 浏览器任务慢（10-30s），不会阻塞 SSH 命令任务（1-3s）
- SSH 任务多（500 个设备），不会让 BMC 任务排队等待

---

## 四、耗时统计设计

### 4.1 Plan 级别时间戳

每个 `TaskPlan` 新增以下字段：

```python
@dataclass
class PlanTiming:
    plan_id: str
    execution_id: str
    device_name: str
    device_group: str
    task_name: str
    task_type: str
    endpoint_key: str
    resource_type: str           # "BMC" | "INBAND"
    status: str                   # "SUCCESS" | "FAILED" | "ERROR"
    
    # 调度阶段
    resource_wait_started_at: float   # plan ready → 开始等待资源
    resource_acquired_at: float       # worker 分配成功
    resource_wait_seconds: float      # = acquired - wait_started
    
    # 执行阶段
    executor_started_at: float        # executor.execute() 开始
    executor_finished_at: float       # executor.execute() 返回
    executor_duration_seconds: float  # = finished - started
    
    # 汇总
    started_at: float                 # = resource_wait_started_at
    ended_at: float                   # = executor_finished_at
    duration_seconds: float           # = ended_at - started_at（含等待 + 执行）
    
    retry_count: int
    output_dir: str
```

**关键区分：**
- `duration_seconds` = 任务从"就绪等待资源"到"执行完成"的总时间（含排队等待）
- `executor_duration_seconds` = 纯执行时间（不含排队）
- `resource_wait_seconds` = 排队等待时间（从就绪到拿到 worker）

这三个值的关系：`duration_seconds ≈ resource_wait_seconds + executor_duration_seconds`

### 4.2 Execution 级别统计

```python
@dataclass
class ExecutionTiming:
    execution_id: str
    execution_started_at: float
    execution_finished_at: float
    wall_clock_seconds: float           # 实际墙上时间
    
    total_plans: int
    success_count: int
    failed_count: int
    
    sum_plan_duration_seconds: float     # 所有 plan 的 duration 之和
    sum_executor_duration_seconds: float # 所有 plan 的 executor_duration 之和
    sum_resource_wait_seconds: float     # 所有 plan 的 wait 时间之和
    
    parallel_efficiency: float           # = sum_plan_duration / wall_clock
    
    bottleneck_endpoint: str             # wall_clock 最长的 endpoint_key
    slowest_task: str                    # executor_duration 最长的 plan_id
```

### 4.3 parallel_efficiency 含义

```
parallel_efficiency = sum_plan_duration_seconds / wall_clock_seconds

= 1.0  → 完全串行（所有任务一个接一个）
= 2.5  → 平均 2.5 个任务同时运行
> 1.0  → 有并发效果，越大越好
< 1.0  → 不应出现（除非 wall_clock 包含了非任务时间如启动/收尾）
```

---

## 五、输出报表设计

### 5.1 result.csv（扩展现有字段）

在现有 `ExecutionResult.csv_header()` 基础上追加以下列：

```
现有列（保留）：
  计划ID, 任务ID, 客户端任务ID, 设备分类, 设备名称, 带外管理IP, 带内管理IP,
  任务序号, 任务名称, 任务类型, 执行模式, 执行状态, 执行失败原因,
  规则状态, 规则不符合原因, 工件状态, 工件失败原因, 检查点状态, 检查点汇总,
  运行时上下文, 最终结论, 截图路径, HTML路径, 文本路径, 日志路径, 输出目录,
  开始时间, 结束时间, 耗时秒, 就绪状态, 就绪失败原因

新增 timing 列：
  endpoint_key, resource_wait_seconds, executor_duration_seconds
```

### 5.2 plan_timing.csv（新建）

每 plan 一行，完整耗时记录：

| 字段 | 类型 | 说明 |
|------|------|------|
| execution_id | str | 本次执行 ID |
| plan_id | str | plan 唯一 ID |
| device_name | str | 设备名 |
| device_group | str | 设备分组 |
| task_name | str | 任务名 |
| task_type | str | BMC/SSH/TELNET/VRP |
| endpoint_key | str | 资源端点 key |
| resource_type | str | BMC/INBAND |
| status | str | SUCCESS/FAILED/ERROR |
| resource_wait_started_at | datetime | 开始等待资源 |
| resource_acquired_at | datetime | 获取资源 |
| resource_wait_seconds | float | 排队等待秒数 |
| executor_started_at | datetime | 开始执行 |
| executor_finished_at | datetime | 执行完成 |
| executor_duration_seconds | float | 纯执行秒数 |
| started_at | datetime | 任务开始（= wait_started） |
| ended_at | datetime | 任务结束（= executor_finished） |
| duration_seconds | float | 总耗时（= wait + exec） |
| retry_count | int | 重试次数 |
| output_dir | str | 输出目录 |

### 5.3 device_timing.csv（新建）

每个 device_name/device_group 汇总一行：

| 字段 | 说明 |
|------|------|
| device_group | 设备分组 |
| device_name | 设备名 |
| total_tasks | 该设备总任务数 |
| success | 成功数 |
| failed | 失败数 |
| total_duration_seconds | 该设备所有任务 duration 之和 |
| wall_clock_seconds | 该设备第一个任务开始到最后一个任务结束 |
| avg_task_seconds | 平均每任务耗时 |
| max_task_seconds | 最慢任务耗时 |
| min_task_seconds | 最快任务耗时 |
| sum_executor_seconds | 纯执行时间之和 |
| sum_wait_seconds | 排队等待时间之和 |

### 5.4 endpoint_timing.csv（新建）

每个 endpoint_key 汇总一行：

| 字段 | 说明 |
|------|------|
| endpoint_key | 端点 key |
| resource_type | BMC/INBAND |
| total_tasks | 该 endpoint 总任务数 |
| success | 成功数 |
| failed | 失败数 |
| wall_clock_seconds | 该 endpoint 第一个任务开始到最后一个任务结束 |
| sum_plan_duration_seconds | 所有任务 duration 之和 |
| sum_resource_wait_seconds | 所有任务等待时间之和 |
| sum_executor_duration_seconds | 所有任务执行时间之和 |

### 5.5 execution_summary.csv / execution_summary.json（新建）

整次执行的总览：

| 字段 | 说明 |
|------|------|
| execution_id | 执行 ID |
| execution_started_at | 执行开始时间 |
| execution_finished_at | 执行结束时间 |
| wall_clock_seconds | 墙上时间 |
| total_plans | 总 plan 数 |
| success_count | 成功数 |
| failed_count | 失败数 |
| sum_plan_duration_seconds | 所有 plan 总耗时 |
| sum_executor_duration_seconds | 所有 plan 纯执行时间 |
| sum_resource_wait_seconds | 所有 plan 资源等待时间 |
| parallel_efficiency | 并行效率 |
| bottleneck_endpoint | 耗时最长的 endpoint_key |
| slowest_task | 最慢的 plan_id + task_name |
| total_endpoint_groups | endpoint group 总数 |
| bmc_endpoint_groups | BMC endpoint 数 |
| inband_endpoint_groups | INBAND endpoint 数 |
| max_bmc_workers | 配置的最大 BMC workers |
| max_inband_workers | 配置的最大 INBAND workers |
| resource_wait_top_5 | 等待时间最长的 5 个 plan |

---

## 六、日志设计

### 6.1 调度启动日志

```
============================================================
  Resource-Aware Scheduler 启动
============================================================
  total_plans:              500
  total_endpoint_groups:    120
  bmc_endpoint_groups:      100
  inband_endpoint_groups:    20
  max_bmc_workers:            4
  max_inband_workers:         8
============================================================
```

### 6.2 Per-plan 日志

```
[plan_start] plan_id=abc123 device=A3-01 task=BMC截图 endpoint_key=BMC:192.168.1.200
[resource_wait_start] plan_id=abc123 endpoint_key=BMC:192.168.1.200
[resource_acquired] plan_id=abc123 wait=2.3s
[executor_start] plan_id=abc123
[executor_done] plan_id=abc123 exec_duration=5.2s status=SUCCESS
[plan_done] plan_id=abc123 duration=7.5s status=SUCCESS
```

### 6.3 执行结束日志

```
============================================================
  Execution Summary
============================================================
  wall_clock_seconds:          245.3
  sum_plan_duration_seconds:   876.5
  parallel_efficiency:           3.57
  slowest_endpoint:             BMC:192.168.1.200 (85.2s)
  slowest_task:                 plan_id=xyz789 (SSH巡检 on L2-03, 45.1s)
  resource_wait_top_5:
    1. plan_id=abc (34.2s wait)
    2. plan_id=def (28.1s wait)
    ...
============================================================
```

---

## 七、性能测试计划

### 测试 1：同 endpoint 串行验证

```
场景：3 个 BMC plan，相同 bmc_ip，每个 sleep 1s
max_bmc_workers = 3

预期：
  - 3 个 plan 串行执行（因为同一 endpoint）
  - wall_clock ≈ 3.0s（不是 1.0s）
  - plan_timing.csv 中 3 个 plan 的执行时间窗口无重叠
  - resource_wait_seconds: plan 2 > 0, plan 3 > 0

验证：
  assert 2.7s < wall_clock < 3.5s
  assert all plans SUCCESS
  assert plan[2].started_at >= plan[1].ended_at - 0.1
  assert plan[3].started_at >= plan[2].ended_at - 0.1
```

### 测试 2：不同 endpoint 并发验证

```
场景：3 个 BMC plan，3 个不同 bmc_ip，每个 sleep 1s
max_bmc_workers = 3

预期：
  - 3 个 plan 并发执行
  - wall_clock ≈ 1.0s（不是 3.0s）
  - plan_timing.csv 中 3 个 plan 的执行时间窗口高度重叠
  - resource_wait_seconds: 全部接近 0

验证：
  assert 0.8s < wall_clock < 1.5s
  assert all plans SUCCESS
  assert parallel_efficiency >= 2.5
```

### 测试 3：混合长尾场景

```
场景：
  - endpoint A: 5 个 BMC plan（同一 bmc_ip_a），每个 sleep 0.5s
  - endpoint B: 1 个 BMC plan（bmc_ip_b），sleep 0.5s
  - endpoint C: 1 个 BMC plan（bmc_ip_c），sleep 0.5s
  - endpoint D: 1 个 BMC plan（bmc_ip_d），sleep 0.5s
  max_bmc_workers = 3

预期：
  - B, C, D 不应等待 A 全部完成
  - wall_clock < A 全部串行时间（2.5s）
  - B/C/D 的开始时间应在 A 的第 1 个 plan 开始后不久

验证：
  b_start = plan_timing[B].started_at
  a1_end = plan_timing[A_plan1].ended_at
  assert b_start < a1_end + 1.0  # B 在 A_plan1 完成前后就开始
  assert wall_clock < 2.5s  # 不是 4.0s（全部串行）
```

### 测试 4：耗时统计准确性

```
场景：2 个 BMC endpoint，各 2 个 plan，每个 sleep 0.5s
max_bmc_workers = 2

验证：
  for each plan in plan_timing.csv:
    assert plan.duration_seconds > 0
    assert plan.executor_duration_seconds > 0
    assert plan.duration_seconds >= plan.executor_duration_seconds
    assert plan.duration_seconds ≈ plan.resource_wait_seconds + plan.executor_duration_seconds

  assert execution wall_clock_seconds >= 0.9s
  assert execution wall_clock_seconds <= 1.5s
  assert sum_plan_duration_seconds >= wall_clock_seconds  # 并发场景
  assert parallel_efficiency > 1.0  # 有并发
```

---

## 八、性能风险

### 8.1 锁竞争

- **风险：** `running_groups` set 在主循环和 worker callback 之间共享，需要锁保护
- **影响：** 如果 plan 执行时间极短（< 100ms），锁可能成为瓶颈
- **缓解：** 使用 `threading.Lock` 保护 critical section，只锁 set 操作不锁 IO
- **观测：** 如果 `resource_wait_seconds` 中"等待锁"占比过高（通过 profiling），考虑用 `queue.Queue` 解耦

### 8.2 长尾 endpoint

- **风险：** 某个 endpoint 有 50 个任务，最后一个任务需要等前面 49 个完成
- **影响：** 该 endpoint 的 wall_clock 会很大，但不影响其他 endpoint
- **缓解：** 
  1. 通过 `endpoint_timing.csv` 识别长尾 endpoint
  2. 后续实现 longest-queue-first 策略，让长队列优先调度（尽早开始消化）
  3. 后续实现 estimated-duration-first（短任务优先），让快任务先完成减少排队
- **观测：** `endpoint_timing.csv` 中的 `wall_clock_seconds` 列

### 8.3 慢任务瓶颈

- **风险：** 某个 BMC endpoint 的 SSH 命令超时 60s，占用 worker 不放
- **影响：** 该 endpoint 被阻塞 60s，但不影响其他 endpoint
- **缓解：** 现有 `ssh_command_timeout` 和 `bmc_page_timeout` 已做超时保护
- **观测：** `plan_timing.csv` 中的 `executor_duration_seconds` 最大值

### 8.4 Worker 数调优

- **风险：** `max_bmc_workers` 设太高 → CPU/内存过载；设太低 → 并发度不够
- **影响：** wall_clock 不理想
- **缓解：** 保留现有动态调参逻辑（`_adjust_pools` 基于 CPU/MEM 自动缩放），下限保护
- **观测：** `execution_summary.csv` 中的 `parallel_efficiency`，持续 < 1.5 表示并发不足

### 8.5 与现有动态调参的兼容

- 现有 `ResourceMonitor` + `_adjust_pools` 基于 CPU/MEM 动态调整 worker pool size
- 新模型中 worker pool size = max concurrent endpoint groups
- 调整逻辑不变：CPU/MEM 高 → 减少 `max_workers`（减少并发 endpoint group 数）
- 需要新增 `max_inband_workers` 的动态调整（复用同一逻辑）

---

## 九、实现清单

### Phase 1：核心调度（P0）

- [ ] 新建 `EndpointAwareScheduler` 类
- [ ] 实现 `endpoint_key(plan)` 函数
- [ ] 实现 `group_by_endpoint_key(plans)` 分组逻辑
- [ ] 实现 endpoint-group 级别调度循环
- [ ] 改 `WorkerPool` 从 track `device_id` 改为 track `endpoint_key`
- [ ] 重命名配置：`max_ssh_workers` → `max_inband_workers`
- [ ] 将 TELNET 正确路由到 INBAND 池
- [ ] 新增 VRP 协议支持

### Phase 2：耗时统计（P0）

- [ ] 新增 `PlanTiming` dataclass
- [ ] 新增 `ExecutionTiming` dataclass
- [ ] 在调度循环中埋点记录时间戳
- [ ] 在 `ExecutionResult` 中追加 timing 字段

### Phase 3：报表输出（P1）

- [ ] 扩展 `result.csv` 追加 timing 列
- [ ] 实现 `plan_timing.csv` 输出
- [ ] 实现 `device_timing.csv` 输出
- [ ] 实现 `endpoint_timing.csv` 输出
- [ ] 实现 `execution_summary.csv` / `execution_summary.json` 输出

### Phase 4：日志（P1）

- [ ] 调度启动日志
- [ ] Per-plan 详细日志
- [ ] 执行结束汇总日志

### Phase 5：测试（P1）

- [ ] 测试 1：同 endpoint 串行
- [ ] 测试 2：不同 endpoint 并发
- [ ] 测试 3：混合长尾场景
- [ ] 测试 4：耗时统计准确性

---

## 十、向后兼容

- `max_bmc_workers` 和 `max_ssh_workers` 的 YAML 配置 key 保持不变，内部映射为新语义
- `result.csv` 新增列为追加，不影响现有列的索引
- 现有 `DynamicScheduler` 保留为 `LegacyDynamicScheduler`，新调度器在新类中实现
- `WorkerPool` 拆分为 `LegacyWorkerPool`（track device_id）和 `EndpointWorkerPool`（track endpoint_key）
