"""Callback delivery helpers for plan runs."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from ..callback_outbox import (
    CallbackOutbox,
    build_outbox_item_from_callback_body,
    build_outbox_summary_from_callback_body,
    classify_callback_error,
)
from ..plan_item_status_callback_client import (
    CallbackResult,
    HttpCallbackTransport,
    PlanItemStatusCallbackClient,
    build_callback_item,
    validate_callback_url,
)
from ..utils.sensitive import redact_sensitive_text, redact_url_for_log
from .models import PlanRun, PlanRunItem

logger = logging.getLogger("bmc_auto_capture.plan_run")


class CallbackDeliveryService:
    """Resolve, persist, and deliver plan-item status callbacks."""

    def __init__(self, workspace_root: str):
        self._workspace_root = workspace_root

    @staticmethod
    def validate_callback_url(url: str) -> dict[str, Any]:
        ok, reason = validate_callback_url(url)
        if ok:
            return {"ok": True}
        return {
            "ok": False,
            "reason": reason,
            "message": "callback.itemStatusUrl is not allowed by executor callback URL policy",
        }

    def resolve_callback_url(self, run: PlanRun) -> str:
        """Resolve callback URL using registry, request, env, then no callback."""
        registry_url = os.environ.get("EXECUTOR_MASTER_REGISTRY_URL", "")
        if registry_url:
            try:
                from ..server_registry_client import discover_callback_url

                discovered = discover_callback_url()
                if discovered:
                    check = self.validate_callback_url(discovered)
                    if not check.get("ok"):
                        logger.warning(
                            "Callback URL from registry rejected: %s",
                            check.get("reason", "INVALID_CALLBACK_URL"),
                        )
                        return ""
                    logger.info(
                        "Callback URL resolved via registry: %s",
                        redact_url_for_log(discovered),
                    )
                    return discovered
            except Exception as exc:
                logger.warning("CALLBACK_REGISTRY_RESOLVE_FAILED: %s", exc)

        if run.item_status_url:
            check = self.validate_callback_url(run.item_status_url)
            if not check.get("ok"):
                logger.warning(
                    "Callback URL from request rejected: %s",
                    check.get("reason", "INVALID_CALLBACK_URL"),
                )
                return ""
            logger.info(
                "Callback URL from request: %s",
                redact_url_for_log(run.item_status_url),
            )
            return run.item_status_url

        env_url = os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", "")
        if env_url:
            check = self.validate_callback_url(env_url)
            if not check.get("ok"):
                logger.warning(
                    "Callback URL from env rejected: %s",
                    check.get("reason", "INVALID_CALLBACK_URL"),
                )
                return ""
            logger.info("Callback URL from env: %s", redact_url_for_log(env_url))
            return env_url

        return ""

    def build_callback_body(self, run: PlanRun, item: PlanRunItem) -> dict[str, Any]:
        started_at_iso = (
            datetime.fromtimestamp(item.started_at, tz=timezone.utc).isoformat()
            if item.started_at else None
        )
        finished_at_iso = (
            datetime.fromtimestamp(item.finished_at, tz=timezone.utc).isoformat()
            if item.finished_at else None
        )
        return build_callback_item(
            plan_id=str(run.plan_id),
            task_id=item.task_id,
            plan_item_id=item.plan_item_id,
            device_group=item.device_group,
            device_name=item.device_name,
            task_name=item.task_name,
            status=item.status,
            updater=run.updater,
            error_message=item.error_message,
            started_at=started_at_iso,
            finished_at=finished_at_iso,
        )

    def deliver_item_status(
        self, run: PlanRun, item: PlanRunItem, cb: PlanItemStatusCallbackClient,
    ) -> None:
        """Persist and send one task status update keyed by planId."""
        callback_url = self.resolve_callback_url(run)
        plan_id = str(run.plan_id)
        url_configured = bool(callback_url and plan_id)
        if not callback_url:
            logger.info("CALLBACK_URL_NOT_CONFIGURED: no callback URL resolved")
        if not plan_id:
            logger.warning("CALLBACK_PLAN_ID_MISSING: plan_id is empty")

        outbox = CallbackOutbox(plan_id, workspace_root=self._workspace_root)
        cb_body = self.build_callback_body(run, item)
        outbox_item = build_outbox_item_from_callback_body(
            plan_id=cb_body["planId"],
            task_id=cb_body.get("taskId", ""),
            plan_item_id=cb_body.get("planItemId", ""),
            device_group=cb_body.get("deviceGroup", ""),
            device_name=cb_body["deviceName"],
            task_name=cb_body["taskName"],
            status=cb_body["status"],
            updater=cb_body["updater"],
            error_message=cb_body["errorMessage"],
            started_at=cb_body.get("startedAt"),
            finished_at=cb_body.get("finishedAt"),
            callback_url=callback_url if url_configured else "",
        )
        if not url_configured:
            outbox_item.delivery_status = "URL_NOT_CONFIGURED"
        outbox.append(outbox_item)
        transport_mode = "http" if isinstance(cb.transport, HttpCallbackTransport) else "fake"
        logger.info(
            "CallbackOutbox: item written for planId=%s deviceGroup=%s status=%s (url_configured=%s, transport=%s)",
            plan_id, item.device_group, item.status, url_configured, transport_mode,
        )

        if not url_configured:
            return

        logger.info(
            "callback item send start: internalRunId=%s planId=%s mode=%s url=%s",
            run.run_id, plan_id, run.callback_mode,
            redact_url_for_log(callback_url),
        )

        try:
            if run.callback_mode == "batch":
                result: CallbackResult = cb.send_batch(callback_url, [cb_body])
            else:
                result = cb.send_single(callback_url, cb_body)
            self.process_outbox_result(
                outbox, [outbox_item], result, callback_url, run.run_id,
            )
        except Exception as exc:
            logger.error(
                "callback item send failed: internalRunId=%s exception=%s",
                run.run_id, redact_sensitive_text(str(exc)[:200]),
            )

    def deliver_plan_summary(
        self, run: PlanRun, cb: PlanItemStatusCallbackClient,
    ) -> None:
        """Persist and send the final batch summary keyed by planId."""
        callback_url = self.resolve_callback_url(run)
        plan_id = str(run.plan_id)
        if not plan_id:
            return

        summary = run.summary
        url_configured = bool(callback_url)
        outbox = CallbackOutbox(plan_id, workspace_root=self._workspace_root)
        outbox_item = build_outbox_summary_from_callback_body(
            plan_id=plan_id,
            summary=summary,
            callback_url=callback_url if url_configured else "",
        )
        if not url_configured:
            outbox_item.delivery_status = "URL_NOT_CONFIGURED"
        outbox.append(outbox_item)
        if not url_configured:
            logger.info("CALLBACK_URL_NOT_CONFIGURED: final summary stored but no callback URL resolved")
            return

        logger.info(
            "callback summary send start: internalRunId=%s planId=%s url=%s",
            run.run_id, plan_id, redact_url_for_log(callback_url),
        )
        try:
            result = cb.send_summary(callback_url, plan_id, summary)
            self.process_outbox_result(
                outbox, [outbox_item], result, callback_url, run.run_id,
            )
        except Exception as exc:
            logger.error(
                "callback summary send failed: internalRunId=%s exception=%s",
                run.run_id, redact_sensitive_text(str(exc)[:200]),
            )

    def process_outbox_result(
        self,
        outbox: Any,
        outbox_items: list[Any],
        result: Any,
        callback_url: str,
        run_id: str = "",
    ) -> None:
        """Update outbox items based on delivery result."""
        if result.ok:
            for outbox_item in outbox_items:
                outbox.mark_sent(outbox_item.outbox_id)
            logger.info(
                "callback send success: internalRunId=%s statusCode=200 itemCount=%d url=%s",
                run_id, len(outbox_items), redact_url_for_log(callback_url),
            )
            return

        error_msg = getattr(result, "last_error", None) or "CALLBACK_FAILED"
        retryable, _ = classify_callback_error(error_msg)

        for outbox_item in outbox_items:
            outbox.mark_failed(
                outbox_item.outbox_id,
                error_message=error_msg,
                retryable=retryable,
            )

        status = "FAILED_RETRYABLE" if retryable else "FAILED_FINAL"
        logger.warning(
            "callback send failed: internalRunId=%s itemCount=%d status=%s url=%s error=%s",
            run_id, len(outbox_items), status, redact_url_for_log(callback_url),
            redact_sensitive_text((error_msg or "")[:120]),
        )

    def retry_pending_callbacks(
        self,
        plan_id: int | str,
        run: PlanRun | None = None,
        callback_url: str = "",
        mode: str = "batch",
        transport_factory: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Retry due callback outbox items for a plan."""
        if mode not in ("batch", "single"):
            return {"accepted": False, "planId": plan_id, "status": "FAILED",
                    "message": f"INVALID_CALLBACK_MODE: {mode}"}

        resolved_url = callback_url
        if resolved_url:
            check = self.validate_callback_url(resolved_url)
            if not check.get("ok"):
                return {"accepted": False, "planId": plan_id,
                        "status": "FAILED",
                        "message": check.get("message", "Invalid callback URL")}
        elif run is not None:
            resolved_url = self.resolve_callback_url(run)
        elif os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", ""):
            resolved_url = os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", "")
            check = self.validate_callback_url(resolved_url)
            if not check.get("ok"):
                resolved_url = ""

        if not resolved_url:
            return {"accepted": False, "planId": plan_id, "status": "FAILED",
                    "message": "No valid callback URL available for retry"}

        outbox = CallbackOutbox(str(plan_id), workspace_root=self._workspace_root)
        pending = outbox.get_pending()
        if not pending:
            return {
                "accepted": True, "planId": plan_id,
                "attempted": 0, "sent": 0, "failed": 0,
                "pendingAfter": 0, "status": "NO_PENDING",
                "message": "no pending callbacks",
            }

        transport = (
            transport_factory(resolved_url)
            if transport_factory is not None else HttpCallbackTransport()
        )
        cb = PlanItemStatusCallbackClient(transport=transport)
        item_pending = [
            it for it in pending
            if getattr(it, "payload_type", "item") != "summary"
        ]
        summary_pending = [
            it for it in pending
            if getattr(it, "payload_type", "item") == "summary"
        ]
        bodies = [it.to_callback_body() for it in item_pending]
        attempted = len(pending)
        sent = 0
        failed = 0
        run_id = run.run_id if run else ""

        if item_pending and mode == "batch":
            result = cb.send_batch(resolved_url, bodies)
            if result.ok:
                for item in item_pending:
                    outbox.mark_sent(item.outbox_id)
                sent += len(item_pending)
            else:
                self.process_outbox_result(outbox, item_pending, result, resolved_url, run_id)
                failed += len(item_pending)
        elif item_pending:
            for item in item_pending:
                result = cb.send_single(resolved_url, item.to_callback_body())
                if result.ok:
                    outbox.mark_sent(item.outbox_id)
                    sent += 1
                else:
                    self.process_outbox_result(outbox, [item], result, resolved_url, run_id)
                    failed += 1

        for item in summary_pending:
            body = item.to_callback_body()
            result = cb.send_summary(
                resolved_url, body["planId"], body.get("summary", {}),
            )
            if result.ok:
                outbox.mark_sent(item.outbox_id)
                sent += 1
            else:
                self.process_outbox_result(outbox, [item], result, resolved_url, run_id)
                failed += 1

        pending_after = len(outbox.get_pending())
        return {
            "accepted": True,
            "planId": plan_id,
            "attempted": attempted,
            "sent": sent,
            "failed": failed,
            "pendingAfter": pending_after,
            "status": "RETRIED",
            "message": "pending callbacks retried",
        }
