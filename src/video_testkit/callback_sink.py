"""Provider Event Callback Sink（VT-11）：TestKit 控制面驱动的回调接收器。

Sink 与 TestKit 同一进程、同一端口，以独立路径 ``/sink/provider-events``
扮演 SmartFire 侧回调接收端（真实 HTTP）：

- 独立 Bearer token 校验（契约：v1 回调固定 Bearer，token 不进入 payload/日志）；
- 可脚本化响应（2xx/401/403/500/延迟/503 断连），用于验证 Provider 重试语义；
- 接收事件按 ``providerInstanceCode + eventId`` 幂等去重；
- revision 顺序校验：仅在同一 providerEpoch + resource 内比较，乱序/迟到可观察；
- Provider 侧投递目标与 token 由控制面 ``POST /testkit/v1/events/sink/config``
  写入 store（运行时可切换，reset 清理）。

测试只经真实 HTTP 观察（Get/Post /testkit/v1/events/sink/...），不调用内部实现。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from video_testkit.provider_api import get_request_id, ok

logger = logging.getLogger(__name__)


class CallbackSinkState:
    """Sink 的接收状态与脚本（挂在 app.state，reset 时清理）。"""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self._seen_ids: set[tuple[str, str]] = set()
        self._revisions: dict[tuple[str, str, str], int] = {}
        self.script_status: int | None = None
        self.delay_seconds: float = 0.0

    def reset(self) -> None:
        self.received.clear()
        self._seen_ids.clear()
        self._revisions.clear()
        self.script_status = None
        self.delay_seconds = 0.0

    def set_script(self, status: int | None, delay_seconds: float = 0.0) -> None:
        self.script_status = status
        self.delay_seconds = delay_seconds

    def record(self, event: dict[str, Any]) -> None:
        """按 providerInstanceCode + eventId 幂等记录；revision 乱序可观察。"""
        event_id = event.get("eventId") or ""
        instance = event.get("providerInstanceCode") or ""
        identity = (instance, event_id)
        if identity in self._seen_ids:
            self.received.append({**event, "_duplicate": True, "_outOfOrder": False})
            return
        self._seen_ids.add(identity)
        epoch = event.get("providerEpoch") or ""
        resource = (
            f"{event.get('resource', {}).get('externalDeviceId') or ''}"
            f"/{event.get('resource', {}).get('externalChannelId') or ''}"
        )
        try:
            revision = int(event.get("revision") or "0")
        except (TypeError, ValueError):
            revision = 0
        key = (instance, epoch, resource)
        previous = self._revisions.get(key)
        out_of_order = previous is not None and revision < previous
        if previous is None or revision > previous:
            self._revisions[key] = revision
        self.received.append({**event, "_duplicate": False, "_outOfOrder": out_of_order})


def install_sink_routes(app: Any, sink: CallbackSinkState) -> None:
    """将回调接收端点挂到主 app（/sink/provider-events）。"""

    @app.post("/sink/provider-events")
    async def receive_provider_event(request: Request) -> dict[str, Any]:
        sink_state: CallbackSinkState = request.app.state.callback_sink
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {request.app.state.callback_sink_token}":
            raise HTTPException(status_code=401, detail="invalid bearer token")
        if sink_state.delay_seconds > 0:
            await asyncio.sleep(sink_state.delay_seconds)
        if sink_state.script_status is not None:
            raise HTTPException(
                status_code=sink_state.script_status, detail="scripted sink response"
            )
        body = await request.json()
        event = body.get("event", body) if isinstance(body, dict) else {}
        sink_state.record(event)
        return {"status": "ok"}

    @app.get("/sink/health")
    def sink_health(request: Request) -> dict[str, Any]:
        sink_state: CallbackSinkState = request.app.state.callback_sink
        return {"status": "UP", "received": len(sink_state.received)}


# ---------------------------------------------------------------- TestKit 控制面


class SinkScriptBody(BaseModel):
    model_config = {"extra": "ignore"}

    status: int | None = Field(default=None, ge=200, le=599)
    delay_seconds: float = Field(default=0.0, ge=0, le=10, alias="delaySeconds")


class SinkConfigBody(BaseModel):
    model_config = {"extra": "ignore"}

    # Provider 事件投递目标（不设置则保持现状）。
    url: str | None = Field(default=None)
    token: str | None = Field(default=None)


def sink_router() -> APIRouter:
    router = APIRouter()

    @router.post("/events/sink/config")
    def sink_config(request: Request, body: SinkConfigBody) -> dict[str, Any]:
        """配置 Provider 事件投递目标与回调 token（写入运行态，重启失效）。"""
        if body.url is not None:
            request.app.state.events_callback_url = body.url
        if body.token is not None:
            request.app.state.events_callback_token = body.token
            # sink 校验 token 与投递 token 保持同一来源（契约：独立 Bearer）。
            request.app.state.callback_sink_token = body.token
        return ok(
            get_request_id(request),
            {
                "status": "ok",
                "url": request.app.state.events_callback_url,
                "tokenConfigured": bool(request.app.state.events_callback_token),
            },
        )

    @router.get("/events/sink/status")
    def sink_status(request: Request) -> dict[str, Any]:
        sink: CallbackSinkState = request.app.state.callback_sink
        return ok(
            get_request_id(request),
            {
                "scriptStatus": sink.script_status,
                "delaySeconds": sink.delay_seconds,
                "received": len(sink.received),
                "duplicates": sum(1 for e in sink.received if e["_duplicate"]),
                "outOfOrder": sum(1 for e in sink.received if e["_outOfOrder"]),
            },
        )

    @router.get("/events/sink/received")
    def sink_received(request: Request) -> dict[str, Any]:
        sink: CallbackSinkState = request.app.state.callback_sink
        return ok(get_request_id(request), {"items": list(sink.received)})

    @router.post("/events/sink/script")
    def sink_script(request: Request, body: SinkScriptBody) -> dict[str, Any]:
        sink: CallbackSinkState = request.app.state.callback_sink
        sink.set_script(body.status, body.delay_seconds)
        return ok(
            get_request_id(request),
            {"status": "ok", "scriptStatus": body.status, "delaySeconds": body.delay_seconds},
        )

    @router.post("/events/sink/clear")
    def sink_clear(request: Request) -> dict[str, Any]:
        sink: CallbackSinkState = request.app.state.callback_sink
        sink.reset()
        return ok(get_request_id(request), {"status": "ok", "received": 0})

    return router
