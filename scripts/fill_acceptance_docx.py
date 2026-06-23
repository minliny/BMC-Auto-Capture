#!/usr/bin/env python3
"""Generate an acceptance DOCX and evidence ZIP from an output run directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.out.acceptance_docx import generate_acceptance_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据执行结果回填验收 DOCX 并打包证据。")
    parser.add_argument("--run-output", default=None, help="包含 final_result.csv 的执行输出目录")
    parser.add_argument("--evidence-dir", action="append", default=[],
                        help="已执行证据目录；可重复传入")
    parser.add_argument("--evidence-dirs", nargs="+", default=None,
                        help="一个或多个已执行证据目录")
    parser.add_argument("--template", default=None, help="DOCX 模板路径；默认使用项目内置模板")
    parser.add_argument("--output-dir", default=None, help="导出目录；默认使用执行输出目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = generate_acceptance_artifacts(
        run_output=args.run_output,
        evidence_dirs=[*args.evidence_dir, *(args.evidence_dirs or [])],
        template_path=args.template,
        output_dir=args.output_dir,
        app_dir=ROOT,
    )
    print(f"DOCX: {result.docx_path}")
    print(f"ZIP : {result.evidence_zip_path}")
    print(f"报告: {result.report_path}")
    print(
        f"已匹配: {result.matched_cases}, 未匹配: {result.unmatched_cases}, "
        f"已打包: {result.packaged_files}, 缺失: {result.missing_files}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
