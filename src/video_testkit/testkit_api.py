"""测试套件控制面路由（``/testkit/v1``）。

用于：复位、查看/修改场景设备、触发 SIP 注册、查看 Registrar 与事件。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field

from video_testkit.errors import ErrorCode, provider_error
from video_testkit.provider_api import get_request_id, get_service, ok
from video_testkit.scenario import scenario_summary
from video_testkit.service import ProviderService
from video_testkit.sip.registrar import SipRegistrar
from video_testkit.sip.simulator import DeviceSimulator
from video_testkit.state import Store

router = APIRouter()


class OnlineStatusBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    online_status: str = Field(default="ONLINE", alias="onlineStatus")


class ReadyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ready: bool = True


def get_store(request: Request) -> Store:
    return cast(Store, request.app.state.store)


def get_simulator(request: Request) -> DeviceSimulator:
    return cast(DeviceSimulator, request.app.state.simulator)


def get_registrar(request: Request) -> SipRegistrar | None:
    return cast(SipRegistrar | None, request.app.state.registrar)


@router.post("/reset")
def reset(request: Request) -> dict[str, Any]:
    service = get_service(request)
    counts = service.reset()
    simulator = get_simulator(request)
    simulator.reset()
    for device_id in get_store(request).devices:
        simulator.set_known_device(device_id)
    registrar = get_registrar(request)
    if registrar is not None:
        registrar.reset()
    return ok(get_request_id(request), {"status": "RESET", **counts})


@router.get("/scenario")
def scenario(request: Request) -> dict[str, Any]:
    return ok(get_request_id(request), scenario_summary(get_store(request)))


@router.get("/devices")
def list_devices(request: Request) -> dict[str, Any]:
    store = get_store(request)
    simulator = get_simulator(request)
    devices = []
    for device_id in sorted(store.devices):
        d = store.devices[device_id]
        devices.append(
            {
                "externalDeviceId": device_id,
                "sourceName": d.source_name,
                "onlineStatus": d.online_status,
                "channelCount": d.channel_count,
                "simulator": simulator.status(device_id),
            }
        )
    return ok(get_request_id(request), {"items": devices})


@router.get("/devices/{external_device_id}")
def get_device(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    device = service.require_device(external_device_id)
    simulator = get_simulator(request)
    return ok(
        get_request_id(request),
        {
            "externalDeviceId": device.external_device_id,
            "sourceName": device.source_name,
            "onlineStatus": device.online_status,
            "channelCount": device.channel_count,
            "simulator": simulator.status(device.external_device_id),
        },
    )


@router.post("/devices/{external_device_id}/register")
def trigger_register(
    external_device_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    background_tasks.add_task(simulator.trigger_register, external_device_id)
    return ok(
        get_request_id(request),
        {"externalDeviceId": external_device_id, "status": "REGISTERING"},
    )


@router.post("/devices/{external_device_id}/unregister")
def trigger_unregister(
    external_device_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)

    async def _unregister_then_mark_offline() -> None:
        view = await simulator.trigger_unregister(external_device_id)
        if view["status"] == "UNREGISTERED":
            # Expires: 0 注销成功后，Provider 侧设备进入可观察离线状态。
            service.set_device_online_status(external_device_id, "OFFLINE")

    background_tasks.add_task(_unregister_then_mark_offline)
    return ok(
        get_request_id(request),
        {"externalDeviceId": external_device_id, "status": "UNREGISTERING"},
    )


@router.get("/devices/{external_device_id}/status")
def device_simulator_status(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    return ok(get_request_id(request), simulator.status(external_device_id))


@router.post("/devices/{external_device_id}/status")
def set_device_status(
    external_device_id: str,
    request: Request,
    body: OnlineStatusBody,
) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    view = service.set_device_online_status(external_device_id, body.online_status)
    return ok(get_request_id(request), view)


@router.get("/sip/registrar/requests")
def registrar_requests(request: Request) -> dict[str, Any]:
    registrar = get_registrar(request)
    if registrar is None:
        raise provider_error(
            ErrorCode.VIDEO_PROVIDER_UNAVAILABLE,
            "内置 Registrar 未启用",
            {"registrarEnabled": False},
        )
    return ok(get_request_id(request), {"items": registrar.requests_log()})


@router.get("/sip/registrar/registrations")
def registrar_registrations(request: Request) -> dict[str, Any]:
    registrar = get_registrar(request)
    if registrar is None:
        raise provider_error(
            ErrorCode.VIDEO_PROVIDER_UNAVAILABLE,
            "内置 Registrar 未启用",
            {"registrarEnabled": False},
        )
    return ok(get_request_id(request), {"items": registrar.registrations()})


@router.get("/events")
def provider_events(request: Request) -> dict[str, Any]:
    store = get_store(request)
    items = []
    for event in store.events:
        items.append(
            {
                "eventId": event.event_id,
                "eventType": event.event_type,
                "occurredAt": event.occurred_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "revision": event.revision,
                "resource": {
                    "externalDeviceId": event.resource_device_id,
                    "externalChannelId": event.resource_channel_id,
                },
                "data": event.data,
                "deliveryState": event.delivery_state,
                "attempts": event.attempts,
                "lastError": event.last_error,
            }
        )
    return ok(get_request_id(request), {"items": items})


@router.post("/ready")
def set_ready(request: Request, body: ReadyBody) -> dict[str, Any]:
    store = get_store(request)
    store.ready_override = body.ready
    return ok(get_request_id(request), {"status": "READY" if body.ready else "NOT_READY"})
