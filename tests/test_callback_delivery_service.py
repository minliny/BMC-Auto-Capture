from __future__ import annotations

from src.callback_outbox import CallbackOutbox, build_outbox_item_from_callback_body
from src.plan_item_status_callback_client import (
    FakeCallbackTransport,
    PlanItemStatusCallbackClient,
)
from src.plan_run_service.callback_delivery import CallbackDeliveryService
from src.plan_run_service.service import PlanRun, PlanRunItem


def _run(**overrides):
    values = {
        "plan_id": "plan-1",
        "run_id": "run-1",
        "status": "RUNNING",
        "item_status_url": "",
        "callback_mode": "batch",
        "updater": "tester",
        "items": [],
    }
    values.update(overrides)
    return PlanRun(**values)


def _item(**overrides):
    values = {
        "plan_id": "plan-1",
        "device_group": "A3",
        "device_name": "device-1",
        "task_name": "task-1",
        "status": "SUCCESS",
        "started_at": 1.0,
        "finished_at": 2.0,
    }
    values.update(overrides)
    return PlanRunItem(**values)


def test_callback_delivery_validation_matches_intranet_policy(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))

    assert service.validate_callback_url("http://127.0.0.1/callback") == {"ok": True}
    assert service.validate_callback_url("http://10.0.0.1/callback") == {"ok": True}

    bad = service.validate_callback_url("ftp://127.0.0.1/callback")
    assert bad["ok"] is False
    assert bad["reason"] == "CALLBACK_INVALID_SCHEME"


def test_callback_delivery_builds_timestamped_callback_body(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))

    body = service.build_callback_body(_run(), _item(error_message="failed"))

    assert body["planId"] == "plan-1"
    assert body["deviceGroup"] == "A3"
    assert body["deviceName"] == "device-1"
    assert body["taskName"] == "task-1"
    assert body["status"] == "SUCCESS"
    assert body["updater"] == "tester"
    assert body["errorMessage"] == "failed"
    assert body["startedAt"] == "1970-01-01T00:00:01+00:00"
    assert body["finishedAt"] == "1970-01-01T00:00:02+00:00"


def test_callback_delivery_without_url_writes_outbox_but_does_not_send(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))
    transport = FakeCallbackTransport()
    cb = PlanItemStatusCallbackClient(transport=transport)

    service.deliver_item_status(_run(), _item(), cb)

    outbox = CallbackOutbox("plan-1", workspace_root=str(tmp_path))
    assert outbox.get_stats() == {"URL_NOT_CONFIGURED": 1}
    assert transport.calls == []


def test_callback_delivery_success_marks_outbox_sent(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))
    transport = FakeCallbackTransport()
    cb = PlanItemStatusCallbackClient(transport=transport)

    service.deliver_item_status(
        _run(item_status_url="http://127.0.0.1/callback"),
        _item(),
        cb,
    )

    outbox = CallbackOutbox("plan-1", workspace_root=str(tmp_path))
    assert outbox.get_stats() == {"SENT": 1}
    assert transport.calls[0]["url"] == "http://127.0.0.1/callback"


def test_callback_delivery_retry_no_pending(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))

    result = service.retry_pending_callbacks(
        "plan-1",
        callback_url="http://127.0.0.1/callback",
        transport_factory=lambda _url: FakeCallbackTransport(),
    )

    assert result["accepted"] is True
    assert result["status"] == "NO_PENDING"
    assert result["attempted"] == 0


def test_callback_delivery_retry_pending_batch_marks_sent(tmp_path):
    service = CallbackDeliveryService(str(tmp_path))
    outbox = CallbackOutbox("plan-1", workspace_root=str(tmp_path))
    outbox.append(build_outbox_item_from_callback_body(
        plan_id="plan-1",
        device_group="A3",
        device_name="device-1",
        task_name="task-1",
        status="SUCCESS",
        updater="tester",
        callback_url="http://127.0.0.1/callback",
    ))
    transport = FakeCallbackTransport()

    result = service.retry_pending_callbacks(
        "plan-1",
        callback_url="http://127.0.0.1/callback",
        mode="batch",
        transport_factory=lambda _url: transport,
    )

    assert result["accepted"] is True
    assert result["status"] == "RETRIED"
    assert result["attempted"] == 1
    assert result["sent"] == 1
    assert result["failed"] == 0
    assert result["pendingAfter"] == 0
    assert outbox.get_stats() == {"SENT": 1}
    assert transport.calls[0]["payload"]["items"][0]["taskName"] == "task-1"
