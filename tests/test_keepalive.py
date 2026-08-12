"""Keepalive(真实 SIP MESSAGE + XML):

1. XML 编解码往返与畸形拒绝(单元);
2. 有效 Keepalive 驱动离线设备恢复 ONLINE,并观察 Provider 200 响应;
3. pause 在有界时间内驱动 OFFLINE;
4. 单次 drop(丢包)不影响心跳循环,设备保持 ONLINE;
5. 畸形 Keepalive 不作为有效心跳,超时后收敛 OFFLINE;
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until, wait_until_value

from video_testkit.sip.keepalive import build_keepalive_xml, parse_keepalive_xml

NVR = "34020000001320000001"


def _provider_status(client: httpx.Client) -> str:
    return data_of(client.get(f"/provider/v1/devices/{NVR}/status"))["onlineStatus"]


def _event_types(client: httpx.Client) -> list[str]:
    return [e["eventType"] for e in data_of(client.get("/testkit/v1/events"))["items"]]


def _registrar_messages(client: httpx.Client, status: int | None = None) -> list[dict]:
    items = data_of(client.get("/testkit/v1/sip/registrar/requests"))["items"]
    messages = [e for e in items if e["method"] == "MESSAGE"]
    if status is not None:
        messages = [e for e in messages if e["status"] == status]
    return messages


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


# ---------------------------------------------------------------- XML 编解码


def test_keepalive_xml_roundtrip() -> None:
    xml = build_keepalive_xml(NVR, 42)
    data = parse_keepalive_xml(xml)
    assert data.device_id == NVR
    assert data.sn == 42
    assert data.status == "OK"

    with pytest.raises(ValueError):
        parse_keepalive_xml("<broken><xml>")
    with pytest.raises(ValueError):
        parse_keepalive_xml("<Notify><CmdType>Keepalive</CmdType></Notify>")


# ---------------------------------------------------------------- 有效心跳驱动 ONLINE


def test_keepalive_drives_online_with_200(client: httpx.Client) -> None:
    """离线设备收到有效 Keepalive 后恢复 ONLINE; Registrar 响应 200."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    assert _provider_status(client) == "OFFLINE"

    view = data_of(client.post(f"/testkit/v1/devices/{NVR}/keepalive/start"))
    assert view["keepaliveActive"] is True

    status = wait_until_value(
        lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0
    )
    assert status == "ONLINE"

    # Provider 对 Keepalive MESSAGE 响应 200(真实 UDP 应答可观察)
    wait_until(lambda: bool(_registrar_messages(client, status=200)), timeout=6.0)
    assert _registrar_messages(client, status=200)


# ---------------------------------------------------------------- pause 驱动 OFFLINE


def test_keepalive_pause_drives_offline(client: httpx.Client) -> None:
    """pause 停止心跳后,设备在有界时间内收敛 OFFLINE."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/start")
    status = wait_until_value(
        lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0
    )
    assert status == "ONLINE"

    view = data_of(client.post(f"/testkit/v1/devices/{NVR}/keepalive/pause"))
    assert view["keepaliveActive"] is False

    status = wait_until_value(
        lambda: _provider_status(client), lambda s: s == "OFFLINE", timeout=8.0
    )
    assert status == "OFFLINE"


def test_keepalive_resume_restores_online(client: httpx.Client) -> None:
    """resume 后同一设备身份恢复 ONLINE."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/start")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0)

    client.post(f"/testkit/v1/devices/{NVR}/keepalive/pause")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "OFFLINE", timeout=8.0)

    view = data_of(client.post(f"/testkit/v1/devices/{NVR}/keepalive/resume"))
    assert view["keepaliveActive"] is True
    status = wait_until_value(
        lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0
    )
    assert status == "ONLINE"


# ---------------------------------------------------------------- 单次丢包


def test_keepalive_drop_single_keeps_online(client: httpx.Client) -> None:
    """drop 跳过下一次心跳(单次丢包),随后正常心跳继续,设备保持 ONLINE."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/start")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0)

    view = data_of(client.post(f"/testkit/v1/devices/{NVR}/keepalive/drop"))
    assert view["keepaliveActive"] is True

    # 跨过发送周期:验证设备持续保持 ONLINE
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        assert _provider_status(client) == "ONLINE", "单次丢包不应导致设备离线"
        time.sleep(0.3)


# ---------------------------------------------------------------- 畸形心跳


def test_keepalive_malformed_not_a_heartbeat(client: httpx.Client) -> None:
    """畸形 Keepalive 被 Registrar 拒绝(400),不刷新心跳,设备最终收敛 OFFLINE."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/start")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0)

    # 停止正常心跳,只发送畸形报文
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/pause")
    client.post(f"/testkit/v1/devices/{NVR}/keepalive/malformed")

    wait_until(lambda: bool(_registrar_messages(client, status=400)), timeout=6.0)
    assert _registrar_messages(client, status=400)

    status = wait_until_value(
        lambda: _provider_status(client), lambda s: s == "OFFLINE", timeout=8.0
    )
    assert status == "OFFLINE"


# ---------------------------------------------------------------- 事件顺序与 reset


def test_keepalive_events_ordered_and_reset(client: httpx.Client) -> None:
    """State events recorded in order, repeatable and reset cleared."""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    assert _event_types(client) == ["DEVICE_OFFLINE"]

    client.post(f"/testkit/v1/devices/{NVR}/keepalive/start")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0)

    client.post(f"/testkit/v1/devices/{NVR}/keepalive/pause")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "OFFLINE", timeout=8.0)

    client.post(f"/testkit/v1/devices/{NVR}/keepalive/resume")
    wait_until_value(lambda: _provider_status(client), lambda s: s == "ONLINE", timeout=6.0)

    events = data_of(client.get("/testkit/v1/events"))["items"]
    types = [e["eventType"] for e in events]
    # 重复 OFFLINE/ONLINE 状态变化全部记录,顺序与发生一致
    assert types == ["DEVICE_OFFLINE", "DEVICE_ONLINE", "DEVICE_OFFLINE", "DEVICE_ONLINE"]
    revisions = [e["revision"] for e in events]
    assert all(int(revisions[i]) < int(revisions[i + 1]) for i in range(len(revisions) - 1)), (
        "事件 revision 必须单调递增"
    )

    client.post("/testkit/v1/reset")
    assert data_of(client.get("/testkit/v1/events"))["items"] == []
    sim = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert sim["keepaliveActive"] is False
