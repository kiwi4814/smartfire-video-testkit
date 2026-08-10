"""FastAPI 应用装配：请求 ID、认证、错误 envelope、生命周期（SIP/事件 worker）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from video_testkit.config import Settings, get_settings
from video_testkit.errors import ErrorCode, ErrorEnvelope, ProviderError
from video_testkit.events import EventsDeliveryWorker
from video_testkit.logging_conf import configure_logging, log_ctx, request_id_var
from video_testkit.provider_api import router as provider_router
from video_testkit.scenario import seed_scenario
from video_testkit.service import ProviderService
from video_testkit.sip.registrar import SipRegistrar
from video_testkit.sip.simulator import DeviceSimulator
from video_testkit.state import Store
from video_testkit.testkit_api import router as testkit_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.validate_startup()

    registrar: SipRegistrar | None = None
    if settings.registrar_enabled:
        registrar = SipRegistrar(
            host=settings.registrar_host,
            port=settings.registrar_port,
            realm=settings.gb_realm,
            password=settings.gb_password,
        )
        await registrar.start()  # 绑定失败即启动失败（fail fast）

    store = Store()
    seed_scenario(store)
    service = ProviderService(store, settings)
    simulator = DeviceSimulator(settings)
    for device_id in store.devices:
        simulator.set_known_device(device_id)

    stop_event = asyncio.Event()
    worker = EventsDeliveryWorker(store, settings)
    worker_task = asyncio.create_task(worker.run(stop_event))

    app.state.store = store
    app.state.service = service
    app.state.simulator = simulator
    app.state.registrar = registrar

    logger.info(
        "testkit startup complete",
        extra=log_ctx(
            providerInstanceCode=settings.provider_instance_code,
            registrarAddr=settings.registrar_addr if settings.registrar_enabled else None,
            registrarEnabled=settings.registrar_enabled,
            authEnabled=settings.auth_enabled,
        ),
    )
    try:
        yield
    finally:
        stop_event.set()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        if registrar is not None:
            await registrar.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    app = FastAPI(
        title="SmartFire Video TestKit",
        description="Fake Video Provider + GB28181 Device Simulator 测试套件",
        version=settings.implementation_version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    def auth_dependency(request: Request) -> None:
        if not settings.auth_enabled:
            return
        expected = f"Bearer {settings.auth_token}"
        if request.headers.get("authorization") != expected:
            raise ProviderError(ErrorCode.VIDEO_PROVIDER_AUTH_FAILED, "unauthorized")

    app.include_router(
        provider_router,
        prefix="/provider/v1",
        dependencies=[Depends(auth_dependency)],
    )
    app.include_router(
        testkit_router,
        prefix="/testkit/v1",
        dependencies=[Depends(auth_dependency)],
    )

    @app.get("/")
    def index() -> dict[str, str]:
        return {
            "name": "smartfire-video-testkit",
            "provider": "/provider/v1",
            "testkit": "/testkit/v1",
        }

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
        logger.info(
            "provider error response",
            extra=log_ctx(code=exc.code.value, httpStatus=exc.http_status),
        )
        envelope = ErrorEnvelope(
            request_id=getattr(request.state, "request_id", ""),
            code=exc.code.value,
            message=exc.message,
            retryable=bool(exc.retryable),
            details=dict(exc.details or {}),
        )
        return JSONResponse(status_code=int(exc.http_status or 500), content=envelope.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        envelope = ErrorEnvelope(
            request_id=getattr(request.state, "request_id", ""),
            code=ErrorCode.VIDEO_INVALID_ARGUMENT.value,
            message="请求参数非法",
            retryable=False,
            details={"errors": exc.errors()[:5]},
        )
        return JSONResponse(status_code=400, content=envelope.to_dict())

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        envelope = ErrorEnvelope(
            request_id=getattr(request.state, "request_id", ""),
            code=ErrorCode.VIDEO_PROVIDER_UNAVAILABLE.value,
            message="internal error",
            retryable=True,
            details={},
        )
        return JSONResponse(status_code=500, content=envelope.to_dict())

    return app
