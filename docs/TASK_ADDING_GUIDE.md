# 任务添加指南

本文说明如何新增当前执行端支持的两类任务：

- SSH 终端任务：常规 Linux 终端证据、VRP 交互终端证据
- BMC 浏览器页面任务：直接跳转截图、页面动作后截图

任务配置由两部分组成：

1. Excel `任务列表`：决定任务是否参与编排、任务 ID、任务序号、任务名、设备分组、输出模板。
2. `tasks.json`：按任务 ID 补充具体执行方式、命令、URL、动作、规则和重试参数。

新增任务时必须同时维护这两处。当前简化版 Excel 没有完整命令列，SSH 任务如果只加 Excel 行而没有 `tasks.json` 定义，会被视为缺少命令。

## 一、通用添加流程

### 1. 在 Excel `任务列表` 增加一行

当前推荐使用简化或扩展格式：

| 列 | 含义 | 示例值 |
| --- | --- | --- |
| 任务ID | 稳定任务定义 ID，必须和 `tasks.json.tasks` 的 key 一致 | `task.030` |
| 任务序号 | 排序和展示用序号 | `21` |
| 任务名称 | 展示名称，可后续修改，不作为主匹配键 | `新增任务名称` |
| 任务类型 | `SSH` 或 `BMC` | `SSH` |
| 设备分组 | 匹配设备分组，支持 `/` 多分组 | `A3` 或 `L1/L2` |
| 输出目录模板 | 任务输出子目录 | `{任务序号} {任务名称}/{设备分类}` |
| 文件名模板 | 证据文件名 | `{带内管理IP}-{设备名称}` |
| 是否启用 | `是/否`、`true/false`、`1/0` | `是` |
| 是否全量截图 | 可选，BMC/SSH 截图控制 | `是` |
| 截图模式 | 可选，BMC 支持 `auto/viewport/content/full_page` | `auto` |

设备分组匹配规则：

- `A3` 只匹配 A3 设备。
- `L1/L2` 会拆分为 L1、L2 两组分别编排。
- 每个生成出的执行项仍然是“一台设备 + 一个任务”。

### 2. 在 `tasks.json` 增加同 ID 定义

`tasks.json` 顶层结构固定为：

```json
{
  "_schema": "2.0",
  "tasks": {
    "task.030": {
      "task_id": "task.030",
      "task_name": "新增任务名称",
      "task_type": "SSH",
      "execution_mode": "SSH_CMD"
    }
  }
}
```

`tasks.json.tasks` 的 key 必须和 Excel `任务ID` 完全一致。`task_name` 只作为展示名称和输出模板变量，不再作为主匹配键；老表没有 `任务ID` 时仍会按任务名兼容匹配并输出 warning。

### 3. 新增前检查

- 任务 ID 保持唯一、稳定。
- 任务名用于展示，可以修改；不要再依赖任务名做匹配。
- Excel 任务类型和 `tasks.json.task_type` 一致。
- SSH 任务必须有命令来源：`command_or_url` 或 `per_group_commands`。
- BMC 任务必须有目标页面来源：`command_or_url` 或 `actions_json` 第一条 `goto`。
- 含分号的 shell 复合命令必须配置 `no_split` 或 `per_group_no_split`。
- 对关键 BMC 页面，建议在 RulePack 里加 ready checks，不要只依赖“能截图”判断页面正确。

## 二、SSH 终端任务

SSH 当前按用户语义分两类：

| 用户分类 | 推荐配置 | 执行行为 |
| --- | --- | --- |
| 常规 Linux | `ssh_profile: "linux"` | 打开 Linux PTY 终端，保存接近人工登录的终端输出并渲染截图 |
| VRP | `ssh_profile: "vrp"` | 打开 VRP 交互 shell，处理 prompt、命令回显、分页和 More |

如果不显式写 `ssh_profile`，执行端会按设备分组推断：

- L1/L2 -> `vrp`
- 其他分组 -> `linux`

### 1. 常规 Linux 任务

适用于 Linux shell 命令、工具查询、脚本输出采集。

```json
{
  "tasks": {
    "task.linux.system_info": {
      "task_id": "task.linux.system_info",
      "task_name": "Linux系统信息采集",
      "task_type": "SSH",
      "execution_mode": "SSH_CMD",
      "ssh_profile": "linux",
      "ssh_evidence_mode": "terminal",
      "command_or_url": "uname -a",
      "timeout_seconds": 60,
      "retry_count": 0,
      "output_dir_template": "{任务序号} {任务名称}/{设备分类}",
      "image_name_template": "{带内管理IP}-{设备名称}"
    }
  }
}
```

说明：

- `ssh_evidence_mode: "terminal"` 是当前主链路，表示保留终端样式证据。
- 终端模式会保留命令回显、prompt 和终端输出，更接近人工登录看到的样式。
- 如果只想要结构化远程执行并依赖 exit code/stderr，可显式设置 `ssh_evidence_mode: "structured"`，执行端会走内部 `exec_command` 能力。普通证据采集不推荐使用。
- 任务 JSON 只描述执行方式；审计规则写入 `config/rule_packs/{protocol}/{task_id}.json`。

### 2. VRP 任务

适用于 L1/L2 这类交换/VRP 设备。

```json
{
  "tasks": {
    "task.vrp.interface_brief": {
      "task_id": "task.vrp.interface_brief",
      "task_name": "VRP接口状态查询",
      "task_type": "SSH",
      "execution_mode": "SSH_CMD",
      "ssh_profile": "vrp",
      "ssh_evidence_mode": "terminal",
      "command_or_url": "screen-length 0 temporary\ndisplay interface brief",
      "timeout_seconds": 60,
      "retry_count": 0,
      "output_dir_template": "{任务序号} {任务名称}/{设备分类}",
      "image_name_template": "{带内管理IP}-{设备名称}"
    }
  }
}
```

说明：

- VRP 模式会在同一个交互会话内发送命令。
- 执行器会处理 prompt、命令回显和 More 分页。
- 多条命令推荐用换行分隔。

### 3. 多分组同一任务，命令不同

同一任务如果要同时匹配 Linux 和 VRP，可以在 Excel `设备分组` 写 `A3/L1/L2`，再用 `per_group_commands` 覆盖不同分组命令。

```json
{
  "tasks": {
    "task.vrp.transceiver_info": {
      "task_id": "task.vrp.transceiver_info",
      "task_name": "光模块信息查询",
      "task_type": "SSH",
      "execution_mode": "SSH_CMD",
      "command_or_url": "display interface transceiver",
      "per_group_commands": {
        "A3": "for i in $(seq 0 15); do echo \"==============> $i\"; hccn_tool -i $i -optical -g; done"
      },
      "per_group_no_split": {
        "A3": true
      },
      "timeout_seconds": 900,
      "per_group_timeout_seconds": {
        "A3": 900,
        "L1": 60,
        "L2": 60
      },
      "retry_count": 0,
      "output_dir_template": "{任务序号} {任务名称}/{设备分类}",
      "image_name_template": "{带内管理IP}-{设备名称}"
    }
  }
}
```

说明：

- A3 使用 `per_group_commands.A3`。
- L1/L2 没有分组覆盖时，使用默认 `command_or_url`。
- A3 命令包含多个分号，但这是一个 shell 复合命令，所以必须设置 `per_group_no_split.A3: true`。
- A3 光模块循环命令可以保留较长超时；L1/L2 VRP 查询建议用 `per_group_timeout_seconds` 覆盖为较短超时。

### 4. SSH 字段速查

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `task_type` | 是 | 固定 `SSH` |
| `execution_mode` | 是 | 固定 `SSH_CMD` |
| `ssh_profile` | 推荐 | `linux` 或 `vrp`，不填则按设备分组推断 |
| `ssh_evidence_mode` | 推荐 | 主链路使用 `terminal` |
| `command_or_url` | 是 | 默认命令 |
| `per_group_commands` | 否 | 按设备分组覆盖命令 |
| `no_split` | 否 | 全局禁止按换行/分号拆命令 |
| `per_group_no_split` | 否 | 按设备分组禁止拆命令 |
| `timeout_seconds` | 推荐 | 单任务超时时间 |
| `per_group_timeout_seconds` | 否 | 按设备分组覆盖超时，适合同一任务同时覆盖 Linux 长命令和 VRP 短命令 |
| `retry_count` | 推荐 | 失败后重试次数 |
| `result_rules` | 兼容 | 执行端运行字段；新规则建议通过 RulePack 生成 |

## 三、BMC 浏览器页面任务

BMC 是浏览器页面证据采集，不属于终端任务。执行端会先登录 BMC Web UI，再跳转页面、执行动作、截图和保存 HTML/状态证据。

BMC 支持两种任务：

| 模式 | 用途 | 目标页面来源 |
| --- | --- | --- |
| `BMC_URL` | 直接跳转指定页面并截图 | `command_or_url` |
| `BMC_ACTIONS` | 跳转后执行点击/等待/切换等动作，再截图 | `actions_json` 第一条 `goto` |

### 1. 直接页面截图：BMC_URL

适用于只需要进入某个页面截图的任务。

```json
{
  "tasks": {
    "task.bmc.asset_page": {
      "task_id": "task.bmc.asset_page",
      "task_name": "BMC资产页面截图",
      "task_type": "BMC",
      "execution_mode": "BMC_URL",
      "command_or_url": "/UI/Static/#/navigate/<PAGE>",
      "timeout_seconds": 60,
      "retry_count": 0,
      "output_dir_template": "{任务序号} {任务名称}/{设备分类}",
      "image_name_template": "{带外管理IP}-{设备名称}"
    }
  }
}
```

说明：

- `command_or_url` 推荐写相对页面路径，执行端会自动拼到当前设备的 BMC 地址。
- 如果写完整 URL，host 必须和当前设备 BMC 地址一致，否则会被拦截。
- BMC 页面就绪和证据规则写入 `config/rule_packs/bmc/{task_id}.json`，不要在新任务里直接写运行字段。
- 如果 BMC 任务没有 RulePack ready checks，执行端会默认检查页面存活、未回到登录页，并从 `command_or_url` 或第一条 `goto` 派生目标路由 `url_contains`。

RulePack 中 BMC ready checks 常用类型：

| type | 用途 | 关键字段 |
| --- | --- | --- |
| `url_contains` / `url_not_contains` | 校验当前 URL/hash | `target` |
| `selector_visible` / `selector_hidden` | 校验元素可见或不可见 | `selector` |
| `selector_count_ge` / `count_ge` | 校验列表/表格行数达到门槛 | `selector`, `min_count` |
| `text_contains` / `text_contains_any` | 校验页面正文包含关键文本 | `target` 或 `values` |
| `text_nonempty` | 校验一个或多个字段不是空文本 | `selector` 或 `selectors` |
| `text_not_in` | 校验字段不为占位符或 loading 文本 | `selector`/`selectors`, `values` |
| `region_stable` | 校验目标区域文本在稳定窗口内不再变化 | `selector`, `stable_for_ms`, `sample_interval_ms` |
| `active_tab_changed` | 校验点击后活动页签/按钮状态可见 | `selector`, `values` 或 `expected` |
| `post_action_state_changed` | 校验动作后目标区域出现期望状态 | `selector`, `values` 或 `expected` |

关键 BMC 页面建议至少组合 `url_contains`、业务容器 `selector_visible`、关键字段 `text_nonempty`、占位符排除 `text_not_in`。动态表格页面再加 `count_ge` 和 `region_stable`。

### 2. 页面动作后截图：BMC_ACTIONS

适用于进入页面后还要展开树、点击页签、等待控件或保存中间证据的任务。

```json
{
  "tasks": {
    "task.bmc.action_flow": {
      "task_id": "task.bmc.action_flow",
      "task_name": "BMC动作流截图",
      "task_type": "BMC",
      "execution_mode": "BMC_ACTIONS",
      "command_or_url": "",
      "actions_json": "[{\"action\":\"goto\",\"value\":\"/UI/Static/#/navigate/<PAGE>\"},{\"action\":\"wait\",\"value\":\"2\"},{\"action\":\"click\",\"selector\":\"<CSS_SELECTOR>\"},{\"action\":\"wait\",\"value\":\"2\"},{\"action\":\"screenshot\",\"value\":\"after_click\"},{\"action\":\"save_html\"}]",
      "timeout_seconds": 120,
      "retry_count": 0,
      "output_dir_template": "{任务序号} {任务名称}/{设备分类}",
      "image_name_template": "{带外管理IP}-{设备名称}"
    }
  }
}
```

说明：

- `actions_json` 必须是合法 JSON 字符串。
- 第一条 `goto` 是目标页面来源。
- `screenshot` 和 `save_html` 在动作流中会作为中间证据保存；最终截图仍会在动作和 ready 检查后统一执行。
- 点击类动作建议后面配 `wait` 或 `wait_for_selector`，避免页面还没稳定就截图。

### 3. BMC 字段速查

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `task_type` | 是 | 固定 `BMC` |
| `execution_mode` | 是 | `BMC_URL` 或 `BMC_ACTIONS` |
| `command_or_url` | `BMC_URL` 必填 | 目标页面路径 |
| `actions_json` | `BMC_ACTIONS` 必填 | 页面动作 JSON 字符串，第一条应为 `goto` |
| `timeout_seconds` | 推荐 | 单任务超时时间 |
| `retry_count` | 推荐 | 失败后重试次数 |
| `capture_ready_conditions` | 兼容 | RulePack adapter 输出；新任务不要手写 |
| `evidence_checkpoints` | 兼容 | RulePack adapter 输出；新任务不要手写 |
| `rules` | legacy | 旧页面规则校验；新规则不要生成 |
| `output_dir_template` | 推荐 | 输出目录模板 |
| `image_name_template` | 推荐 | 证据文件名模板 |
| `artifact_profile` | 可选 | `full` 完整证据；`fast` 仅 PNG/HTML |

`artifact_profile` 默认不填，即使用全局 `full`。只有现场批量执行确认不需要 MHTML/state JSON 时，才建议显式设为 `fast`。

BMC 和 SSH/TELNET 执行器会为每次取证额外写一个 `<证据文件名>.metadata.json`。该文件记录截图、HTML、TXT、MHTML、state JSON 等产物的相对路径、文件大小和 SHA-256，用于离线复核证据是否完整、是否被替换。`metadata.json` 是本地诊断证据，不改变 Excel 字段，也不代表自动上传文件。

## 四、验证方式

新增任务后，先做静态检查或预检查，再执行真实任务。

源码环境可运行：

```bash
python run.py --excel <EXCEL_FILE> --precheck-only
```

Windows release 包可运行：

```cmd
启动.bat
```

然后选择 Excel 文件并先执行预检查。

重点检查：

- 新任务是否出现在执行计划里。
- 匹配的设备分组数量是否符合预期。
- SSH 命令是否为空。
- BMC 目标页面是否为空。
- 输出目录和文件名模板是否存在未替换变量。

## 五、常见错误

### 1. Excel 加了任务，但没有执行

检查：

- Excel `是否启用` 是否为启用值。
- `设备分组` 是否能匹配设备表中的分组。
- `tasks.json` 是否存在同名 key。
- `tasks.json.enabled` 是否显式为 `false`。

### 2. SSH 任务被拆成多条错误命令

如果命令本身包含 shell 语法分号，例如 `for/do/done` 或 `if/fi`，必须配置：

```json
{
  "no_split": true
}
```

或仅对某个分组生效：

```json
{
  "per_group_no_split": {
    "A3": true
  }
}
```

### 3. SSH 任务失败后等待很久

如果同一个任务同时覆盖 Linux 和 VRP，先检查 `timeout_seconds` 是否按最长命令配置。需要长短命令共用一个任务时，给短命令分组配置：

```json
{
  "per_group_timeout_seconds": {
    "A3": 900,
    "L1": 60,
    "L2": 60
  }
}
```

### 4. BMC 截图不是目标页面

检查：

- `BMC_URL.command_or_url` 是否指向正确页面。
- `BMC_ACTIONS.actions_json` 第一条是否为 `goto`。
- 是否需要等待页面加载、展开树、切换 tab。
- 是否为关键页面配置了 RulePack ready checks。

### 5. 任务状态无法可靠回写服务端

当前任务状态回写使用 `planId` 表示一次全量执行批次，`taskId` 表示 Excel 中的稳定任务定义，`planItemId` 表示批次内“一台设备 + 一个任务”的具体执行项。新增任务时必须保持 `taskId` 稳定；任务名可作为展示文本调整。
