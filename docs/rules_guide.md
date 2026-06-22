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

新规则应通过 `rulepack.v1` 管理。执行端会把 RulePack 适配成 `result_rules`、`capture_ready_conditions` 和 `evidence_checkpoints`。旧 `rules`、`rules_json`、`ssh_rules`、`checkpoints`、`stderr_fail_patterns`、`stderr_allow_patterns`、`stderr_ignore_patterns`、`allow_exit_codes` 只作为兼容输入保留，不应由 skill 或新 API 客户端生成。

每个成功进入取证阶段的 BMC/SSH 任务还会生成 `<证据文件名>.metadata.json`，记录本次产物清单、相对路径、文件大小和 SHA-256。该清单只用于离线复核截图/HTML/TXT/state JSON 是否完整、是否被替换；它不代替 `capture_ready_conditions` 的页面就绪判断，也不代替 `result_rules` 的业务结果判断。

## 一、规则入口

推荐入口是 `config/rule_packs/{protocol}/{task_id}.json`。`tasks.json` 中的运行字段由 RulePack adapter 生成或兼容读取；不要为新任务手写 `rules` / `ssh_rules` / `checkpoints` / `stderr_fail_patterns`。

## 二、legacy `rules` 规则组格式

本节只说明旧 `rules` / `rules_json` 兼容层。新规则包不要使用这个格式。

```json
{
  "name": "规则组名称（描述这个组要检查什么）",
  "desc": "用自然语言描述检查目的，帮助模型理解",
  "checks": [
    // 检查项列表
  ]
}
```

`result_rules` 是执行端运行字段，只读取命令输出和已保存证据，不执行页面点击、填表、截图等动作。新规则包不要直接生成 `result_rules`；应生成 RulePack 的 `stage_gate`、`action_completion`、`content_integrity` 或 `evidence_validation`。

## 三、legacy 检查项格式

```json
{
  "type": "检查类型",
  "target": "检查目标（CSS选择器或文本）",
  "expect": "预期值",
  "desc": "用自然语言描述这条检查"
}
```

## 四、legacy BMC 检查类型与 SSH/TELNET 运行检查类型

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
| `allowed_patterns` | 指定输出流每个非空行必须匹配 allow 或 ignore pattern | `source`, `patterns`, `ignore_patterns` |
| `min_output_lines` | 输出行数不少于指定值 | `target` |
| `min_body_lines` | 去掉命令回显、空行、prompt 后正文行数不少于指定值 | `target` |
| `command_echo_required` | interactive shell 输出中必须能看到命令回显 | 无 |
| `prompt_required` | interactive shell 输出中必须能看到设备 prompt | 无 |
| `sentinel_seen` | 命令输出必须出现明确完成标记 | `target`, `patterns` |
| `exit_code_in` | exit code 必须在允许集合内 | `source: "exit_code"`, `allowed`, `values`, `target` |
| `pager_exhausted` | 输出中不得残留分页提示 | `patterns` 或默认分页提示 |
| `interface_status` / `interface_status_not` | 结构化解析 `display interface brief` 真实接口行 | `fields`, `forbidden` |

`interface_status` 不做全文 substring 匹配，只检查真实接口记录中的 `physical` / `protocol` 字段。命令回显、prompt、表头、legend、分隔线、描述字段里的 `down` 不会触发失败；无法解析接口行时返回 `RULE_PARSE_FAILED`。SSH/TELNET 命令执行成功但 `result_rules` 失败时，执行状态为 `EXEC_SUCCESS_RULE_FAILED`，失败明细进入 `CheckResult.details.failures` 和 `failure_detail.csv`，TXT 证据仍保留完整命令输出。

SSH/TELNET 检查可使用 `source` 限定检查对象：`combined`、`stdout`、`stderr`、`cmd:<name>` 或 `exit_code`。旧 `stderr_fail_patterns` 等字段会被兼容转换为 source-aware `result_rules`，但不再作为新配置入口。

## 五、如何找到 CSS 选择器（HTML 元素定位）

浏览器 F12 → Elements 面板 → 找到目标元素 → 右键 → Copy → Copy selector。

优先用：
- 文本匹配：`text=RAID卡` 或 `text=Storage`
- 属性匹配：`[title="系统状态"]`
- ID：`#storage-table`
- Class：`.port-status`
- 标签+属性：`td.down`、`span.alert`

## 六、RulePack 检查常用模式

### 检查页面正常加载
```json
{"type": "text_contains", "target": "Storage", "desc": "页面包含 Storage 标题"}
```

### 检查无异常告警
```json
{"type": "selector_not_visible", "selector": ".alert-danger", "desc": "无红色告警"}
{"type": "text_not_in", "target": "body", "values": ["告警", "异常"], "desc": "页面无异常告警文字"}
```

### 检查设备信息正确
```json
{"type": "text_contains", "target": "OK", "desc": "设备状态为 OK"}
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

## 七、RulePack 完整示例

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.bmc.raid_config.v1",
  "task_id": "task.bmc.raid_config",
  "protocol": "BMC",
  "execution_mode": "BMC_URL",
  "applies_to": {
    "task_ids": ["task.bmc.raid_config"],
    "task_type": "BMC",
    "execution_modes": ["BMC_URL"]
  },
  "audit_metadata": {
    "created_by": "bmc-auto-capture-bmc-page-rules",
    "created_from_artifacts": ["output/task.bmc.raid_config.html"],
    "artifact_hashes": {},
    "review_status": "generated"
  },
  "rule_classes": {
    "stage_gate": [
      {
        "rule_id": "bmc.storage_route_ready",
        "priority": "P0",
        "effect_on_final": "fail",
        "checks": [
          {"type": "url_contains", "target": "/navigate/system/storage"},
          {"type": "text_contains", "target": "Storage"}
        ]
      }
    ],
    "action_completion": [],
    "content_integrity": [
      {
        "rule_id": "bmc.storage_no_alert",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [
          {"type": "selector_not_visible", "selector": ".alert-danger"},
          {"type": "text_not_in", "target": "body", "values": ["异常", "告警"]}
        ]
      }
    ],
    "evidence_validation": [
      {
        "rule_id": "bmc.storage_evidence_contains_title",
        "priority": "P2",
        "effect_on_final": "warning",
        "checks": [
          {"type": "html_contains", "target": "Storage"}
        ]
      }
    ]
  }
}
```

```json
{
  "schema_version": "rulepack.v1",
  "rule_pack_id": "rulepack.task.019.v1",
  "task_id": "task.019",
  "protocol": "SSH",
  "execution_mode": "SSH_CMD",
  "applies_to": {
    "task_ids": ["task.019"],
    "task_type": "SSH",
    "execution_modes": ["SSH_CMD"]
  },
  "audit_metadata": {
    "created_by": "bmc-auto-capture-ssh-output-rules",
    "created_from_artifacts": ["output/task.019.txt"],
    "artifact_hashes": {},
    "review_status": "generated"
  },
  "rule_classes": {
    "stage_gate": [
      {
        "rule_id": "ssh.prompt_seen",
        "priority": "P1",
        "effect_on_final": "partial",
        "checks": [
          {"type": "prompt_required"}
        ]
      }
    ],
    "action_completion": [
      {
        "rule_id": "ssh.exit_code_ok",
        "priority": "P0",
        "effect_on_final": "fail",
        "checks": [
          {"type": "exit_code_in", "source": "exit_code", "allowed": [0]}
        ]
      }
    ],
    "content_integrity": [
      {
        "rule_id": "ssh.interface_status_ok",
        "priority": "P0",
        "effect_on_final": "fail",
        "checks": [
          {
            "type": "interface_status",
            "fields": ["physical", "protocol"],
            "forbidden": ["down"],
            "desc": "真实接口记录的 physical/protocol 状态不得为 down"
          }
        ]
      }
    ],
    "evidence_validation": [
      {
        "rule_id": "ssh.transcript_contains_command",
        "priority": "P2",
        "effect_on_final": "warning",
        "checks": [
          {"type": "text_contains", "target": "display interface brief"}
        ]
      }
    ]
  }
}
```

旧 `rules` / `rules_json` / `ssh_rules` / `checkpoints` / `stderr_*` / `allow_exit_codes` 只读兼容，不作为新示例。

## 八、关键原则

1. **规则不影响截图** — 规则校验失败 ≠ 截图丢失，截图始终在规则运行前保存
2. **规则组独立** — 一个规则组失败不影响其他组
3. **检查项有序** — 同组内按顺序执行，任一项失败该组标记失败
4. **用 desc 描述意图** — 帮助模型和人类理解每条规则的目的
