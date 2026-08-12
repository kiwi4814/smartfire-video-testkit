"""ZLM 回放集成冒烟（VT-08）：真实播放媒体到达验证。

仅在显式配置 ``VIDEO_TESTKIT_ZLM_API_URL`` 时运行（未配置则 skip）。
- Provider start_playback → ZLM RTP 端口 → INVITE(s=Playback) → 设备推流 → ZLM online → STREAMING
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
QUERY_START = "2026-08-01T00:00:00.000Z"
QUERY_END = "2026-08-01T02:30:00.000Z"


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


def _start_playback(client: httpx.Client, record_key: str) -> dict:
    resp = client.post(
        "/provider/v1/playback-streams",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "recordKey": record_key,
        },
    )
    return data_of(resp, expected_status=201)


def _stream(client: httpx.Client, key: str) -> dict:
    return data_of(client.get(f"/provider/v1/playback-streams/{key}"))


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


def test_playback_media_arrives_zlm_online_and_provider_streaming(
    zlm_client: httpx.Client,
) -> None:
    """Playback start 返回 STARTING；媒体到达 ZLM online 后，Provider 报告 STREAMING。"""
    record_key = _create_record(zlm_client)
    stream = _start_playback(zlm_client, record_key)
    key = stream["providerStreamKey"]
    assert stream["state"] == "STARTING"
    assert stream["media"]["mediaServerId"] == "testkit-zlm-01"

    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)  # 真实 RTP 到达 ZLM


def test_playback_zlm_reports_expected_stream_id(zlm_client: httpx.Client) -> None:
    """ZLM 侧流身份与 Provider PlaybackStream media.streamId 一致。"""
    record_key = _create_record(zlm_client)
    stream = _start_playback(zlm_client, record_key)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert f"rtp/{stream_id}" in _zlm_streams()


# ---------------------------------------------------------------- 失败与清理


def test_playback_no_media_fails_and_cleans(zlm_client: httpx.Client) -> None:
    """设备回放不推流：Provider 有界等待后 FAILED，ZLM 无遗留流。"""
    record_key = _create_record(zlm_client)
    zlm_client.post(
        f"/testkit/v1/devices/{NVR}/playback",
        json={"mediaMode": "none"},
    )
    stream = _start_playback(zlm_client, record_key)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "FAILED")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert not _zlm_online(stream_id)


def test_playback_stop_tears_down_zlm_stream(zlm_client: httpx.Client) -> None:
    """DELETE 后 BYE + closeRtpServer：ZLM 无该流，设备停止推流。"""
    record_key = _create_record(zlm_client)
    stream = _start_playback(zlm_client, record_key)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)

    assert zlm_client.delete(f"/provider/v1/playback-streams/{key}").status_code == 204
    wait_until_value(lambda: _zlm_online(stream_id), lambda online: online is False, timeout=6.0)


def test_playback_double_reset_leaves_no_orphan(zlm_client: httpx.Client) -> None:
    """连续两次 reset：ZLM 无遗留流，RTP 端口可复用（不残留 orphan）。"""
    record_key = _create_record(zlm_client)
    stream = _start_playback(zlm_client, record_key)
    key = stream["providerStreamKey"]
    _wait_stream_state(zlm_client, key, "STREAMING")
    stream_id = _stream(zlm_client, key)["media"]["streamId"]
    assert _zlm_online(stream_id)

    assert zlm_client.post("/testkit/v1/reset").status_code == 200
    assert zlm_client.post("/testkit/v1/reset").status_code == 200
    assert _zlm_streams() == []
    assert not _zlm_online(stream_id)
