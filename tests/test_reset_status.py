"""控制面：reset / 场景 / 设备在线状态注入 / ready 注入 / 事件。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of

NVR = "34020000001320000001"
IPC = "34020000001320000002"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def test_reset_returns_counts(client: httpx.Client) -> None:
    resp = client.post("/testkit/v1/reset")
    data = data_of(resp)
    assert data["status"] == "RESET"
    assert data["devices"] == 2


def test_scenario_summary(client: httpx.Client) -> None:
    data = data_of(client.get("/testkit/v1/scenario"))
    assert data["name"] == "ipc-nvr-4ch"
    assert set(data["devices"]) == {NVR, IPC}


def test_testkit_devices_list(client: httpx.Client) -> None:
    data = data_of(client.get("/testkit/v1/devices"))
    items = data["items"]
    assert len(items) == 2
    by_id = {d["externalDeviceId"]: d for d in items}
    assert by_id[NVR]["channelCount"] == 4
    assert by_id[NVR]["sourceName"] == "测试NVR-01"
    assert by_id[NVR]["simulator"]["status"] == "IDLE"


def test_simulator_status_initial(client: httpx.Client) -> None:
    data = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert data["externalDeviceId"] == NVR
    assert data["registered"] is False
    assert data["status"] == "IDLE"
    assert data["attemptCount"] == 0


def test_set_device_online_status(client: httpx.Client) -> None:
    resp = client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    assert data_of(resp)["onlineStatus"] == "OFFLINE"

    prov = data_of(client.get(f"/provider/v1/devices/{NVR}/status"))
    assert prov["onlineStatus"] == "OFFLINE"
    assert prov["externalDeviceId"] == NVR

    events = data_of(client.get("/testkit/v1/events"))["items"]
    assert any(
        e["eventType"] == "DEVICE_OFFLINE" and e["resource"]["externalDeviceId"] == NVR
        for e in events
    )

    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "ONLINE"})
    assert data_of(client.get(f"/provider/v1/devices/{NVR}/status"))["onlineStatus"] == "ONLINE"


def test_set_online_status_invalid(client: httpx.Client) -> None:
    resp = client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "BOGUS"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_ready_override(client: httpx.Client) -> None:
    resp = client.post("/testkit/v1/ready", json={"ready": False})
    assert data_of(resp) == {"status": "NOT_READY"}

    not_ready = client.get("/provider/v1/health/ready")
    assert not_ready.status_code == 503
    assert data_of(not_ready, expected_status=503) == {"status": "NOT_READY"}

    client.post("/testkit/v1/ready", json={"ready": True})
    ready = client.get("/provider/v1/health/ready")
    assert ready.status_code == 200
    assert data_of(ready) == {"status": "READY"}


def test_reset_clears_events_and_override(client: httpx.Client) -> None:
    client.post("/testkit/v1/ready", json={"ready": False})
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    assert data_of(client.get("/testkit/v1/events"))["items"]
    assert client.get("/provider/v1/health/ready").status_code == 503

    client.post("/testkit/v1/reset")
    assert data_of(client.get("/testkit/v1/events"))["items"] == []
    assert client.get("/provider/v1/health/ready").status_code == 200
