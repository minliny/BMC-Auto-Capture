"""
Webhook registry — register URLs, receive events, fire async HTTP callbacks.
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

import httpx

logger = logging.getLogger("bmc_auto_capture.webhook")

# Supported event types
ALL_EVENTS = (
    "execution.started",
    "execution.complete",
    "execution.error",
    "plan.started",
    "plan.completed",
    "plan.failed",
)


@dataclass
class Webhook:
    id: str
    url: str
    events: tuple[str, ...]
    secret: str = ""
    enabled: bool = True
    created_at: float = 0.0

    def matches(self, event: str) -> bool:
        return self.enabled and event in self.events


class WebhookRegistry:
    def __init__(self):
        self._hooks: dict[str, Webhook] = {}
        self._lock = asyncio.Lock()

    def register(self, url: str, events: list[str], secret: str = "") -> Webhook:
        """Register a new webhook. Returns the created Webhook."""
        hook = Webhook(
            id=uuid.uuid4().hex[:12],
            url=url,
            events=tuple(e for e in events if e in ALL_EVENTS),
            secret=secret,
            created_at=asyncio.get_event_loop().time(),
        )
        self._hooks[hook.id] = hook
        logger.info("Registered webhook %s for events %s", hook.id, hook.events)
        return hook

    def unregister(self, hook_id: str) -> bool:
        """Remove a webhook by ID. Returns True if found."""
        if hook_id in self._hooks:
            del self._hooks[hook_id]
            logger.info("Unregistered webhook %s", hook_id)
            return True
        return False

    def list_all(self) -> list[Webhook]:
        """List all registered webhooks (without secrets)."""
        return list(self._hooks.values())

    def get(self, hook_id: str) -> Webhook | None:
        return self._hooks.get(hook_id)

    async def dispatch(self, event: str, payload: dict) -> None:
        """Fire async HTTP POST to all matching webhooks for the given event."""
        matching = [h for h in self._hooks.values() if h.matches(event)]
        if not matching:
            return

        logger.debug("Dispatching event '%s' to %d webhooks", event, len(matching))

        async with self._lock:
            fire_tasks = [_fire_one(h, event, payload) for h in matching]
            if fire_tasks:
                await asyncio.gather(*fire_tasks, return_exceptions=True)


async def _fire_one(hook: Webhook, event: str, payload: dict) -> None:
    """Fire a single webhook asynchronously. Failures are logged but not raised."""
    try:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "BMC-Auto-Capture/0.2",
            "X-BMC-Event": event,
            "X-BMC-Webhook-ID": hook.id,
        }
        body = json.dumps(payload, ensure_ascii=False)
        if hook.secret:
            sig = hmac.new(
                hook.secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).digest()
            headers["X-BMC-Signature"] = f"sha256={base64.b64encode(sig).decode()}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                hook.url,
                content=body.encode(),
                headers=headers,
            )
            logger.info(
                "Webhook %s [%s] → %s (%d)",
                hook.id,
                event,
                hook.url,
                resp.status_code,
            )
    except Exception as e:
        logger.warning("Webhook %s [%s] failed: %s", hook.id, event, e)


# Global registry instance
_registry = WebhookRegistry()


def get_registry() -> WebhookRegistry:
    return _registry
