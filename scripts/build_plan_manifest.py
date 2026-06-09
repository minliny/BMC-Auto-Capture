#!/usr/bin/env python3
"""
Build a deterministic PlanManifest from Excel + validation.json.

Usage:
  python3 scripts/build_plan_manifest.py --excel input.xlsx --validation-json validation.json --out ./output/
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))


def main():
    parser = argparse.ArgumentParser(description="Build PlanManifest from Excel + validation.json")
    parser.add_argument("--excel", required=True, help="Path to Excel file")
    parser.add_argument("--validation-json", required=True, help="Path to validation.json")
    parser.add_argument("--out", default="./output", help="Output directory (default: ./output)")
    args = parser.parse_args()

    excel = args.excel
    vj = args.validation_json
    out_dir = args.out

    if not os.path.exists(excel):
        print(f"ERROR: Excel file not found: {excel}")
        sys.exit(1)
    if not os.path.exists(vj):
        print(f"ERROR: Validation JSON not found: {vj}")
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)

    from src.plan_catalog import PlanCatalogPlanner

    planner = PlanCatalogPlanner(excel, vj)
    manifest, catalog, report = planner.build()

    # Write manifest
    manifest_path = os.path.join(out_dir, "plan_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"[OK] plan_manifest.json → {manifest_path}")

    # Write catalog
    catalog_path = os.path.join(out_dir, "task_catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"[OK] task_catalog.json → {catalog_path}")

    # Write validation report
    report_path = os.path.join(out_dir, "validation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"[OK] validation_report.json → {report_path}")

    # Summary
    print()
    print(f"plan_id    : {manifest.plan_id}")
    print(f"plan_hash  : {manifest.plan_hash}")
    print(f"task_count : {manifest.task_count}")
    print(f"validation errors  : {report.error_count}")
    print(f"validation warnings: {report.warning_count}")

    if not report.is_valid:
        for e in report.errors:
            print(f"  [ERROR] {e.code}: {e.message} ({e.row_ref})")
        sys.exit(1)


if __name__ == "__main__":
    main()
