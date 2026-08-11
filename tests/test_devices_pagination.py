"""设备与通道：分页、过滤、稳定排序、404（真实 HTTP）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import data_of

NVR = "34020000001320000001"
IPC = "34020000001320000002"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def test_list_devices_sorted(client: httpx.Client) -> None:
    data = data_of(client.get("/provider/v1/devices"))
    ids = [d["externalDeviceId"] for d in data["items"]]
    assert ids == sorted(ids)
    assert ids == [NVR, IPC]
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["pageSize"] == 100


def test_devices_pagination(client: httpx.Client) -> None:
    p1 = data_of(client.get("/provider/v1/devices?page=1&pageSize=1"))
    assert [d["externalDeviceId"] for d in p1["items"]] == [NVR]
    assert p1["total"] == 2

    p2 = data_of(client.get("/provider/v1/devices?page=2&pageSize=1"))
    assert [d["externalDeviceId"] for d in p2["items"]] == [IPC]
    assert p2["total"] == 2

    p3 = data_of(client.get("/provider/v1/devices?page=3&pageSize=1"))
    assert p3["items"] == []
    assert p3["total"] == 2


def test_devices_page_size_bounds(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/devices?pageSize=501")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"

    resp = client.get("/provider/v1/devices?page=0")
    assert resp.status_code == 400


def test_devices_filter_query(client: httpx.Client) -> None:
    data = data_of(client.get("/provider/v1/devices?query=%E6%B5%8B%E8%AF%95NVR"))
    assert [d["externalDeviceId"] for d in data["items"]] == [NVR]


def test_devices_filter_online_status(client: httpx.Client) -> None:
    client.post(f"/testkit/v1/devices/{IPC}/status", json={"onlineStatus": "OFFLINE"})
    online = data_of(client.get("/provider/v1/devices?onlineStatus=ONLINE"))
    assert [d["externalDeviceId"] for d in online["items"]] == [NVR]
    offline = data_of(client.get("/provider/v1/devices?onlineStatus=OFFLINE"))
    assert [d["externalDeviceId"] for d in offline["items"]] == [IPC]

    resp = client.get("/provider/v1/devices?onlineStatus=BOGUS")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_devices_filter_updated_after(client: httpx.Client) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    data = data_of(client.get(f"/provider/v1/devices?updatedAfter={future}"))
    assert data["items"] == []
    assert data["total"] == 0


def test_get_device_detail(client: httpx.Client) -> None:
    data = data_of(client.get(f"/provider/v1/devices/{NVR}"))
    assert data["externalDeviceId"] == NVR
    assert data["sourceName"] == "测试NVR-01"
    assert data["transport"] == "UDP"
    assert data["streamMode"] == "UDP"
    assert data["onlineStatus"] == "ONLINE"
    assert data["channelCount"] == 4


def test_channels_pagination(client: httpx.Client) -> None:
    all_ch = data_of(client.get(f"/provider/v1/devices/{NVR}/channels"))
    assert all_ch["total"] == 4
    assert len(all_ch["items"]) == 4
    ids = [c["externalChannelId"] for c in all_ch["items"]]
    assert ids == sorted(ids)

    p1 = data_of(client.get(f"/provider/v1/devices/{NVR}/channels?page=1&pageSize=2"))
    assert len(p1["items"]) == 2
    assert p1["total"] == 4
    p2 = data_of(client.get(f"/provider/v1/devices/{NVR}/channels?page=2&pageSize=2"))
    assert len(p2["items"]) == 2
    assert p2["total"] == 4


def test_device_not_found(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/devices/00000000000000000000")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "VIDEO_DEVICE_NOT_FOUND"
    assert error["retryable"] is False
