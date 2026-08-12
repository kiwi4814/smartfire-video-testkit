"""测试套件控制面路由（``/testkit/v1``）。

用于：复位、查看/修改场景设备、触发 SIP 注册、查看 Registrar 与事件。
"""

from __future__ import annotations

import contextlib
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
from video_testkit.zlm_client import ZlmError

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
async def reset(request: Request) -> dict[str, Any]:
    service = get_service(request)
    store = get_store(request)
    zlm_stream_ids = [
        str(s.media["streamId"]) for s in store.live_streams.values() if s.media is not None
    ]
    counts = service.reset()
    simulator = get_simulator(request)
    simulator.reset()
    for device_id in get_store(request).devices:
        simulator.set_known_device(device_id)
    await simulator.ensure_all_listeners()  # 同步重建常驻监听（新端口），reset 返回即可查询
    registrar = get_registrar(request)
    if registrar is not None:
        registrar.reset()
    # 强制关闭本场景遗留的 ZLM RTP 端口/流（幂等；重复 reset 不影响其他场景）。
    zlm = service.zlm_client
    if zlm is not None:
        for stream_id in zlm_stream_ids:
            with contextlib.suppress(ZlmError):
                await zlm.close_rtp_server(stream_id)
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


# ---------------------------------------------------------------- Keepalive 控制


@router.post("/devices/{external_device_id}/keepalive/start")
async def keepalive_start(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    view = simulator.start_keepalive(external_device_id)
    return ok(get_request_id(request), view)


@router.post("/devices/{external_device_id}/keepalive/pause")
async def keepalive_pause(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    view = simulator.stop_keepalive(external_device_id)
    return ok(get_request_id(request), view)


@router.post("/devices/{external_device_id}/keepalive/resume")
async def keepalive_resume(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    view = simulator.resume_keepalive(external_device_id)
    return ok(get_request_id(request), view)


@router.post("/devices/{external_device_id}/keepalive/drop")
async def keepalive_drop(external_device_id: str, request: Request) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    view = simulator.drop_next_keepalive(external_device_id)
    return ok(get_request_id(request), view)


@router.post("/devices/{external_device_id}/keepalive/send")
def keepalive_send(
    external_device_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    background_tasks.add_task(simulator.send_keepalive, external_device_id)
    return ok(
        get_request_id(request),
        {"externalDeviceId": external_device_id, "status": "SENT"},
    )


@router.post("/devices/{external_device_id}/keepalive/malformed")
def keepalive_malformed(
    external_device_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator = get_simulator(request)
    simulator.set_known_device(external_device_id)
    background_tasks.add_task(simulator.send_keepalive, external_device_id, malformed=True)
    return ok(
        get_request_id(request),
        {"externalDeviceId": external_device_id, "status": "SENT_MALFORMED"},
    )


class CatalogBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = Field(default="normal")
    page_size: int = Field(default=0, ge=0, le=64)
    delay_seconds: float = Field(default=0.0, ge=0, le=10)
    missing_channel_ids: list[str] = Field(default_factory=list, alias="missingChannelIds")
    charset: str | None = None


@router.post("/devices/{external_device_id}/catalog")
def configure_catalog(
    external_device_id: str,
    request: Request,
    body: CatalogBody,
) -> dict[str, Any]:
    """安排设备目录响应场景（normal/multi/duplicate/delayed/missing/malformed/out-of-order/timeout）。"""
    simulator = get_simulator(request)
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator.set_known_device(external_device_id)
    view = simulator.configure_catalog(
        external_device_id,
        mode=body.mode,
        page_size=body.page_size,
        delay_seconds=body.delay_seconds,
        missing_channel_ids=body.missing_channel_ids,
        charset=body.charset,
    )
    return ok(get_request_id(request), view)


@router.get("/devices/{external_device_id}/catalog")
def catalog_status(external_device_id: str, request: Request) -> dict[str, Any]:
    """查看设备目录场景与响应统计（queriesReceived/responsesSent/最后查询 SN）。"""
    simulator = get_simulator(request)
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    return ok(get_request_id(request), simulator.catalog_status(external_device_id))


class LiveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = Field(default="normal")
    delay_seconds: float = Field(default=0.0, ge=0, le=10)
    reject_code: int = Field(default=486, ge=400, le=699)
    ack_timeout_seconds: float = Field(default=1.5, gt=0, le=10, alias="ackTimeoutSeconds")
    media_mode: str = Field(default="normal", alias="mediaMode")
    media_loss_rate: float = Field(default=0.0, ge=0, lt=1, alias="mediaLossRate")
    media_stop_after_seconds: float = Field(default=0.0, ge=0, le=60, alias="mediaStopAfterSeconds")


@router.post("/devices/{external_device_id}/live")
def configure_live(
    external_device_id: str,
    request: Request,
    body: LiveBody,
) -> dict[str, Any]:
    """安排设备实时流应答场景（normal/rejection/delayed/no-ack/drop）。"""
    simulator = get_simulator(request)
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    simulator.set_known_device(external_device_id)
    view = simulator.configure_live(
        external_device_id,
        mode=body.mode,
        delay_seconds=body.delay_seconds,
        reject_code=body.reject_code,
        ack_timeout=body.ack_timeout_seconds,
        media_mode=body.media_mode,
        media_loss_rate=body.media_loss_rate,
        media_stop_after_seconds=body.media_stop_after_seconds,
    )
    return ok(get_request_id(request), view)


@router.get("/devices/{external_device_id}/live")
def live_status(external_device_id: str, request: Request) -> dict[str, Any]:
    """查看设备实时流场景与 Dialog 脱敏诊断。"""
    simulator = get_simulator(request)
    service: ProviderService = get_service(request)
    service.require_device(external_device_id)
    return ok(get_request_id(request), simulator.live_status(external_device_id))


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
