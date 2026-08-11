"""Provider 共同契约 Conformance 测试用例集合。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from video_testkit.conformance.bundle import ContractBundle, ContractValidationError


class TestCase:
    def __init__(
        self,
        test_id: str,
        name: str,
        category: str,
        func: Callable[[httpx.Client, ContractBundle, dict[str, Any]], None],
        required_capability: str | None = None,
    ) -> None:
        self.test_id = test_id
        self.name = name
        self.category = category
        self.func = func
        self.required_capability = required_capability

    def run(self, client: httpx.Client, bundle: ContractBundle, context: dict[str, Any]) -> None:
        self.func(client, bundle, context)


ALL_CASES: list[TestCase] = []


def register_case(
    test_id: str,
    name: str,
    category: str,
    required_capability: str | None = None,
) -> Callable[[Callable[[httpx.Client, ContractBundle, dict[str, Any]], None]], TestCase]:
    def decorator(
        func: Callable[[httpx.Client, ContractBundle, dict[str, Any]], None],
    ) -> TestCase:
        case = TestCase(
            test_id=test_id,
            name=name,
            category=category,
            func=func,
            required_capability=required_capability,
        )
        ALL_CASES.append(case)
        return case

    return decorator


def _get_request_id(resp: httpx.Response | None, body: Any) -> str:
    if isinstance(body, dict) and "requestId" in body:
        return str(body["requestId"])
    if resp is not None:
        return str(resp.headers.get("X-Request-Id", ""))
    return ""


def _request_and_validate(
    client: httpx.Client,
    bundle: ContractBundle,
    method: str,
    url: str,
    operation_id: str,
    expected_status: int | tuple[int, ...],
    headers: dict[str, str] | None = None,
    json_body: Any = None,
) -> tuple[httpx.Response, Any]:
    resp = client.request(method, url, headers=headers, json=json_body)
    expected_tuple = expected_status if isinstance(expected_status, tuple) else (expected_status,)

    body = None
    if resp.status_code != 204:
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    request_id = _get_request_id(resp, body)

    if resp.status_code not in expected_tuple:
        raise ContractValidationError(
            operation_id=operation_id,
            request_id=request_id,
            expected=f"HTTP status code in {expected_tuple}",
            actual=resp.status_code,
            message=f"HTTP status {resp.status_code} did not match expected {expected_tuple}",
        )

    # 校验 JSON Schema
    bundle.validate_response(
        operation_id=operation_id,
        status_code=resp.status_code,
        body=body,
        request_id=request_id,
    )

    return resp, body


# ---------------------------------------------------------------- General & Health


@register_case("CT-HLT-001", "Check provider liveness", "Health")
def test_hlt_001(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(
        client, bundle, "GET", "/health/live", "getProviderLiveness", 200
    )
    if body.get("data", {}).get("status") != "UP":
        raise ContractValidationError(
            "getProviderLiveness",
            _get_request_id(None, body),
            "status == 'UP'",
            body.get("data"),
            "Health live status must be UP",
        )


@register_case("CT-HLT-002", "Check provider readiness", "Health")
def test_hlt_002(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _request_and_validate(
        client, bundle, "GET", "/health/ready", "getProviderReadiness", (200, 503)
    )


@register_case("CT-HLT-004", "Check provider capabilities declaration", "Provider")
def test_hlt_004(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(
        client, bundle, "GET", "/capabilities", "getProviderCapabilities", 200
    )
    caps = body.get("data", {}).get("capabilities", [])
    if len(caps) < 14:
        raise ContractValidationError(
            "getProviderCapabilities",
            _get_request_id(None, body),
            "at least 14 capability items",
            len(caps),
            "Capabilities list length must be at least 14",
        )


@register_case("CT-GEN-001", "Echo caller X-Request-Id header", "General")
def test_gen_001(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    req_id = "test-req-echo-" + uuid.uuid4().hex[:8]
    resp, body = _request_and_validate(
        client,
        bundle,
        "GET",
        "/health/live",
        "getProviderLiveness",
        200,
        headers={"X-Request-Id": req_id},
    )
    if body.get("requestId") != req_id or resp.headers.get("X-Request-Id") != req_id:
        raise ContractValidationError(
            "getProviderLiveness",
            req_id,
            f"echoed requestId '{req_id}'",
            body.get("requestId"),
            "Request ID was not properly echoed in body and header",
        )


@register_case("CT-GEN-002", "Reject write operation without Idempotency-Key", "General")
def test_gen_002(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/live-streams",
        "startLiveStream",
        400,
        json_body={
            "externalDeviceId": "34020000001320000001",
            "externalChannelId": "34020000001310000001",
            "streamProfile": "AUTO",
        },
    )
    err_code = body.get("error", {}).get("code")
    if err_code != "VIDEO_INVALID_ARGUMENT":
        raise ContractValidationError(
            "startLiveStream",
            _get_request_id(None, body),
            "error code VIDEO_INVALID_ARGUMENT",
            err_code,
            "Missing Idempotency-Key must produce VIDEO_INVALID_ARGUMENT",
        )


@register_case("CT-GEN-004", "Validate provider info contract version", "General")
def test_gen_004(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(client, bundle, "GET", "/info", "getProviderInfo", 200)
    contract_ver = body.get("data", {}).get("contractVersion")
    if contract_ver != bundle.version:
        raise ContractValidationError(
            "getProviderInfo",
            _get_request_id(None, body),
            f"contractVersion == '{bundle.version}'",
            contract_ver,
            "Provider contractVersion does not match pinned bundle version",
        )
    ctx["info_data"] = body.get("data", {})


@register_case("CT-GEN-005", "Standard error envelope on non-existent resource", "General")
def test_gen_005(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(
        client,
        bundle,
        "GET",
        "/devices/non-existent-device-id-999999",
        "getProviderDevice",
        404,
    )
    err_code = body.get("error", {}).get("code")
    if err_code != "VIDEO_DEVICE_NOT_FOUND":
        raise ContractValidationError(
            "getProviderDevice",
            _get_request_id(None, body),
            "error code VIDEO_DEVICE_NOT_FOUND",
            err_code,
            "Querying non-existent device must return VIDEO_DEVICE_NOT_FOUND",
        )


# ---------------------------------------------------------------- Devices & Catalog


@register_case("CT-DEV-001", "List and get provider devices", "Devices")
def test_dev_001(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    _, body = _request_and_validate(client, bundle, "GET", "/devices", "listProviderDevices", 200)
    items = body.get("data", {}).get("items", [])
    if not items:
        return
    dev_id = items[0]["externalDeviceId"]
    ctx["test_device_id"] = dev_id

    _, dev_body = _request_and_validate(
        client, bundle, "GET", f"/devices/{dev_id}", "getProviderDevice", 200
    )
    if dev_body.get("data", {}).get("externalDeviceId") != dev_id:
        raise ContractValidationError(
            "getProviderDevice",
            _get_request_id(None, dev_body),
            f"externalDeviceId == '{dev_id}'",
            dev_body.get("data", {}).get("externalDeviceId"),
            "Fetched device ID mismatch",
        )


@register_case("CT-DEV-002", "List provider device channels with pagination", "Devices")
def test_dev_002(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    _, body = _request_and_validate(
        client, bundle, "GET", f"/devices/{dev_id}/channels", "listProviderDeviceChannels", 200
    )
    items = body.get("data", {}).get("items", [])
    if items:
        ctx["test_channel_id"] = items[0]["externalChannelId"]


@register_case("CT-DEV-003", "Get provider device status", "Devices")
def test_dev_003(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    _, body = _request_and_validate(
        client, bundle, "GET", f"/devices/{dev_id}/status", "getProviderDeviceStatus", 200
    )
    status = body.get("data", {}).get("onlineStatus")
    if status not in ("ONLINE", "OFFLINE", "UNKNOWN"):
        raise ContractValidationError(
            "getProviderDeviceStatus",
            _get_request_id(None, body),
            "onlineStatus in (ONLINE, OFFLINE, UNKNOWN)",
            status,
            "Invalid onlineStatus value",
        )


@register_case(
    "CT-DEV-004",
    "Submit and poll catalog sync operation",
    "Catalog",
    required_capability="CATALOG_SYNC",
)
def test_dev_004(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    idem_key = uuid.uuid4().hex
    _, submit_body = _request_and_validate(
        client,
        bundle,
        "POST",
        f"/devices/{dev_id}/catalog-syncs",
        "createProviderCatalogSync",
        202,
        headers={"Idempotency-Key": idem_key},
    )
    op_id = submit_body.get("data", {}).get("operationId")
    if not op_id:
        raise ContractValidationError(
            "createProviderCatalogSync",
            _get_request_id(None, submit_body),
            "operationId in response data",
            submit_body.get("data"),
            "Catalog sync submit response must contain operationId",
        )

    # 轮询到完成
    deadline = time.monotonic() + 5.0
    last_body = None
    while time.monotonic() < deadline:
        _, poll_body = _request_and_validate(
            client, bundle, "GET", f"/catalog-syncs/{op_id}", "getProviderCatalogSync", 200
        )
        last_body = poll_body
        st = poll_body.get("data", {}).get("status")
        if st in ("SUCCEEDED", "FAILED", "PARTIAL", "EXPIRED"):
            break
        time.sleep(0.05)

    final_status = last_body.get("data", {}).get("status") if last_body else None
    if final_status not in ("SUCCEEDED", "PARTIAL"):
        raise ContractValidationError(
            "getProviderCatalogSync",
            _get_request_id(None, last_body),
            "Catalog sync status SUCCEEDED or PARTIAL",
            final_status,
            f"Catalog sync operation ended with status {final_status}",
        )


# ---------------------------------------------------------------- Live Stream


@register_case(
    "CT-LIVE-001",
    "Start, query and stop live stream",
    "Live",
    required_capability="LIVE_STREAM",
)
def test_live_001(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    ch_id = ctx.get("test_channel_id", "34020000001310000001")
    idem_key = uuid.uuid4().hex

    _, start_body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/live-streams",
        "startLiveStream",
        (200, 201),
        headers={"Idempotency-Key": idem_key},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": ch_id,
            "streamProfile": "AUTO",
        },
    )
    stream_key = start_body.get("data", {}).get("providerStreamKey")
    if not stream_key:
        raise ContractValidationError(
            "startLiveStream",
            _get_request_id(None, start_body),
            "providerStreamKey in data",
            start_body.get("data"),
            "Live stream creation must return providerStreamKey",
        )

    _, get_body = _request_and_validate(
        client, bundle, "GET", f"/live-streams/{stream_key}", "getLiveStream", 200
    )
    if get_body.get("data", {}).get("providerStreamKey") != stream_key:
        raise ContractValidationError(
            "getLiveStream",
            _get_request_id(None, get_body),
            f"providerStreamKey == '{stream_key}'",
            get_body.get("data"),
            "Get live stream key mismatch",
        )

    _request_and_validate(
        client, bundle, "DELETE", f"/live-streams/{stream_key}", "stopLiveStream", 204
    )


@register_case(
    "CT-LIVE-005",
    "Reject live stream start for non-existent channel",
    "Live",
    required_capability="LIVE_STREAM",
)
def test_live_005(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    _, body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/live-streams",
        "startLiveStream",
        404,
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": "34020000001310009999",
            "streamProfile": "AUTO",
        },
    )
    err_code = body.get("error", {}).get("code")
    if err_code != "VIDEO_CHANNEL_NOT_FOUND":
        raise ContractValidationError(
            "startLiveStream",
            _get_request_id(None, body),
            "error code VIDEO_CHANNEL_NOT_FOUND",
            err_code,
            "Non-existent channel must return VIDEO_CHANNEL_NOT_FOUND",
        )


@register_case(
    "CT-LIVE-008",
    "Stop live stream idempotency",
    "Live",
    required_capability="LIVE_STREAM",
)
def test_live_008(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    ch_id = ctx.get("test_channel_id", "34020000001310000001")
    _, start_body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/live-streams",
        "startLiveStream",
        (200, 201),
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": ch_id,
            "streamProfile": "AUTO",
        },
    )
    stream_key = start_body["data"]["providerStreamKey"]
    _request_and_validate(
        client, bundle, "DELETE", f"/live-streams/{stream_key}", "stopLiveStream", 204
    )
    # 重复删除，依然幂等返回 204
    _request_and_validate(
        client, bundle, "DELETE", f"/live-streams/{stream_key}", "stopLiveStream", 204
    )


# ---------------------------------------------------------------- Records & Playback


@register_case(
    "CT-REC-001",
    "Submit and poll device record query",
    "Records",
    required_capability="DEVICE_RECORD_QUERY",
)
def test_rec_001(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    dev_id = ctx.get("test_device_id", "34020000001320000001")
    ch_id = ctx.get("test_channel_id", "34020000001310000001")
    _, submit_body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/device-record-queries",
        "createDeviceRecordQuery",
        202,
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": ch_id,
            "startTime": "2026-08-01T00:00:00.000Z",
            "endTime": "2026-08-01T02:00:00.000Z",
            "recordType": "ALL",
        },
    )
    query_id = submit_body.get("data", {}).get("queryId")
    if not query_id:
        raise ContractValidationError(
            "createDeviceRecordQuery",
            _get_request_id(None, submit_body),
            "queryId in data",
            submit_body.get("data"),
            "Record query submit response must contain queryId",
        )

    deadline = time.monotonic() + 5.0
    last_body = None
    while time.monotonic() < deadline:
        _, poll_body = _request_and_validate(
            client,
            bundle,
            "GET",
            f"/device-record-queries/{query_id}",
            "getDeviceRecordQuery",
            200,
        )
        last_body = poll_body
        if poll_body.get("data", {}).get("status") in ("SUCCEEDED", "PARTIAL", "FAILED"):
            break
        time.sleep(0.05)

    items = last_body.get("data", {}).get("items", []) if isinstance(last_body, dict) else []
    if items:
        ctx["test_record_key"] = items[0]["recordKey"]


@register_case(
    "CT-REC-002",
    "Start, query and stop playback stream",
    "Playback",
    required_capability="DEVICE_RECORD_PLAYBACK",
)
def test_rec_002(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    record_key = ctx.get("test_record_key")
    if not record_key:
        return

    dev_id = ctx.get("test_device_id", "34020000001320000001")
    ch_id = ctx.get("test_channel_id", "34020000001310000001")

    _, start_body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/playback-streams",
        "startPlaybackStream",
        (200, 201),
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": ch_id,
            "recordKey": record_key,
        },
    )
    stream_key = start_body.get("data", {}).get("providerStreamKey")
    if not stream_key:
        raise ContractValidationError(
            "startPlaybackStream",
            _get_request_id(None, start_body),
            "providerStreamKey in data",
            start_body.get("data"),
            "Playback stream start must return providerStreamKey",
        )

    _request_and_validate(
        client, bundle, "GET", f"/playback-streams/{stream_key}", "getPlaybackStream", 200
    )

    _request_and_validate(
        client, bundle, "DELETE", f"/playback-streams/{stream_key}", "stopPlaybackStream", 204
    )


@register_case(
    "CT-REC-006",
    "Reject playback start for mismatched channel and recordKey",
    "Playback",
    required_capability="DEVICE_RECORD_PLAYBACK",
)
def test_rec_006(client: httpx.Client, bundle: ContractBundle, ctx: dict[str, Any]) -> None:
    record_key = ctx.get("test_record_key")
    if not record_key:
        return

    dev_id = ctx.get("test_device_id", "34020000001320000001")
    # 使用不匹配的 channel
    mismatched_ch = "34020000001310000002"

    _, body = _request_and_validate(
        client,
        bundle,
        "POST",
        "/playback-streams",
        "startPlaybackStream",
        409,
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json_body={
            "externalDeviceId": dev_id,
            "externalChannelId": mismatched_ch,
            "recordKey": record_key,
        },
    )
    err_code = body.get("error", {}).get("code")
    if err_code != "VIDEO_RECORD_MISMATCH":
        raise ContractValidationError(
            "startPlaybackStream",
            _get_request_id(None, body),
            "error code VIDEO_RECORD_MISMATCH",
            err_code,
            "Channel and recordKey mismatch must return VIDEO_RECORD_MISMATCH",
        )
