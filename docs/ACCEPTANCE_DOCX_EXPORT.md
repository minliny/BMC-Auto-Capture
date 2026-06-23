# 验收 DOCX 回填与证据打包

执行结果生成后，可以从项目内置验收模板创建副本，并把 `final_result.csv`
中的截图证据回填到第 4 章对应用例的“测试结果”框中。

## 默认模板

内置模板路径：

```text
templates/acceptance/Atlas 900 A3 SuperPoD 超节点 验收测试指南 03.docx
```

导出时不会修改模板原件，只会在输出目录创建 `_filled.docx` 副本。

## 默认输出目录

默认输出目录为本次执行的时间戳目录：

```text
output/<timestamp>/
```

生成文件：

```text
output/<timestamp>/
├── Atlas 900 A3 SuperPoD 超节点 验收测试指南 03_filled.docx
├── Atlas 900 A3 SuperPoD 超节点 验收测试指南 03_evidence.zip
└── Atlas 900 A3 SuperPoD 超节点 验收测试指南 03_fill_report.json
```

## 任务匹配规则

优先按任务名称匹配：

1. 文档用例标题与执行结果 `任务名称` 完全一致。
2. 如果一个文档用例拆成多个执行任务，匹配 `文档用例标题-子场景`。
3. 不按用例编号模糊猜测；未匹配项写入 `fill_report.json`。
   - `unmatched_case_details`：模板中有、执行结果未匹配到的用例。
   - `unmatched_result_tasks`：执行结果中有、模板未匹配到的任务。

例如文档用例 `计算节点部件信息查询测试` 会匹配：

```text
计算节点部件信息查询测试-CPU
计算节点部件信息查询测试-NPU
计算节点部件信息查询测试-内存
```

同一文档用例框内，每个子场景插入一张主截图。

## 默认截图路径

Excel 任务模板和任务默认值使用：

```text
输出目录：{任务序号}_{任务名称}/{设备分类}
截图名称：{TaskIP}-{任务名称}
```

手动导入已有截图时，也兼容历史目录名：

```text
{任务序号}.{任务名称}/{设备分类}
```

`{TaskIP}` 是智能 IP：

- BMC 任务使用带外管理 IP。
- SSH/TELNET 任务使用带内管理 IP。

示例：

```text
output/20260623_103000/
└── 4.1.10_NPU驱动和固件安装测试/
    └── A3/
        ├── 10.10.1.10-NPU驱动和固件安装测试.png
        └── 10.10.1.10-NPU驱动和固件安装测试.txt
```

## 证据 ZIP 规则

证据 ZIP 会剥掉 `output/<timestamp>/`，只保留执行目录下级结构：

```text
4.1.10_NPU驱动和固件安装测试/A3/10.10.1.10-NPU驱动和固件安装测试.png
4.1.10_NPU驱动和固件安装测试/A3/10.10.1.10-NPU驱动和固件安装测试.txt
```

ZIP 只包含：

- 图片：`.png`、`.jpg`、`.jpeg`
- SSH/TELNET 任务的 `.txt`

不包含 CSV、HTML、MHTML、state JSON 或 log。

## 使用方式

对已有执行目录生成：

```bash
python run.py --acceptance-docx --acceptance-run-output output/20260623_103000
```

只选择部分已执行证据目录生成：

```bash
python run.py --acceptance-docx \
  --acceptance-evidence-dirs \
  output/20260623_103000/4.2.4_计算节点部件信息查询测试-CPU/A3 \
  output/20260623_103000/4.2.4_计算节点部件信息查询测试-NPU/A3
```

也可以重复使用单目录参数：

```bash
python run.py --acceptance-docx \
  --acceptance-evidence-dir output/20260623_103000/4.2.4_计算节点部件信息查询测试-CPU/A3 \
  --acceptance-evidence-dir output/20260623_103000/4.2.4_计算节点部件信息查询测试-NPU/A3
```

选择的目录可以是：

- 整个 `output/<timestamp>` 目录。
- 某个 `任务序号_任务名称` 目录。
- 某个任务下的 `设备分类` 目录。

执行任务后自动生成：

```bash
python run.py --excel examples/task_template.xlsx --acceptance-docx
```

Windows `启动.bat` 中：

- 选择 `[1] 顺序执行` 或 `[2] 并发执行` 后，会先询问本次执行完成后是否生成测试用例报告。
- 菜单 `[8] 使用已有截图手动生成测试用例报告` 支持拖拽一个或多个已执行截图目录后手动生成。

可选覆盖模板或输出目录：

```bash
python run.py --acceptance-docx \
  --acceptance-run-output output/20260623_103000 \
  --acceptance-template path/to/template.docx \
  --acceptance-output-dir path/to/export
```
