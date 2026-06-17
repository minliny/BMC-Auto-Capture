# 规则编写指南

规则用于在执行截图后判断 BMC/SSH 页面状态是否满足测试用例要求。**规则不影响截图步骤**，只在截图完成后运行。

## 当前统一检查模型

执行端已经把分散的检查结果统一记录为 `CheckResult`。这不是一套新的规则语法，而是统一输出模型，方便 CSV/API/final verdict 汇总。

统一阶段：

| stage | 含义 |
|---|---|
| `CONFIG_CHECK` | 配置和 schema 检查 |
| `PRECHECK` | 执行前连接、端口、资源检查 |
| `SESSION_CHECK` | 登录态、会话、页面健康检查 |
| `EXECUTION_CHECK` | 命令或页面动作是否执行成功 |
| `READY_CHECK` | 最终截图/取证前是否到达正确状态 |
| `ARTIFACT_CHECK` | 截图、HTML、TXT、log 等证据是否可信 |
| `RESULT_CHECK` | 任务结果是否满足业务预期 |
| `POST_AUDIT` | 事后证据审计诊断，不直接改变最终结论 |

统一结果字段：

```json
{
  "stage": "RESULT_CHECK",
  "check_id": "ssh.result_rules",
  "status": "PASS|FAIL|WARN|SKIP|ERROR",
  "severity": "ERROR|WARNING|INFO",
  "message": "失败或通过说明",
  "details": {
    "field": "protocol",
    "raw_line": "100GE1/0/1 up down ..."
  }
}
```

现有 `rules`、`result_rules`、`capture_ready_conditions`、`evidence_checkpoints` 和 legacy `checkpoints` 会继续兼容，同时迁移为统一 `CheckResult` 输出。

每个成功进入取证阶段的 BMC/SSH 任务还会生成 `<证据文件名>.metadata.json`，记录本次产物清单、相对路径、文件大小和 SHA-256。该清单只用于离线复核截图/HTML/TXT/state JSON 是否完整、是否被替换；它不代替 `capture_ready_conditions` 的页面就绪判断，也不代替 `result_rules` 的业务结果判断。

## 一、规则在 tasks.json 中的位置

```json
{
  "tasks": {
    "task.bmc.storage": {
      "task_id": "task.bmc.storage",
      "task_name": "存储页面检查",
      "task_type": "BMC",
      "execution_mode": "BMC_URL",
      "command_or_url": "/UI/Static/#/navigate/system/storage",
      "rules": [
        // 规则组写在这里
      ]
    }
  }
}
```

## 二、规则组格式

```json
{
  "name": "规则组名称（描述这个组要检查什么）",
  "desc": "用自然语言描述检查目的，帮助模型理解",
  "checks": [
    // 检查项列表
  ]
}
```

每个任务的 BMC 页面检查使用 `rules: [规则组, 规则组, ...]`；SSH/TELNET 命令执行结果检查推荐使用 `result_rules: [规则组, 规则组, ...]`。

`result_rules` 是执行结果校验规则，只读取命令输出和已保存证据，不执行页面点击、填表、截图等动作。旧字段 `ssh_rules` 和 SSH/TELNET 任务上的 `rules` 会继续兼容，但新任务应写 `result_rules`。

## 三、检查项格式

```json
{
  "type": "检查类型",
  "target": "检查目标（CSS选择器或文本）",
  "expect": "预期值",
  "desc": "用自然语言描述这条检查"
}
```

## 四、检查类型

| type | 含义 | target 填写 | expect 填写 |
|---|---|---|---|
| `text_exists` | 页面包含指定文本 | 要查找的文本 | 空 |
| `text_not_exists` | 页面不包含指定文本 | 不应出现的文本 | 空 |
| `element_exists` | 指定元素存在且可见 | CSS选择器 | 空 |
| `element_not_exists` | 指定元素不存在 | CSS选择器 | 空 |
| `element_text_is` | 元素文本等于某值 | CSS选择器 | 期望文本 |
| `element_text_contains` | 元素文本包含某值 | CSS选择器 | 包含文本 |

SSH/TELNET `result_rules` 支持的检查类型：

| type | 含义 | 关键字段 |
|---|---|---|
| `contains` / `text_exists` / `required_pattern` / `required_patterns` | 命令输出必须包含文本 | `target` 或 `patterns` |
| `not_contains` / `text_not_exists` / `forbidden_pattern` / `forbidden_patterns` | 命令输出不得包含文本 | `target` 或 `patterns` |
| `regex_exists` / `regex_match` | 命令输出必须匹配正则 | `pattern` 或 `target` |
| `regex_all_of` | 命令输出必须匹配全部正则 | `patterns` |
| `regex_any_of` | 命令输出至少匹配一个正则 | `patterns` |
| `regex_not_exists` / `regex_not_match` | 命令输出不得匹配正则 | `pattern` 或 `target` |
| `min_output_lines` | 输出行数不少于指定值 | `target` |
| `min_body_lines` | 去掉命令回显、空行、prompt 后正文行数不少于指定值 | `target` |
| `command_echo_required` | interactive shell 输出中必须能看到命令回显 | 无 |
| `prompt_required` | interactive shell 输出中必须能看到设备 prompt | 无 |
| `interface_status` | 结构化解析 `display interface brief` 真实接口行 | `fields`, `forbidden` |

`interface_status` 不做全文 substring 匹配，只检查真实接口记录中的 `physical` / `protocol` 字段。命令回显、prompt、表头、legend、分隔线、描述字段里的 `down` 不会触发失败；无法解析接口行时返回 `RULE_PARSE_FAILED`。SSH/TELNET 命令执行成功但 `result_rules` 失败时，执行状态为 `EXEC_SUCCESS_RULE_FAILED`，失败明细进入 `CheckResult.details.failures` 和 `failure_detail.csv`，TXT 证据仍保留完整命令输出。

## 五、如何找到 CSS 选择器（HTML 元素定位）

浏览器 F12 → Elements 面板 → 找到目标元素 → 右键 → Copy → Copy selector。

优先用：
- 文本匹配：`text=RAID卡` 或 `text=Storage`
- 属性匹配：`[title="系统状态"]`
- ID：`#storage-table`
- Class：`.port-status`
- 标签+属性：`td.down`、`span.alert`

## 六、BMC 页面常用检查模式

### 检查页面正常加载
```json
{"type": "text_exists", "target": "Storage", "expect": "", "desc": "页面包含 Storage 标题"}
```

### 检查无异常告警
```json
{"type": "element_not_exists", "target": ".alert-danger", "expect": "", "desc": "无红色告警"}
{"type": "text_not_exists", "target": "告警", "expect": "", "desc": "页面无告警文字"}
{"type": "text_not_exists", "target": "异常", "expect": "", "desc": "无异常状态"}
```

### 检查设备信息正确
```json
{"type": "element_text_contains", "target": "#device-status", "expect": "OK", "desc": "设备状态为OK"}
```

### 检查端口状态（SSH 命令输出）
```json
{
  "type": "interface_status",
  "fields": ["physical", "protocol"],
  "forbidden": ["down"],
  "desc": "真实接口记录的 physical/protocol 状态不得为 down"
}
```

## 七、完整示例

```json
{
  "tasks": {
    "task.bmc.raid_config": {
      "task_id": "task.bmc.raid_config",
      "task_name": "RAID配置测试",
      "task_type": "BMC",
      "execution_mode": "BMC_URL",
      "command_or_url": "/UI/Static/#/navigate/system/storage",
      "rules": [
        {
          "name": "RAID页面基础检查",
          "desc": "验证存储页面正常加载，无异常告警，RAID卡信息可见",
          "checks": [
            {"type": "text_exists", "target": "Storage", "expect": "", "desc": "页面标题包含Storage"},
            {"type": "element_not_exists", "target": ".alert-danger", "expect": "", "desc": "无红色告警"},
            {"type": "text_not_exists", "target": "异常", "expect": "", "desc": "无异常状态"},
            {"type": "text_not_exists", "target": "告警", "expect": "", "desc": "无告警信息"}
          ]
        }
      ]
    }
  }
}
```

```json
{
  "tasks": {
    "task.019": {
      "task_id": "task.019",
      "task_name": "计算节点L1交换网络端口查询测试",
      "task_type": "SSH",
      "execution_mode": "SSH_CMD",
      "command_or_url": "display interface brief | include up",
      "result_rules": [
        {
          "name": "端口状态检查",
          "desc": "L1交换机接口状态字段应正常，不因描述文本中的 down 误判",
          "checks": [
            {
              "type": "interface_status",
              "fields": ["physical", "protocol"],
              "forbidden": ["down"],
              "desc": "真实接口记录的 physical/protocol 状态不得为 down"
            }
          ]
        }
      ]
    }
  }
}
```

## 八、关键原则

1. **规则不影响截图** — 规则校验失败 ≠ 截图丢失，截图始终在规则运行前保存
2. **规则组独立** — 一个规则组失败不影响其他组
3. **检查项有序** — 同组内按顺序执行，任一项失败该组标记失败
4. **用 desc 描述意图** — 帮助模型和人类理解每条规则的目的
