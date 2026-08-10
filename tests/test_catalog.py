"""Catalog 异步操作机：202 接受、轮询 SUCCEEDED、幂等复用、错误路径。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

NVR = "34020000001320000001"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def test_catalog_sync_succeeds(client: httpx.Client) -> None:
    resp = client.post(
        f"/provider/v1/devices/{NVR}/catalog-syncs",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    accepted = data_of(resp, expected_status=202)
    assert accepted["status"] == "ACCEPTED"
    operation_id = accepted["operationId"]
    assert operation_id.startswith("catalog-")

    def get_operation() -> dict:
        return data_of(client.get(f"/provider/v1/catalog-syncs/{operation_id}"))

    result = wait_until_value(get_operation, lambda d: d["status"] == "SUCCEEDED", timeout=5.0)
    assert result["externalDeviceId"] == NVR
    assert result["discoveredCount"] == 4
    assert result["completedAt"]


def test_catalog_sync_idempotent_reuse(client: httpx.Client) -> None:
    key = uuid.uuid4().hex
    r1 = client.post(f"/provider/v1/devices/{NVR}/catalog-syncs", headers={"Idempotency-Key": key})
    r2 = client.post(f"/provider/v1/devices/{NVR}/catalog-syncs", headers={"Idempotency-Key": key})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["data"]["operationId"] == r2.json()["data"]["operationId"]


def test_catalog_sync_missing_idem_key(client: httpx.Client) -> None:
    resp = client.post(f"/provider/v1/devices/{NVR}/catalog-syncs")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_catalog_sync_offline_device(client: httpx.Client) -> None:
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    resp = client.post(
        f"/provider/v1/devices/{NVR}/catalog-syncs",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VIDEO_DEVICE_OFFLINE"


def test_catalog_operation_not_found(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/catalog-syncs/catalog-does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VIDEO_OPERATION_NOT_FOUND"
