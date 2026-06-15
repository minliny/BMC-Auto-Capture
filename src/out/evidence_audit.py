"""
Post-execution evidence audit — scans saved evidence files per task type.

BMC tasks expect: png, html/evidence_html, mhtml, state_json, page_health_debug
SSH/TELNET tasks expect: txt, log, (png if rendered), no html/mhtml required.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from typing import Sequence

from ..utils.path_safety import safe_join_under_root, is_safe_path_component

from ..models.execution_result import ExecutionResult
from ..executor.bmc_health_check import (
    scan_html_for_keywords,
    scan_mhtml_for_keywords,
    check_evidence_files,
)

logger = logging.getLogger("bmc_auto_capture.evidence_audit")


EVIDENCE_AUDIT_HEADER = [
    "execution_id", "plan_id", "device_name", "task_name", "task_type",
    "endpoint_key",
    "result_status", "final_verdict",
    "evidence_expected_type",
    "evidence_status", "evidence_reason", "matched_keyword",
    "screenshot_path", "html_path", "mhtml_path", "log_path",
    "url", "title",
    "screenshot_size", "html_size", "mhtml_size",
    "opened_status", "authenticated_status", "page_basic_health_status",
    "ready_for_capture_status", "screenshot_status",
    "page_health_gate", "page_health_reason",
    "page_health_debug_path",
    "matched_selector", "matched_text", "is_visible",
]


def _expected_evidence(task_type: str) -> str:
    t = task_type.upper()
    if t in ("BMC",):
        return "png,html,mhtml,state_json"
    if t in ("SSH", "TELNET"):
        return "txt,log[,png]"
    return "unknown"


def _ssh_has_command_output(content: str) -> bool:
    """Check if SSH output contains actual command execution evidence.

    Returns False if the content looks like ONLY a login banner with no
    command echo or command output (prompt-only sessions).
    """
    if not content or len(content.strip()) < 100:
        return False

    # Common patterns that indicate ONLY login (not command execution):
    # - "login:" / "Password:" prompts
    # - SSH banner lines ("The max number of VTY users...")
    # - Device prompt with no trailing command output

    # Check for command-execution indicators:
    # - Lines longer than typical prompts (command output is usually wide)
    # - Common command-output patterns (tables, headers like "---", interface stats)
    # - Error messages from commands
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    non_banner_lines = 0
    for line in lines:
        # Skip typical login/banner lines
        lower = line.lower()
        if any(kw in lower for kw in (
            "login:", "password:", "the max number of vty",
            "the current login time", "the last login time",
            "copyright", "all rights reserved", "warning:",
        )):
            continue
        # Skip short prompt-only lines (e.g. "<DeviceName>")
        if len(line) < 15 and (line.startswith("<") or line.startswith("[") or line.startswith("~")):
            continue
        non_banner_lines += 1

    return non_banner_lines >= 3


def _read_page_health_debug(output_dir: str) -> dict | None:
    if not output_dir:
        return None
    path = safe_join_under_root(output_dir, "page_health_debug.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def audit_plan_evidence(result: ExecutionResult) -> dict:
    audit = {k: "" for k in EVIDENCE_AUDIT_HEADER}
    audit["plan_id"] = result.plan_id
    audit["execution_id"] = result.plan_id
    audit["device_name"] = result.device_name
    audit["task_name"] = result.task_name
    audit["task_type"] = result.task_type
    audit["endpoint_key"] = result.endpoint_key
    audit["result_status"] = result.execution_status
    audit["final_verdict"] = result.final_verdict
    audit["evidence_expected_type"] = _expected_evidence(result.task_type)

    out_dir = result.output_dir

    # Screenshot paths
    if result.screenshots:
        audit["screenshot_path"] = ";".join(result.screenshots)
    audit["html_path"] = result.html_file
    audit["log_path"] = result.log_file

    # MHTML
    html_subdir = os.path.join(out_dir, "html") if out_dir else ""
    if os.path.isdir(html_subdir):
        for fname in os.listdir(html_subdir):
            if fname.endswith(".mhtml"):
                audit["mhtml_path"] = os.path.join(html_subdir, fname)
                break

    task_type = result.task_type.upper()

    # BMC-specific evidence check
    if task_type in ("BMC",):
        # Check screenshots
        if audit["screenshot_path"]:
            for sp in result.screenshots:
                if os.path.exists(sp):
                    audit["screenshot_size"] = str(max(
                        int(audit["screenshot_size"] or "0"), os.path.getsize(sp)))
        if not audit["screenshot_path"] or int(audit["screenshot_size"] or "0") < 500:
            audit["evidence_status"] = "SCREENSHOT_MISSING"
            audit["evidence_reason"] = "No valid screenshot for BMC task"

        # Check HTML
        html_scan = scan_html_for_keywords(audit["html_path"])
        audit["html_size"] = str(html_scan["html_size"])
        if not html_scan["healthy"]:
            audit["evidence_status"] = html_scan["status"]
            audit["matched_keyword"] = html_scan["matched_keyword"]
            audit["evidence_reason"] = f"HTML: {html_scan['status']}"

        # Check MHTML
        mhtml_scan = scan_mhtml_for_keywords(audit["mhtml_path"])
        audit["mhtml_size"] = str(mhtml_scan["mhtml_size"])
        if not mhtml_scan["healthy"] and mhtml_scan["status"] != "MHTML_MISSING":
            if audit["evidence_status"] == "OK":
                audit["evidence_status"] = mhtml_scan["status"]
                audit["matched_keyword"] = mhtml_scan["matched_keyword"]
                audit["evidence_reason"] = f"MHTML: {mhtml_scan['status']}"

        # HTML missing entirely
        if not audit["html_path"] or not os.path.exists(audit["html_path"]):
            if audit["evidence_status"] == "OK":
                audit["evidence_status"] = "HTML_MISSING"
                audit["evidence_reason"] = "No HTML file for BMC task"
        if audit["evidence_status"] == "HTML_MISSING" and result.execution_status != "EXEC_SUCCESS":
            audit["evidence_reason"] = (
                "HTML_MISSING explained by "
                f"result_status={result.execution_status}; "
                f"failure_reason={(result.execution_failure_reason or result.artifact_failure_reason or '')[:200]}"
            )

        # Read page_health_debug.json for gate results
        phd = _read_page_health_debug(out_dir)
        if phd:
            audit["page_health_debug_path"] = os.path.join(out_dir, "page_health_debug.json")
            all_gr = phd.get("all_gate_results", [])
            for gr in all_gr:
                gate = gr.get("gate", "")
                status = gr.get("severity", "")
                if gate == "OPENED":
                    audit["opened_status"] = status
                elif gate == "AUTHENTICATED":
                    audit["authenticated_status"] = status
                elif gate == "PAGE_BASIC_HEALTH":
                    audit["page_basic_health_status"] = status
                elif gate == "READY_FOR_CAPTURE":
                    audit["ready_for_capture_status"] = status
                elif gate == "SCREENSHOT_VALIDATED":
                    audit["screenshot_status"] = status
            audit["page_health_gate"] = phd.get("failed_gate", "")
            audit["page_health_reason"] = phd.get("reason", "")
            # Extract matched selector/text from the first non-pass gate
            for gr in all_gr:
                if gr.get("severity", "PASS") != "PASS":
                    audit["matched_selector"] = gr.get("matched_selector", "")
                    audit["matched_text"] = gr.get("matched_text", "")
                    audit["is_visible"] = str(gr.get("is_visible", ""))
                    break

        # false positive check: result SUCCESS but gate FAIL
        if result.execution_status == "EXEC_SUCCESS":
            gate_failed = phd and phd.get("failed_gate", "")
            if gate_failed:
                audit["evidence_status"] = f"FALSE_POSITIVE_GATE_FAIL:{gate_failed}"
                if not audit["evidence_reason"]:
                    audit["evidence_reason"] = phd.get("reason", "")

    # SSH/TELNET evidence check
    elif task_type in ("SSH", "TELNET"):
        txt_ok = bool(result.txt_file and os.path.exists(result.txt_file))
        log_ok = bool(result.log_file and os.path.exists(result.log_file))

        if not txt_ok and not log_ok:
            audit["evidence_status"] = "TXT_LOG_MISSING"
            if result.execution_status.startswith("EXEC_SKIPPED"):
                audit["evidence_reason"] = (
                    "TXT_LOG_MISSING explained by "
                    f"result_status={result.execution_status}; "
                    f"failure_reason={(result.execution_failure_reason or '')[:200]}"
                )
            else:
                audit["evidence_reason"] = "No TXT or log file for SSH/TELNET task"
        elif not txt_ok:
            audit["evidence_status"] = "TXT_MISSING"
            audit["evidence_reason"] = "No TXT file for SSH/TELNET task"
        else:
            # P0: check TXT content, not just existence
            try:
                txt_size = os.path.getsize(result.txt_file)
                audit["txt_size"] = str(txt_size)
            except OSError:
                txt_size = 0

            if txt_size < 50:
                audit["evidence_status"] = "TXT_EMPTY"
                audit["evidence_reason"] = f"TXT file is empty or too small ({txt_size} bytes)"
            else:
                # Read content and check for actual command output
                try:
                    with open(result.txt_file, "r", encoding="utf-8", errors="replace") as fh:
                        txt_content = fh.read()
                except Exception:
                    txt_content = ""

                # Check for login-only patterns (no command execution evidence)
                has_cmd_output = _ssh_has_command_output(txt_content)
                if not has_cmd_output:
                    audit["evidence_status"] = "ONLY_LOGIN_BANNER"
                    audit["evidence_reason"] = (
                        "SSH TXT contains only login banner/prompt, "
                        "no command echo or output detected"
                    )
                else:
                    audit["evidence_status"] = "OK"
                    audit["evidence_reason"] = f"TXT present ({txt_size} bytes, commands detected)"

        # Screenshot check for SSH
        if audit["screenshot_path"]:
            for sp in result.screenshots:
                if os.path.exists(sp):
                    try:
                        ss_size = os.path.getsize(sp)
                        audit["screenshot_size"] = str(ss_size)
                        if ss_size < 200:
                            audit["evidence_status"] = (
                                "IMAGE_BLANK" if audit["evidence_status"] == "OK"
                                else audit["evidence_status"] + ";IMAGE_BLANK"
                            )
                            audit["evidence_reason"] = (
                                str(audit["evidence_reason"]) + f"; Screenshot too small ({ss_size} bytes)"
                            )
                    except OSError:
                        pass

    if not audit["evidence_status"]:
        audit["evidence_status"] = "OK"

    return audit


def audit_all(results: Sequence[ExecutionResult]) -> list[dict]:
    audits = []
    for r in results:
        try:
            audits.append(audit_plan_evidence(r))
        except Exception as e:
            audits.append({"plan_id": r.plan_id, "device_name": r.device_name,
                           "task_name": r.task_name, "result_status": r.execution_status,
                           "evidence_status": "AUDIT_ERROR",
                           "evidence_reason": str(e)[:200]})
    return audits


def write_evidence_audit_csv(results: Sequence[ExecutionResult], output_dir: str,
                             filename: str = "evidence_audit.csv") -> str:
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)
    audits = audit_all(results)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(EVIDENCE_AUDIT_HEADER)
        for a in sorted(audits, key=lambda a: (a.get("device_name", ""), a.get("task_name", ""))):
            writer.writerow([a.get(k, "") for k in EVIDENCE_AUDIT_HEADER])

    ok = sum(1 for a in audits if a["evidence_status"] in ("OK", ""))
    bad = len(audits) - ok
    logger.info("Wrote evidence_audit.csv: %d OK, %d anomalies → %s", ok, bad, path)

    if bad > 0:
        by_status: dict[str, int] = {}
        for a in audits:
            s = a["evidence_status"]
            if s not in ("OK", ""):
                by_status[s] = by_status.get(s, 0) + 1
        for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
            print(f"    {status}: {count}")

        false_pos = [a for a in audits if a["result_status"] == "EXEC_SUCCESS"
                     and a["evidence_status"] not in ("OK", "")]
        if false_pos:
            print(f"\n  FALSE POSITIVES (result=SUCCESS, evidence=BAD): {len(false_pos)}")
            for a in false_pos:
                print(f"    {a['device_name']} / {a['task_name']} → "
                      f"{a['evidence_status']}: {a['evidence_reason'][:80]}")
    return path


def write_evidence_audit_summary(results, output_dir) -> dict:
    audits = audit_all(results)
    ok = sum(1 for a in audits if a["evidence_status"] in ("OK", ""))
    fp = sum(1 for a in audits if a["result_status"] == "EXEC_SUCCESS"
             and a["evidence_status"] not in ("OK", ""))
    return {"total": len(audits), "evidence_ok": ok, "evidence_bad": len(audits) - ok,
            "false_positives": fp}
