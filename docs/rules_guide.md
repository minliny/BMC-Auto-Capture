# 规则编写指南

规则用于在执行截图后判断 BMC/SSH 页面状态是否满足测试用例要求。**规则不影响截图步骤**，只在截图完成后运行。

## 一、规则在 tasks.json 中的位置

```json
{
  "任务名称": {
    "task_type": "BMC",
    "execution_mode": "BMC_URL",
    "command_or_url": "/UI/Static/#/navigate/system/storage",
    "rules": [
      // 规则组写在这里
    ]
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

每个任务是 `rules: [规则组, 规则组, ...]`。

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
  "RAID配置测试": {
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
```

```json
{
  "计算节点L1交换网络端口查询测试": {
    "task_type": "SSH",
    "execution_mode": "SSH_CMD",
    "command_or_url": "display interface brief | include up",
    "rules": [
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
```

## 八、关键原则

1. **规则不影响截图** — 规则校验失败 ≠ 截图丢失，截图始终在规则运行前保存
2. **规则组独立** — 一个规则组失败不影响其他组
3. **检查项有序** — 同组内按顺序执行，任一项失败该组标记失败
4. **用 desc 描述意图** — 帮助模型和人类理解每条规则的目的
