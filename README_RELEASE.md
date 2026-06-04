# BMC Auto-Capture — 使用说明

## 一、这是什么

BMC Auto-Capture 是一个 BMC/SSH 自动化测试证据采集平台。
- 支持 BMC (Playwright 浏览器截图) 和 SSH (命令执行 + 终端截图)
- 配置通过 Excel + tasks.json 管理
- 支持顺序执行 (sequential) 和动态并发 (full) 两种模式

## 二、目录结构

解压后目录结构如下：

```
bmc-auto-capture/
├── 启动.bat              ← 双击运行
├── run.py                ← Python 源码入口（无 exe 时用 python run.py）
├── runtime/
│   ├── bmc-engine.exe    ← 执行引擎
│   ├── _internal/        ← Python 运行时 + 依赖库
│   └── playwright_browsers/  ← Chromium 浏览器（BMC 截图用）
├── app/
│   ├── src/              ← 源代码
│   ├── config/           ← YAML 配置文件
│   ├── examples/         ← Excel 模板
│   └── tasks.json        ← 任务定义
└── output/               ← 执行结果（自动生成）
```

## 三、使用方法

### 方式一：双击运行（推荐）

1. 双击 `启动.bat`
2. 菜单 `[5]` 设定你的 Excel 配置文件路径（支持拖拽）
3. 菜单 `[1]` 顺序执行（逐台设备，最稳定）
4. 等待完成，结果在 `output/` 目录

### 方式二：命令行直接执行

```cmd
启动.bat --excel "C:\path\to\your_tasks.xlsx" --mode full
```

或：

```cmd
runtime\bmc-engine.exe --app-dir app --excel "C:\path\to\your_tasks.xlsx" --mode sequential
```

## 四、Excel 配置要求

Excel 需包含两个工作表：

- **设备信息**：设备分类 / 设备名称 / 设备是否启用 / 带外管理IP / 带外管理用户名 / 带外管理密码 / 带内管理IP / 带内管理用户名 / 带内管理密码
- **任务列表**：任务序号 / 任务名称 / 任务类型 / 设备分组 / 输出目录模板 / 图片命名格式 / 是否启用

模板文件位于：`app/examples/task_template.xlsx`

## 五、RC 版本升级（仅更新脚本）

如果已下载过完整包（runtime 层），后续 RC 版本只需要：

1. 下载 `bmc-app-vX.X.X-RCXX.zip`
2. 解压到项目根目录，覆盖 `app/`、`run.py`、`启动.bat`
3. 不需要重新下载 runtime 层（240MB）

## 六、常用命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--excel` | Excel 配置文件路径 | 必填 |
| `--mode` | sequential（顺序）/ full（并发） | sequential |
| `--output` | 输出根目录 | YAML 配置 |
| `--max-bmc-workers` | BMC 最大并发数 | 4 |
| `--max-ssh-workers` | SSH 最大并发数 | 8 |
| `--preflight-only` | 仅预检，不执行 | — |
| `--verbose` | 调试模式 | — |

## 七、结果查看

执行完成后在 `output/` 目录生成：

- `final_result.csv` — 执行结果汇总
- `failure_detail.csv` — 失败明细
- `summary_pivot.csv` — 设备×任务透视表
- `{设备分组}/{设备名称}/{任务名称}/` — 各任务截图/日志

## 八、常见问题

**Q: 双击启动.bat 闪退**
A: 右键用管理员运行 CMD，cd 到项目目录后手动执行 `启动.bat`，查看错误信息。

**Q: 提示"找不到执行引擎"**
A: 确保 `runtime/bmc-engine.exe` 存在。如果是从源码运行，需要 Python 3.9+ 和 `pip install -r requirements.txt`。

**Q: BMC 任务全部失败**
A: 检查网络连通性 — 首先确认设备 BMC IP 可达（TCP 443 端口）。可以使用菜单 `[3]` 预检。

**Q: SSH 任务报 "Error reading SSH protocol banner"**
A: 设备不可达或 SSH 端口（22）不通。先用 `[3]` 预检确认。
