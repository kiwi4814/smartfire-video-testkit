"""ZLM 集成冒烟（VT-06）：真实媒体到达验证。

仅在显式配置 ``VIDEO_TESTKIT_ZLM_API_URL`` 时运行（conftest zlm_server
未配置则整体 skip）。断言均经真实网络：
- Provider start → ZLM RTP 端口 → SIP INVITE → 设备推流 → ZLM stream-online → STREAMING
- 失败/清理路径强制关闭 ZLM 端口，重复 reset 幂等且无 orphan。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"

_ZLM_API = os.environ.get("VIDEO_TESTKIT_ZLM_API_URL", "")
_ZLM_SECRET = os.environ.get("VIDEO_TESTKIT_ZLM_API_SECRET", "")


@pytest.fixture(autouse=True)
def _reset(zlm_client: httpx.Client) -> Iterator[None]:
    zlm_client.post("/testkit/v1/reset")
    try:
        yield
    finally:
        zlm_client.post("/testkit/v1/reset")


def _zlm_get(action: str, **params: object) -> dict:
    resp = httpx.get(
        f"{_ZLM_API}/index/api/{action}",
        params={"secret": _ZLM_SECRET, **params},
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()


def _start_live(client: httpx.Client) -> dict:
    resp = client.post(
        "/provider/v1/live-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "streamProfile": "MAIN",
        },
    )
    return data_of(resp, expected_status=201)


def _stream(client: httpx.Client, key: str) -> dict:
    return data_of(client.get(f"/provider/v1/live-streams/{key}"))


def _wait_stream_state(client: httpx.Client, key: str, state: str, timeout: float = 8.0) -> dict:
    return wait_until_value(
        lambda: _stream(client, key)["state"], lambda s: s == state, timeout=timeout
    )


def _zlm_online(stream_id: str) -> bool:
    data = _zlm_get(
        "isMediaOnline",
        vhost="__defaultVhost__",
        app="rtp",
        stream=stream_id,
        schema="rtsp",
    )
    return bool(data.get("online"))


def _zlm_streams() -> list[str]:
    data = _zlm_get("getMediaList")
    return [
        f"{item.get('app')}/{item.get('stream')}"
        for item in data.get("data", [])
        if item.get("app") == "rtp"
    ]


# ---------------------------------------------------------------- 正常到达


def test_media_arrives_zlm_online_and_provider_streaming(
    zlm_client: httpx.Client,
) -> None:
    """start 返回 STARTING；媒体真实到达后 ZLM online，Provider 才报告 STREAMING。"""
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    assert stream["state"] == "STARTING"
    assert stream["media"]["mediaServerId"] == "testkit-zlm-01"

    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)  # 真实 RTP 到达 ZLM


def test_zlm_reports_expected_stream_id(zlm_client: httpx.Client) -> None:
    """ZLM 侧流身份与 Provider media.streamId 一致（app=rtp）。"""
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert f"rtp/{stream_id}" in _zlm_streams()


# ---------------------------------------------------------------- 失败路径


def test_no_media_fails_and_cleans(zlm_client: httpx.Client) -> None:
    """设备不推流：Provider 有界等待后 FAILED，ZLM 无遗留流。"""
    zlm_client.post(
        f"/testkit/v1/devices/{NVR}/live",
        json={"mediaMode": "none"},
    )
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "FAILED")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert not _zlm_online(stream_id)


def test_wrong_ssrc_not_recognized(zlm_client: httpx.Client) -> None:
    """设备用错误 SSRC 推流：ZLM 不识别为协商流，Provider FAILED。"""
    zlm_client.post(
        f"/testkit/v1/devices/{NVR}/live",
        json={"mediaMode": "wrong-ssrc"},
    )
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "FAILED")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert not _zlm_online(stream_id)


def test_packet_loss_observable(zlm_client: httpx.Client) -> None:
    """确定性丢包：媒体仍到达 ZLM online，设备侧丢包统计可观察。"""
    zlm_client.post(
        f"/testkit/v1/devices/{NVR}/live",
        json={"mediaMode": "lossy", "mediaLossRate": 0.5},
    )
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")

    def dialog_stats() -> dict:
        dialogs = data_of(zlm_client.get(f"/testkit/v1/devices/{NVR}/live"))["dialogs"]
        established = [d for d in dialogs if d["status"] == "ESTABLISHED"]
        return established[0] if established else {}

    stats = wait_until_value(dialog_stats, lambda d: d.get("mediaSent", 0) > 10, timeout=6.0)
    assert stats["mediaDropped"] > 0  # 确定性丢包确实发生


# ---------------------------------------------------------------- 停止与清理


def test_stop_tears_down_zlm_stream(zlm_client: httpx.Client) -> None:
    """DELETE 后 BYE + closeRtpServer：ZLM 无该流，设备停止推流。"""
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)

    assert zlm_client.delete(f"/provider/v1/live-streams/{key}").status_code == 204
    wait_until_value(lambda: _zlm_online(stream_id), lambda online: online is False, timeout=6.0)


def test_double_reset_leaves_no_orphan(zlm_client: httpx.Client) -> None:
    """连续两次 reset：ZLM 无遗留流，RTP 端口可复用（不残留 orphan）。"""
    stream = _start_live(zlm_client)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)

    assert zlm_client.post("/testkit/v1/reset").status_code == 200
    assert zlm_client.post("/testkit/v1/reset").status_code == 200
    assert _zlm_streams() == []
    assert not _zlm_online(stream_id)

    # 端口可复用：新流能再次 online
    stream2 = _start_live(zlm_client)
    _wait_stream_state(zlm_client, stream2["providerStreamKey"], "STREAMING")
    stream_id2 = _stream(zlm_client, stream2["providerStreamKey"])["media"]["streamId"]
    assert _zlm_online(stream_id2)
    zlm_client.post("/testkit/v1/reset")
    assert _zlm_streams() == []
