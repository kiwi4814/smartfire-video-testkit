"""实时流 / 设备录像查询 / 回放流：创建、复用、删除幂等、错误路径。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"
NVR_CH2 = "34020000001310000002"
IPC = "34020000001320000002"
IPC_CH1 = "34020000001310000021"

QUERY_START = "2026-08-01T00:00:00Z"
QUERY_END = "2026-08-01T02:30:00Z"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _start_live(
    client: httpx.Client, device: str, channel: str, key: str | None = None
) -> httpx.Response:
    return client.post(
        "/provider/v1/live-streams",
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
        json={
            "externalDeviceId": device,
            "externalChannelId": channel,
            "streamProfile": "MAIN",
        },
    )


def _create_record_query(client: httpx.Client, device: str, channel: str) -> dict:
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

    result = wait_until_value(get_query, lambda d: d["status"] == "SUCCEEDED", timeout=5.0)
    return result


# ---------------------------------------------------------------- 实时流


def test_live_stream_create_and_get(client: httpx.Client) -> None:
    resp = _start_live(client, NVR, NVR_CH1)
    stream = data_of(resp, expected_status=201)
    assert stream["streamType"] == "LIVE"
    assert stream["state"] == "STREAMING"
    assert stream["streamProfile"] == "MAIN"
    assert stream["externalDeviceId"] == NVR
    assert stream["externalChannelId"] == NVR_CH1
    assert stream["media"]["mediaServerId"] == "zlm-mock-01"
    assert stream["media"]["streamId"]
    assert len(stream["sources"]) >= 2
    assert all(s["url"].startswith("http://") for s in stream["sources"])

    key = stream["providerStreamKey"]
    got = data_of(client.get(f"/provider/v1/live-streams/{key}"))
    assert got["providerStreamKey"] == key


def test_live_stream_reuse_same_key(client: httpx.Client) -> None:
    key = uuid.uuid4().hex
    r1 = _start_live(client, NVR, NVR_CH1, key=key)
    r2 = _start_live(client, NVR, NVR_CH1, key=key)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["data"]["providerStreamKey"] == r2.json()["data"]["providerStreamKey"]


def test_live_stream_reuse_active_channel(client: httpx.Client) -> None:
    r1 = _start_live(client, NVR, NVR_CH1)
    r2 = _start_live(client, NVR, NVR_CH1)  # 新 Key，同一活动通道 → 复用 200
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["data"]["providerStreamKey"] == r2.json()["data"]["providerStreamKey"]


def test_live_stream_delete_idempotent(client: httpx.Client) -> None:
    stream = data_of(_start_live(client, NVR, NVR_CH1), expected_status=201)
    key = stream["providerStreamKey"]
    assert client.delete(f"/provider/v1/live-streams/{key}").status_code == 204
    assert client.get(f"/provider/v1/live-streams/{key}").status_code == 404
    assert client.delete(f"/provider/v1/live-streams/{key}").status_code == 204


def test_live_stream_missing_idem_key(client: httpx.Client) -> None:
    resp = client.post(
        "/provider/v1/live-streams",
        json={"externalDeviceId": NVR, "externalChannelId": NVR_CH1},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_live_stream_unknown_channel(client: httpx.Client) -> None:
    resp = _start_live(client, NVR, "34020000001310009999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIDEO_CHANNEL_NOT_FOUND"


# ---------------------------------------------------------------- 设备录像查询


def test_record_query_hourly_items(client: httpx.Client) -> None:
    result = _create_record_query(client, NVR, NVR_CH1)
    assert len(result["items"]) == 3
    for item in result["items"]:
        assert item["recordType"] == "TIME"
        assert item["externalDeviceId"] == NVR
        assert item["externalChannelId"] == NVR_CH1
        assert item["recordKey"].startswith("rec-")
    assert result["items"][0]["startTime"] == QUERY_START
    assert result["items"][-1]["endTime"] == QUERY_END


def test_record_query_invalid_type(client: httpx.Client) -> None:
    resp = client.post(
        "/provider/v1/device-record-queries",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "startTime": QUERY_START,
            "endTime": QUERY_END,
            "recordType": "ALARM",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


# ---------------------------------------------------------------- 回放流


def test_playback_stream_lifecycle(client: httpx.Client) -> None:
    records = _create_record_query(client, NVR, NVR_CH1)
    record_key = records["items"][0]["recordKey"]

    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "recordKey": record_key,
        },
    )
    stream = data_of(resp, expected_status=201)
    assert stream["streamType"] == "PLAYBACK"
    assert stream["state"] == "STREAMING"
    assert stream["streamProfile"] is None
    assert stream["media"]["app"] == "playback"

    key = stream["providerStreamKey"]
    got = data_of(client.get(f"/provider/v1/playback-streams/{key}"))
    assert got["providerStreamKey"] == key

    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    assert client.get(f"/provider/v1/playback-streams/{key}").status_code == 404


def test_playback_record_mismatch(client: httpx.Client) -> None:
    records = _create_record_query(client, NVR, NVR_CH1)
    record_key = records["items"][0]["recordKey"]
    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": IPC,
            "externalChannelId": IPC_CH1,
            "recordKey": record_key,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VIDEO_RECORD_MISMATCH"


def test_playback_record_not_found(client: httpx.Client) -> None:
    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "recordKey": "rec-does-not-exist",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIDEO_RECORD_NOT_FOUND"


def test_playback_stop_idempotent(client: httpx.Client) -> None:
    records = _create_record_query(client, NVR, NVR_CH1)
    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "recordKey": records["items"][0]["recordKey"],
        },
    )
    key = data_of(resp, expected_status=201)["providerStreamKey"]
    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    assert client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
