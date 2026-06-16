"""Write all run-level result artifacts."""

from __future__ import annotations

import logging
from typing import Sequence

from ..models.execution_result import ExecutionResult
from ..utils.sensitive import redact_sensitive_text
from .collector import compute_summary, write_final_result_csv, write_result_csv
from .summary import build_pivot_csv, print_terminal_summary, write_failure_csv

logger = logging.getLogger("bmc_auto_capture.result_writer")


class ResultWriter:
    """Writes CSV summaries, timing reports, and evidence audit artifacts."""

    def write(
        self,
        results: Sequence[ExecutionResult],
        output_dir: str,
        *,
        execution_started_at: float | None = None,
        execution_id: str = "",
        stop_metadata: dict | None = None,
        emit_terminal_summary: bool = True,
    ) -> dict:
        try:
            from .evidence_audit import attach_evidence_audit_checks

            attach_evidence_audit_checks(results)
        except Exception as e:
            logger.warning("Failed to attach evidence audit checks: %s", redact_sensitive_text(str(e)))

        write_result_csv(results, output_dir)
        write_final_result_csv(results, output_dir)

        try:
            build_pivot_csv(results, output_dir)
        except Exception as e:
            logger.warning("Failed to build pivot table: %s", redact_sensitive_text(str(e)))

        try:
            write_failure_csv(results, output_dir)
        except Exception as e:
            logger.warning("Failed to write connectivity summary: %s", redact_sensitive_text(str(e)))

        try:
            from .timing import write_all_timing_reports

            write_all_timing_reports(
                results,
                output_dir,
                execution_started_at=execution_started_at,
                execution_id=execution_id,
                stop_metadata=stop_metadata,
            )
        except Exception as e:
            logger.warning("Failed to write timing reports: %s", redact_sensitive_text(str(e)))

        try:
            from .evidence_audit import write_evidence_audit_csv

            write_evidence_audit_csv(results, output_dir)
        except Exception as e:
            logger.warning("Failed to write evidence audit: %s", redact_sensitive_text(str(e)))

        summary = compute_summary(results)
        logger.info("汇总:  %s", summary)
        if emit_terminal_summary:
            print_terminal_summary(results)
        return summary
