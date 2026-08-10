"""Provider 事件投递：内存 outbox + 有界重试回调（httpx）。

事件记录在 Store.events；若配置了 ``events_callback_url`` 则至少一次投递到
SmartFire 的 ``/internal/integration/video/provider-events``，否则标记
``NOT_CONFIGURED``（仍可通过控制面查看）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from video_testkit.config import Settings
from video_testkit.state import ProviderEvent, Store, now_utc

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "DEVICE_ONLINE",
    "DEVICE_OFFLINE",
    "CATALOG_CHANGED",
    "CHANNEL_ONLINE",
    "CHANNEL_OFFLINE",
)


def record_event(
    store: Store,
    settings: Settings,
    event_type: str,
    device_id: str | None,
    channel_id: str | None,
    data: dict[str, Any],
) -> ProviderEvent:
    """记录一条 Provider 事件（不投递，投递由 worker 负责）。"""
    event = ProviderEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        occurred_at=now_utc(),
        revision=store.next_revision(),
        resource_device_id=device_id,
        resource_channel_id=channel_id,
        data=data,
        delivery_state="PENDING",
    )
    store.events.append(event)
    return event


class EventsDeliveryWorker:
    """后台投递 worker：读取 outbox 中未送达事件并带退避重试。"""

    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def run(self, stop_event: asyncio.Event) -> None:
        timeout = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while not stop_event.is_set():
                await self._deliver_pending(client)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.1)
                except TimeoutError:
                    pass

    async def _deliver_pending(self, client: httpx.AsyncClient) -> None:
        url = self._settings.events_callback_url
        for event in self._store.events:
            if event.delivery_state == "DELIVERED":
                continue
            if event.delivery_state == "NOT_CONFIGURED":
                continue
            if url is None:
                event.delivery_state = "NOT_CONFIGURED"
                continue
            if (
                event.delivery_state == "FAILED"
                and event.attempts >= self._settings.events_max_attempts
            ):
                continue
            payload = {
                "eventId": event.event_id,
                "eventType": event.event_type,
                "providerType": self._settings.provider_type,
                "providerInstanceCode": self._settings.provider_instance_code,
                "occurredAt": event.occurred_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "revision": event.revision,
                "resource": {
                    "externalDeviceId": event.resource_device_id,
                    "externalChannelId": event.resource_channel_id,
                },
                "data": event.data,
            }
            event.attempts += 1
            event.delivery_state = "FAILED"
            event.last_error = None
            try:
                resp = await client.post(url, json=payload)
                if 200 <= resp.status_code < 300:
                    event.delivery_state = "DELIVERED"
                else:
                    event.last_error = f"http {resp.status_code}"
            except httpx.HTTPError as exc:
                event.last_error = type(exc).__name__
            if (
                event.delivery_state == "FAILED"
                and event.attempts < self._settings.events_max_attempts
            ):
                delay = self._settings.events_retry_base_delay * (2 ** (event.attempts - 1))
                await asyncio.sleep(delay)
