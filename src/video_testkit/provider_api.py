"""Provider 共同契约路由（``/provider/v1``）。

只做参数/响应形态映射，业务规则全部委托给 :class:`ProviderService`。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel

from video_testkit.errors import invalid_argument
from video_testkit.models import (
    CapabilitiesData,
    HealthData,
    LiveStartRequest,
    PlaybackStartRequest,
    ReadyData,
    RecordQueryRequest,
)
from video_testkit.service import CAPABILITIES, ProviderService

router = APIRouter()


def ok(request_id: str, data: Any) -> dict[str, Any]:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    elif isinstance(data, list):
        data = [d.model_dump(mode="json") if isinstance(d, BaseModel) else d for d in data]
    return {"requestId": request_id, "data": data}


def json_response(request_id: str, data: Any, status_code: int = 200) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content=ok(request_id, data))


def get_service(request: Request) -> ProviderService:
    return cast(ProviderService, request.app.state.service)


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def require_idem_key(
    x_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if not x_idempotency_key:
        raise invalid_argument("写操作必须携带 Idempotency-Key", {"header": "Idempotency-Key"})
    return x_idempotency_key


def _validate_online_status(value: str | None) -> None:
    if value is not None and value not in ("ONLINE", "OFFLINE", "UNKNOWN"):
        raise invalid_argument(
            "onlineStatus 必须是 ONLINE/OFFLINE/UNKNOWN", {"onlineStatus": value}
        )


# ---------------------------------------------------------------- 健康 / 信息 / 能力


@router.get("/health/live")
def health_live(request: Request) -> dict[str, Any]:
    return ok(get_request_id(request), HealthData(status="UP"))


@router.get("/health/ready")
def health_ready(request: Request) -> Response:
    service = get_service(request)
    registrar = request.app.state.registrar
    listening = bool(registrar is not None and registrar.listening)
    status = service.ready_status(
        registrar_enabled=request.app.state.settings.registrar_enabled,
        registrar_listening=listening,
    )
    if status == "READY":
        return json_response(get_request_id(request), ReadyData(status=status))
    return json_response(get_request_id(request), ReadyData(status=status), status_code=503)


@router.get("/info")
def info(request: Request) -> dict[str, Any]:
    service = get_service(request)
    return ok(get_request_id(request), service.info_data())


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    return ok(get_request_id(request), CapabilitiesData(capabilities=CAPABILITIES))


# ---------------------------------------------------------------- 设备与通道


@router.get("/devices")
def list_devices(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    query: str | None = Query(None, max_length=256),
    online_status: str | None = Query(None, alias="onlineStatus"),
    updated_after: str | None = Query(None, alias="updatedAfter"),
) -> dict[str, Any]:
    _validate_online_status(online_status)
    service = get_service(request)
    items, total = service.list_devices(page, page_size, query, online_status, updated_after)
    return ok(get_request_id(request), service.device_page_view(items, page, page_size, total))


@router.get("/devices/{external_device_id}")
def get_device(external_device_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    device = service.get_device(external_device_id)
    return ok(get_request_id(request), service.device_view(device))


@router.get("/devices/{external_device_id}/channels")
def list_channels(
    external_device_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    query: str | None = Query(None, max_length=256),
    online_status: str | None = Query(None, alias="onlineStatus"),
) -> dict[str, Any]:
    _validate_online_status(online_status)
    service = get_service(request)
    device = service.require_device(external_device_id)
    items, total = service.list_channels(external_device_id, page, page_size, query, online_status)
    view = service.channel_page_view(device, items, page, page_size, total)
    return ok(get_request_id(request), view)


@router.get("/devices/{external_device_id}/status")
def device_status(external_device_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    return ok(get_request_id(request), service.device_status(external_device_id))


# ---------------------------------------------------------------- Catalog 同步


@router.post("/devices/{external_device_id}/catalog-syncs", status_code=202)
def submit_catalog_sync(
    external_device_id: str,
    request: Request,
    idem_key: str = Depends(require_idem_key),
) -> Response:
    service = get_service(request)
    op, _created = service.submit_catalog_sync(external_device_id, idem_key)
    view = service.catalog_accepted_view(op)
    return json_response(get_request_id(request), view, status_code=202)


@router.get("/catalog-syncs/{operation_id}")
def get_catalog_sync(operation_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    op = service.get_catalog_operation(operation_id)
    return ok(get_request_id(request), service.catalog_result_view(op))


# ---------------------------------------------------------------- 实时流


@router.post("/live-streams")
def start_live_stream(
    request: Request,
    body: LiveStartRequest,
    idem_key: str = Depends(require_idem_key),
) -> Response:
    service = get_service(request)
    stream, created = service.start_live_stream(body, idem_key)
    view = service.stream_view(stream, "LIVE")
    return json_response(get_request_id(request), view, status_code=201 if created else 200)


@router.get("/live-streams/{provider_stream_key}")
def get_live_stream(provider_stream_key: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    stream = service.get_live_stream(provider_stream_key)
    return ok(get_request_id(request), service.stream_view(stream, "LIVE"))


@router.delete("/live-streams/{provider_stream_key}", status_code=204)
def stop_live_stream(provider_stream_key: str, request: Request) -> Response:
    service = get_service(request)
    service.stop_live_stream(provider_stream_key)
    return Response(status_code=204)


# ---------------------------------------------------------------- 设备录像查询


@router.post("/device-record-queries", status_code=202)
def submit_record_query(
    request: Request,
    body: RecordQueryRequest,
    idem_key: str = Depends(require_idem_key),
) -> Response:
    service = get_service(request)
    query, _created = service.submit_record_query(body, idem_key)
    view = service.record_query_accepted_view(query)
    return json_response(get_request_id(request), view, status_code=202)


@router.get("/device-record-queries/{query_id}")
def get_record_query(query_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    query = service.get_record_query(query_id)
    return ok(get_request_id(request), service.record_query_result_view(query))


# ---------------------------------------------------------------- 回放流


@router.post("/playback-streams")
def start_playback(
    request: Request,
    body: PlaybackStartRequest,
    idem_key: str = Depends(require_idem_key),
) -> Response:
    service = get_service(request)
    stream, created = service.start_playback(body, idem_key)
    view = service.stream_view(stream, "PLAYBACK")
    return json_response(get_request_id(request), view, status_code=201 if created else 200)


@router.get("/playback-streams/{provider_stream_key}")
def get_playback_stream(provider_stream_key: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    stream = service.get_playback_stream(provider_stream_key)
    return ok(get_request_id(request), service.stream_view(stream, "PLAYBACK"))


@router.delete("/playback-streams/{provider_stream_key}", status_code=204)
def stop_playback_stream(provider_stream_key: str, request: Request) -> Response:
    service = get_service(request)
    service.stop_playback_stream(provider_stream_key)
    return Response(status_code=204)
