"""Idempotency-Key 语义：缺失 400、同键同请求复用、同键不同请求 409。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"
NVR_CH2 = "34020000001310000002"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _live_body(channel: str) -> dict:
    return {"externalDeviceId": NVR, "externalChannelId": channel}


def test_missing_idem_key_400(client: httpx.Client) -> None:
    resp = client.post("/provider/v1/live-streams", json=_live_body(NVR_CH1))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "VIDEO_INVALID_ARGUMENT"
    assert "Idempotency-Key" in error["message"]


def test_same_key_same_body_reuses(client: httpx.Client) -> None:
    key = uuid.uuid4().hex
    r1 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH1)
    )
    r2 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH1)
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["data"]["providerStreamKey"] == r2.json()["data"]["providerStreamKey"]


def test_same_key_different_body_conflict(client: httpx.Client) -> None:
    key = uuid.uuid4().hex
    r1 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH1)
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH2)
    )
    assert r2.status_code == 409
    error = r2.json()["error"]
    assert error["code"] == "VIDEO_IDEMPOTENCY_CONFLICT"
    assert error["retryable"] is False


def test_distinct_keys_are_independent(client: httpx.Client) -> None:
    r1 = client.post(
        "/provider/v1/live-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json=_live_body(NVR_CH1),
    )
    r2 = client.post(
        "/provider/v1/live-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json=_live_body(NVR_CH1),
    )
    assert r1.status_code == 201
    assert r2.status_code == 200  # 同一活动通道 → 复用，非新建
    assert r1.json()["data"]["providerStreamKey"] == r2.json()["data"]["providerStreamKey"]


def test_reset_clears_idempotency_store(client: httpx.Client) -> None:
    key = uuid.uuid4().hex
    r1 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH1)
    )
    assert r1.status_code == 201
    client.post("/testkit/v1/reset")
    r2 = client.post(
        "/provider/v1/live-streams", headers={"Idempotency-Key": key}, json=_live_body(NVR_CH1)
    )
    assert r2.status_code == 201  # reset 后同 Key 可再次使用
