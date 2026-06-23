from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

from PIL import Image
from docx import Document

from src.out.acceptance_docx import generate_acceptance_artifacts


def _write_template(path: Path) -> None:
    doc = Document()
    doc.add_heading("测试用例及测试记录", level=1)
    doc.add_heading("管理功能测试", level=2)
    doc.add_heading("计算节点部件信息查询测试", level=3)
    table = doc.add_table(rows=8, cols=2)
    labels = ["用例编号", "测试目的", "测试组网", "预置条件", "测试步骤", "预期结果", "测试结果", "备注"]
    values = ["4.2.4", "目的", "NA", "", "", "", "", ""]
    for idx, label in enumerate(labels):
        table.rows[idx].cells[0].text = label
        table.rows[idx].cells[1].text = values[idx]

    doc.add_heading("测试结果分析", level=1)
    summary = doc.add_table(rows=2, cols=4)
    for idx, value in enumerate(["测试类别", "用例编号", "用例名称", "测试结果 （pass/fail/NT）"]):
        summary.rows[0].cells[idx].text = value
    for idx, value in enumerate(["管理功能测试", "4.2.4", "计算节点部件信息查询测试", ""]):
        summary.rows[1].cells[idx].text = value
    doc.save(path)


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), color).save(path)


def test_generate_acceptance_docx_and_evidence_zip(tmp_path: Path):
    run_output = tmp_path / "output" / "20260623_103000"
    template = tmp_path / "template.docx"
    _write_template(template)

    cpu_png = run_output / "4.2.4.计算节点部件信息查询测试-CPU" / "A3" / "10.0.0.1-计算节点部件信息查询测试-CPU.png"
    npu_png = run_output / "4.2.4.计算节点部件信息查询测试-NPU" / "A3" / "10.0.0.1-计算节点部件信息查询测试-NPU.png"
    ssh_png = run_output / "4.1.10.NPU驱动和固件安装测试" / "A3" / "10.0.0.2-NPU驱动和固件安装测试.png"
    ssh_txt = run_output / "4.1.10.NPU驱动和固件安装测试" / "A3" / "10.0.0.2-NPU驱动和固件安装测试.txt"
    _write_png(cpu_png, (200, 20, 20))
    _write_png(npu_png, (20, 200, 20))
    _write_png(ssh_png, (20, 20, 200))
    ssh_txt.parent.mkdir(parents=True, exist_ok=True)
    ssh_txt.write_text("npu-smi info\nOK\n", encoding="utf-8")

    csv_path = run_output / "final_result.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["任务序号", "任务名称", "任务类型", "设备分类", "设备名称", "执行状态", "最终结论", "执行失败原因", "截图路径", "文本路径"])
        writer.writerow(["4.2.4", "计算节点部件信息查询测试-CPU", "BMC", "A3", "A3-01", "EXEC_SUCCESS", "PASS", "", str(cpu_png), ""])
        writer.writerow(["4.2.4", "计算节点部件信息查询测试-NPU", "BMC", "A3", "A3-01", "EXEC_SUCCESS", "PASS", "", str(npu_png), ""])
        writer.writerow(["4.1.10", "NPU驱动和固件安装测试", "SSH", "A3", "A3-01", "EXEC_SUCCESS", "PASS", "", str(ssh_png), str(ssh_txt)])

    result = generate_acceptance_artifacts(
        run_output=run_output,
        template_path=template,
        output_dir=run_output,
    )

    assert result.docx_path.exists()
    assert result.evidence_zip_path.exists()
    assert result.report_path.exists()
    assert result.matched_cases == 1
    assert result.packaged_files == 4

    filled = Document(result.docx_path)
    text = "\n".join(paragraph.text for paragraph in filled.paragraphs)
    for table in filled.tables:
        for row in table.rows:
            for cell in row.cells:
                text += "\n" + cell.text
    assert "测试结果：PASS" in text
    assert "证据1：计算节点部件信息查询测试-CPU" in text
    assert "证据2：计算节点部件信息查询测试-NPU" in text

    with zipfile.ZipFile(result.evidence_zip_path) as zf:
        names = sorted(zf.namelist())
    assert "4.2.4.计算节点部件信息查询测试-CPU/A3/10.0.0.1-计算节点部件信息查询测试-CPU.png" in names
    assert "4.2.4.计算节点部件信息查询测试-NPU/A3/10.0.0.1-计算节点部件信息查询测试-NPU.png" in names
    assert "4.1.10.NPU驱动和固件安装测试/A3/10.0.0.2-NPU驱动和固件安装测试.png" in names
    assert "4.1.10.NPU驱动和固件安装测试/A3/10.0.0.2-NPU驱动和固件安装测试.txt" in names
    assert all(not name.startswith("output/") for name in names)
    assert all("20260623_103000" not in name for name in names)

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["cases"][0]["matched_tasks"] == [
        "计算节点部件信息查询测试-CPU",
        "计算节点部件信息查询测试-NPU",
    ]


def test_generate_acceptance_docx_filters_selected_evidence_dirs(tmp_path: Path):
    run_output = tmp_path / "output" / "20260623_103000"
    template = tmp_path / "template.docx"
    _write_template(template)

    cpu_dir = run_output / "4.2.4.计算节点部件信息查询测试-CPU" / "A3"
    npu_dir = run_output / "4.2.4.计算节点部件信息查询测试-NPU" / "A3"
    cpu_png = cpu_dir / "10.0.0.1-计算节点部件信息查询测试-CPU.png"
    npu_png = npu_dir / "10.0.0.1-计算节点部件信息查询测试-NPU.png"
    _write_png(cpu_png, (200, 20, 20))
    _write_png(npu_png, (20, 200, 20))

    csv_path = run_output / "final_result.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["任务序号", "任务名称", "任务类型", "设备分类", "设备名称", "执行状态", "最终结论", "执行失败原因", "截图路径", "文本路径"])
        writer.writerow(["4.2.4", "计算节点部件信息查询测试-CPU", "BMC", "A3", "A3-01", "EXEC_SUCCESS", "PASS", "", str(cpu_png), ""])
        writer.writerow(["4.2.4", "计算节点部件信息查询测试-NPU", "BMC", "A3", "A3-01", "EXEC_SUCCESS", "PASS", "", str(npu_png), ""])

    result = generate_acceptance_artifacts(
        evidence_dirs=[cpu_dir],
        template_path=template,
        output_dir=run_output,
    )

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["run_output"] == str(run_output.resolve())
    assert report["cases"][0]["matched_tasks"] == ["计算节点部件信息查询测试-CPU"]
    assert result.packaged_files == 1

    with zipfile.ZipFile(result.evidence_zip_path) as zf:
        names = sorted(zf.namelist())
    assert names == [
        "4.2.4.计算节点部件信息查询测试-CPU/A3/10.0.0.1-计算节点部件信息查询测试-CPU.png"
    ]


def test_generate_acceptance_docx_synthesizes_rows_without_result_csv(tmp_path: Path):
    run_output = tmp_path / "output" / "manual"
    template = tmp_path / "template.docx"
    _write_template(template)

    cpu_dir = run_output / "4.2.4.计算节点部件信息查询测试-CPU" / "A3"
    cpu_png = cpu_dir / "10.0.0.1-计算节点部件信息查询测试-CPU.png"
    _write_png(cpu_png, (200, 20, 20))

    result = generate_acceptance_artifacts(
        evidence_dirs=[cpu_dir],
        template_path=template,
        output_dir=run_output,
    )

    assert result.matched_cases == 1
    assert result.packaged_files == 1
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["cases"][0]["matched_tasks"] == ["计算节点部件信息查询测试-CPU"]
