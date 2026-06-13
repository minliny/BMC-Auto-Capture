# BMC Auto Capture - 统一启动入口使用说明

## 一、概述

`启动.cmd` 是 BMC Auto Capture 的统一入口，用户只需双击此文件即可启动工具。

## 二、文件结构

```
bmc-auto-capture/
├── 启动.cmd              # 统一启动入口 (用户双击)
├── src/
│   └── cli/
│       ├── __init__.py
│       └── launcher.py   # Python launcher 逻辑
├── run.py               # 原有入口 (保持兼容)
├── dist/                # release 包目录
└── ...
```

新增 SSH/BMC 任务时，请先阅读 [任务添加指南](TASK_ADDING_GUIDE.md)。

## 三、CMD 中文适配

`启动.cmd` 会自动设置以下环境变量：

| 环境变量 | 说明 |
|----------|------|
| `chcp 65001` | 设置 CMD 编码为 UTF-8 |
| `PYTHONUTF8=1` | Python UTF-8 模式 |
| `PYTHONIOENCODING=utf-8` | Python 标准输出编码 |
| `LANG=zh_CN.UTF-8` | 语言设置 |
| `LC_ALL=zh_CN.UTF-8` | 本地化设置 |

## 四、启动流程

1. **环境检测** - 检测是 release 包还是源码包
2. **依赖检查** - 检查 tasks.json、Playwright 等
3. **Excel 配置** - 提示用户输入或拖拽 Excel 文件
4. **输出目录** - 自动生成或用户指定
5. **风险检查** - 检测高风险任务并拦截
6. **网络检测** - 可选的网络连通性检测
7. **计划确认** - 显示执行计划并等待确认
8. **任务执行** - 调用 run.py 或 bmc-engine.exe
9. **结果汇总** - 显示执行结果统计

## 五、命令行参数

```bash
# 基本用法
启动.bat --excel 设备任务表.xlsx

# 指定输出目录
启动.bat --excel 设备任务表.xlsx --output my_output

# 指定并发数（>1 会进入 full 动态调度，并同时映射 BMC/SSH worker）
启动.bat --excel 设备任务表.xlsx --concurrency 3

# 推荐显式写法
runtime\bmc-engine.exe --app-dir app --excel 设备任务表.xlsx --mode full --max-bmc-workers 4 --max-ssh-workers 20

# BMC 快速证据模式：只保存最终 PNG + HTML
runtime\bmc-engine.exe --app-dir app --excel 设备任务表.xlsx --mode full --bmc-artifact-profile fast

# 严格模式 (网络检测失败则中止)
启动.bat --excel 设备任务表.xlsx --strict

# 仅预检查
启动.bat --excel 设备任务表.xlsx --precheck-only

# 跳过确认直接执行
启动.bat --excel 设备任务表.xlsx --yes

# 显示帮助
启动.bat --help
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--excel`, `-e` | Excel 文件路径 | 交互式输入 |
| `--output`, `-o` | 输出目录 | `runtime/output/yyyyMMdd_HHmmss` |
| `--concurrency`, `-c` | 兼容并发数；大于 1 时进入 full，并映射缺省的 BMC/SSH worker | 1 |
| `--bmc-artifact-profile` | BMC 证据模式；`full` 保存完整证据，`fast` 只保存 PNG/HTML | `full` |
| `--strict` | 严格模式 (网络失败中止) | False |
| `--precheck-only` | 仅预检查，不执行 | False |
| `--yes`, `-y` | 跳过确认直接执行 | False |

## 六、交互式输入

如果未指定 `--excel` 参数，launcher 会：

1. 显示提示信息 `[信息] 请输入 Excel 文件路径 (可直接拖拽文件到窗口)`
2. 等待用户输入或拖拽文件到 CMD 窗口
3. 验证文件存在和扩展名
4. 读取并显示 Excel 摘要

**拖拽输入示例：**
```
Excel 路径: C:\Users\xxx\Documents\设备任务表.xlsx
```

## 七、输出目录

默认输出目录格式：
```
runtime/output/yyyyMMdd_HHmmss/
```

例如：`runtime/output/20240601_143025/`

包含文件：
- `result.csv` - 任务执行结果
- `final_result.csv` - 最终汇总
- `failure_detail.csv` - 失败详情
- PNG/HTML/TXT 证据文件

## 八、高风险任务拦截

Launcher 会自动检查以下高风险任务：

- `拔插硬盘后清除Foreign配置`
- `foreignConfig`
- `清除Foreign配置`

如果检测到这些任务，会显示错误并停止执行：
```
[失败] 检测到高风险状态变更任务，已停止执行
[失败] 请从任务配置中删除以下项后重试:
  - tasks.json: xxx任务 (foreignConfig)
```

## 九、网络连通性检测

执行前会检测设备连通性，输出表格：

```
设备名称              设备分组        BMC IP           BMC       带内IP           SSH
-------------------------------------------------------------------------------------
服务器A             计算节点       10.10.10.10     可达       192.168.1.10     可达
服务器B             计算节点       10.10.10.11     不可达     192.168.1.11     可达
-------------------------------------------------------------------------------------
共检测 2 个设备
```

## 十、执行计划确认

显示计划摘要后等待用户确认：

```
============================================================
  七、执行计划确认
============================================================

启用设备数量: 5
启用任务数量: 20
预计输出目录: runtime/output/20240601_143025
并发数: 1

风险检查: 未发现

是否开始执行? (Y/N/P): Y
```

- `Y` - 开始执行
- `N` - 取消执行
- `P` - 仅做预检查，不执行任务

## 十一、结果汇总

执行完成后显示：

```
============================================================
  十、结果汇总
============================================================

result.csv 路径: runtime/output/20240601_143025/result.csv
总任务数: 100
成功数: 95
失败数: 3
部分成功数: 2
跳过数: 0
PNG 数量: 100
HTML 数量: 100
TXT 数量: 50

工件状态分布:
  ARTIFACT_SAVED: 97
  ARTIFACT_PARTIAL: 3

就绪状态分布:
  READY_OK: 95
  READY_NOT_READY: 5
```

## 十二、常见问题

### Q1: 中文显示乱码
**A:** 确保 CMD 窗口字体设置为「点阵字体」或「Lucida Console」，或使用 Windows Terminal。

### Q2: 找不到 Python
**A:** 确保已安装 Python 3.9+ 并添加到 PATH，或使用 venv 虚拟环境。

### Q3: 提示 openpyxl 未安装
**A:** 执行 `pip install openpyxl`

### Q4: 提示 Playwright 未安装
**A:** 执行 `pip install playwright && python -m playwright install chromium`

## 十三、原始入口兼容性

`启动.cmd` 不会破坏原有的 `run.py` 入口，用户仍可直接：

```bash
# Windows
python run.py --excel 设备任务表.xlsx

# macOS/Linux
python3 run.py --excel 设备任务表.xlsx
```

## 十四、日志文件

日志文件位于 `runtime/output/<timestamp>/app.log`

日志使用 UTF-8 编码，包含完整的执行过程记录。
