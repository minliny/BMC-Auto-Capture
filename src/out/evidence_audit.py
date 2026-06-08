"""
Post-execution evidence audit — scans saved HTML/MHTML/log files for
session anomalies and generates evidence_audit.csv.

Run standalone:
  python -m src.out.evidence_audit <output_dir>
"""

from __future__ import annotations

import csv
import logging
import os
from typing import Sequence

from ..models.execution_result import ExecutionResult
from ..executor.bmc_health_check import (
    scan_html_for_keywords,
    scan_mhtml_for_keywords,
    check_evidence_files,
)

logger = logging.getLogger("bmc_auto_capture.evidence_audit")

# ---------------------------------------------------------------------------
# Audit one plan's evidence
# ---------------------------------------------------------------------------

def audit_plan_evidence(result: ExecutionResult) -> dict:
    """Audit the saved evidence files for a single plan.

    Returns a dict with all evidence_audit.csv fields.
    """
    audit = {
        "execution_id": result.plan_id,  # execution_id not yet wired
        "plan_id": result.plan_id,
        "device_name": result.device_name,
        "task_name": result.task_name,
        "endpoint_key": result.endpoint_key,
        "result_status": result.execution_status,
        "final_verdict": result.final_verdict,
        "evidence_status": "OK",
        "evidence_reason": "",
        "matched_keyword": "",
        "screenshot_path": "",
        "html_path": "",
        "mhtml_path": "",
        "log_path": "",
        "url": "",
        "title": "",
        "screenshot_size": 0,
        "html_size": 0,
        "mhtml_size": 0,
    }

    out_dir = result.output_dir

    # Collect evidence paths
    if result.screenshots:
        audit["screenshot_path"] = ";".join(result.screenshots)
    audit["html_path"] = result.html_file
    audit["log_path"] = result.log_file

    # Find MHTML
    html_subdir = os.path.join(out_dir, "html") if out_dir else ""
    if os.path.isdir(html_subdir):
        for fname in os.listdir(html_subdir):
            if fname.endswith(".mhtml"):
                audit["mhtml_path"] = os.path.join(html_subdir, fname)
                break

    # Check file presence/sizes
    if audit["screenshot_path"]:
        for sp in result.screenshots:
            if os.path.exists(sp):
                audit["screenshot_size"] = max(audit["screenshot_size"], os.path.getsize(sp))
    if not audit["screenshot_path"] or audit["screenshot_size"] < 500:
        audit["evidence_status"] = "SCREENSHOT_MISSING"
        audit["evidence_reason"] = "No valid screenshot found"

    # Scan HTML
    html_scan = scan_html_for_keywords(audit["html_path"])
    audit["html_size"] = html_scan["html_size"]
    if not html_scan["healthy"]:
        audit["evidence_status"] = html_scan["status"]
        audit["matched_keyword"] = html_scan["matched_keyword"]
        audit["evidence_reason"] = f"HTML scan failed: {html_scan['status']}"
        return audit

    # Scan MHTML
    mhtml_scan = scan_mhtml_for_keywords(audit["mhtml_path"])
    audit["mhtml_size"] = mhtml_scan["mhtml_size"]
    if not mhtml_scan["healthy"] and mhtml_scan["status"] != "MHTML_MISSING":
        # MHTML has issue; use it only if HTML didn't already catch something
        if audit["evidence_status"] == "OK":
            audit["evidence_status"] = mhtml_scan["status"]
            audit["matched_keyword"] = mhtml_scan["matched_keyword"]
            audit["evidence_reason"] = f"MHTML scan failed: {mhtml_scan['status']}"

    return audit


def audit_all(results: Sequence[ExecutionResult]) -> list[dict]:
    """Audit evidence for all results. Returns list of audit dicts."""
    audits = []
    for r in results:
        try:
            audits.append(audit_plan_evidence(r))
        except Exception as e:
            audits.append({
                "plan_id": r.plan_id,
                "device_name": r.device_name,
                "task_name": r.task_name,
                "result_status": r.execution_status,
                "evidence_status": "AUDIT_ERROR",
                "evidence_reason": str(e)[:200],
                "matched_keyword": "",
            })
    return audits


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

EVIDENCE_AUDIT_HEADER = [
    "execution_id", "plan_id", "device_name", "task_name",
    "endpoint_key",
    "result_status", "final_verdict",
    "evidence_status", "evidence_reason", "matched_keyword",
    "screenshot_path", "html_path", "mhtml_path", "log_path",
    "url", "title",
    "screenshot_size", "html_size", "mhtml_size",
]


def write_evidence_audit_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "evidence_audit.csv",
) -> str:
    """Generate evidence_audit.csv from execution results."""
    path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    audits = audit_all(results)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(EVIDENCE_AUDIT_HEADER)
        for a in sorted(audits, key=lambda a: (a.get("device_name", ""), a.get("task_name", ""))):
            writer.writerow([a.get(k, "") for k in EVIDENCE_AUDIT_HEADER])

    # Count anomalies
    ok = sum(1 for a in audits if a["evidence_status"] == "OK")
    bad = len(audits) - ok
    logger.info(
        "Wrote evidence_audit.csv: %d OK, %d anomalies → %s",
        ok, bad, path,
    )

    # Print summary of anomalies
    if bad > 0:
        print(f"\n  Evidence Audit: {ok} OK, {bad} anomalies")
        by_status: dict[str, int] = {}
        for a in audits:
            s = a["evidence_status"]
            if s != "OK":
                by_status[s] = by_status.get(s, 0) + 1
        for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
            print(f"    {status}: {count}")
        # List tasks where result is SUCCESS but evidence is bad (false positives)
        false_positives = [
            a for a in audits
            if a["result_status"] == "EXEC_SUCCESS" and a["evidence_status"] != "OK"
        ]
        if false_positives:
            print(f"\n  FALSE POSITIVES (result=SUCCESS, evidence=BAD): {len(false_positives)}")
            for a in false_positives:
                print(f"    {a['device_name']} / {a['task_name']} → {a['evidence_status']}: {a['evidence_reason'][:80]}")

    return path


def write_evidence_audit_summary(
    results: Sequence[ExecutionResult],
    output_dir: str,
) -> dict:
    """Return summary counts for evidence audit."""
    audits = audit_all(results)
    ok = sum(1 for a in audits if a["evidence_status"] == "OK")
    false_pos = sum(
        1 for a in audits
        if a["result_status"] == "EXEC_SUCCESS" and a["evidence_status"] != "OK"
    )
    return {
        "total": len(audits),
        "evidence_ok": ok,
        "evidence_bad": len(audits) - ok,
        "false_positives": false_pos,
        "by_status": _count_by_status(audits),
    }


def _count_by_status(audits: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in audits:
        s = a["evidence_status"]
        counts[s] = counts.get(s, 0) + 1
    return counts
