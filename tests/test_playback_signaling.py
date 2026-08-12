"""回放流 SIP 信令与场景（VT-08）：真实 INVITE/SDP/ACK/BYE 生命周期。

公共 seam：``/testkit/v1`` 编排设备回放应答场景，``/provider/v1`` playback-streams
是结果 seam，真实 SIP UDP 信令是协议 seam。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"
IPC = "34020000001320000002"
IPC_CH1 = "34020000001310000021"

QUERY_START = "2026-08-01T00:00:00.000Z"
QUERY_END = "2026-08-01T02:30:00.000Z"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _create_record(client: httpx.Client, device: str = NVR, channel: str = NVR_CH1) -> str:
    resp = client.post(
        "/provider/v1/device-record-queries",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": device,
            "externalChannelId": channel,
            "startTime": QUERY_START,
            "endTime": QUERY_END,
            "recordType": "ALL",
        },
    )
    query_id = data_of(resp, expected_status=202)["queryId"]

    def get_query() -> dict:
        return data_of(client.get(f"/provider/v1/device-record-queries/{query_id}"))

    result = wait_until_value(get_query, lambda d: d["status"] == "SUCCEEDED", timeout=8.0)
    return result["items"][0]["recordKey"]


def _start_playback(
    client: httpx.Client,
    record_key: str,
    device: str = NVR,
    channel: str = NVR_CH1,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
        json={
            "externalDeviceId": device,
            "externalChannelId": channel,
            "recordKey": record_key,
        },
    )


def _configure_playback(client: httpx.Client, device_id: str = NVR, **kwargs: object) -> dict:
    resp = client.post(f"/testkit/v1/devices/{device_id}/playback", json=kwargs)
    return data_of(resp, expected_status=200)


def _device_playback(client: httpx.Client, device_id: str = NVR) -> dict:
    return data_of(client.get(f"/testkit/v1/devices/{device_id}/playback"))


def _stream_state(client: httpx.Client, stream_key: str) -> str:
    return data_of(client.get(f"/provider/v1/playback-streams/{stream_key}"))["state"]


def _wait_dialog(
    client: httpx.Client, predicate, device_id: str = NVR, timeout: float = 6.0
) -> list[dict]:
    def dialogs() -> list[dict]:
        return _device_playback(client, device_id)["dialogs"]

    return wait_until_value(dialogs, predicate, timeout=timeout)


# ---------------------------------------------------------------- Mismatch / 异常路径


def test_playback_mismatch_errors(client: httpx.Client) -> None:
    """不存在 recordKey 报 404；device/channel mismatch 报 409。"""
    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "recordKey": "rec-nonexistent-key",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIDEO_RECORD_NOT_FOUND"

    record_key = _create_record(client, NVR, NVR_CH1)
    mismatch_resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": IPC,
            "externalChannelId": IPC_CH1,
            "recordKey": record_key,
        },
    )
    assert mismatch_resp.status_code == 409
    assert mismatch_resp.json()["error"]["code"] == "VIDEO_RECORD_MISMATCH"


# ---------------------------------------------------------------- 正常 Dialog & 信令证据


def test_playback_dialog_reaches_established_then_terminated(client: httpx.Client) -> None:
    """正常回放 INVITE(s=Playback)/200/ACK 达到 ESTABLISHED，DELETE 后 BYE 到达 TERMINATED。"""
    record_key = _create_record(client)
    resp = _start_playback(client, record_key)
    stream = data_of(resp, expected_status=201)
    key = stream["providerStreamKey"]
    assert stream["streamType"] == "PLAYBACK"

    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    dialog = next(d for d in dialogs if d["status"] == "ESTABLISHED")
    assert dialog["deviceId"] == NVR
    assert dialog["ssrc"]

    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_playback_stream_real_sip_evidence(client: httpx.Client) -> None:
    """SIP seam 证据：设备经真实 UDP 收到回放 INVITE，ACK 到达后 Dialog ESTABLISHED。"""
    record_key = _create_record(client)
    _start_playback(client, record_key)
    _wait_dialog(client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds))
    pb = _device_playback(client)
    assert pb["invitesReceived"] >= 1
    established = next(d for d in pb["dialogs"] if d["status"] == "ESTABLISHED")
    assert established["ackReceived"] is True


# ---------------------------------------------------------------- 失败场景编排


def test_playback_rejection_maps_to_failed(client: httpx.Client) -> None:
    """设备回 486：Provider playback stream 最终 FAILED。"""
    record_key = _create_record(client)
    _configure_playback(client, mode="rejection")
    resp = _start_playback(client, record_key)
    stream = data_of(resp, expected_status=201)
    key = stream["providerStreamKey"]

    def state() -> str:
        return _stream_state(client, key)

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


def test_playback_invite_timeout_maps_to_failed(client: httpx.Client) -> None:
    """设备不响应回放 INVITE：Provider playback stream 最终 FAILED。"""
    record_key = _create_record(client)
    _configure_playback(client, mode="drop")
    resp = _start_playback(client, record_key)
    stream = data_of(resp, expected_status=201)

    def state() -> str:
        return _stream_state(client, stream["providerStreamKey"])

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


def test_playback_missing_ack_is_observable_failure(client: httpx.Client) -> None:
    """设备 200 后未收到 ACK：设备侧 Dialog 以 FAILED 状态可观察。"""
    record_key = _create_record(client)
    _configure_playback(client, mode="no-ack")
    _start_playback(client, record_key)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "FAILED" and not d["ackReceived"] for d in ds)
    )
    assert dialogs


# ---------------------------------------------------------------- 幂等与清理


def test_playback_repeated_start_and_stop_idempotent(client: httpx.Client) -> None:
    """相同 Idempotency-Key 重复 start 返回相同流；重复 DELETE 均 204。"""
    record_key = _create_record(client)
    idem_key = uuid.uuid4().hex
    r1 = _start_playback(client, record_key, key=idem_key)
    r2 = _start_playback(client, record_key, key=idem_key)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["data"]["providerStreamKey"] == r2.json()["data"]["providerStreamKey"]

    key = r1.json()["data"]["providerStreamKey"]
    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_playback_reset_clears_dialogs_and_scenario(client: httpx.Client) -> None:
    """reset 后 Playback Dialog 与场景编排清空。"""
    record_key = _create_record(client)
    _configure_playback(client, mode="rejection")
    _start_playback(client, record_key)
    wait_until_value(
        lambda: _device_playback(client)["invitesReceived"], lambda n: n >= 1, timeout=6.0
    )

    client.post("/testkit/v1/reset")
    assert _device_playback(client)["dialogs"] == []
    assert _device_playback(client)["mode"] == "normal"
