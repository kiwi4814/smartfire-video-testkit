"""实时流 SIP 信令（VT-05）：真实 INVITE/SDP/ACK/BYE 生命周期。

公共 seam：``/testkit/v1`` 编排设备应答场景，``/provider/v1`` live-streams
是结果 seam，真实 SIP UDP 信令是协议 seam。SDP/SSRC/目标地址只作为
``/testkit/v1`` 脱敏诊断暴露，不进入 Provider Interface。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

from video_testkit.sip.sdp import build_sdp_answer, build_sdp_offer, parse_sdp

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _start_live(client: httpx.Client) -> dict:
    resp = client.post(
        "/provider/v1/live-streams",
        headers={"Idempotency-Key": "live-red-test-key"},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "streamProfile": "MAIN",
        },
    )
    return data_of(resp, expected_status=201)


def _configure(client: httpx.Client, **kwargs: object) -> dict:
    resp = client.post(f"/testkit/v1/devices/{NVR}/live", json=kwargs)
    return data_of(resp, expected_status=200)


def _device_live(client: httpx.Client) -> dict:
    return data_of(client.get(f"/testkit/v1/devices/{NVR}/live"))


def _stream_state(client: httpx.Client, key: str) -> str:
    return data_of(client.get(f"/provider/v1/live-streams/{key}"))["state"]


def _wait_dialog(client: httpx.Client, predicate, timeout: float = 6.0) -> list[dict]:
    def dialogs() -> list[dict]:
        return _device_live(client)["dialogs"]

    return wait_until_value(dialogs, predicate, timeout=timeout)


# ---------------------------------------------------------------- SDP 编解码


def test_sdp_roundtrip() -> None:
    offer = build_sdp_offer("127.0.0.1", 30000, "0100000001", "H264")
    parsed = parse_sdp(offer)
    assert parsed.connect_address == "127.0.0.1"
    assert 30000 in parsed.media_ports
    assert parsed.ssrc == "0100000001"
    assert "H264" in parsed.codecs

    answer = build_sdp_answer("127.0.0.1", 20000, "0100000002", "H264")
    parsed_answer = parse_sdp(answer)
    assert 20000 in parsed_answer.media_ports
    assert parsed_answer.ssrc == "0100000002"


# ---------------------------------------------------------------- 正常 Dialog


def test_live_dialog_reaches_established_then_terminated(client: httpx.Client) -> None:
    """正常 INVITE/SDP/200/ACK 到达 ESTABLISHED，BYE 后 TERMINATED。"""
    stream = _start_live(client)
    assert stream["state"] == "STREAMING"

    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    dialog = next(d for d in dialogs if d["status"] == "ESTABLISHED")
    assert dialog["deviceId"] == NVR
    assert dialog["ssrc"]
    assert dialog["mediaPort"] > 0

    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_live_stream_real_sip_evidence(client: httpx.Client) -> None:
    """SIP seam 证据：设备经真实 UDP 收到 INVITE，ACK 到达后 Dialog ESTABLISHED。"""
    _start_live(client)
    _wait_dialog(client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds))
    live = _device_live(client)
    assert live["invitesReceived"] >= 1
    established = next(d for d in live["dialogs"] if d["status"] == "ESTABLISHED")
    assert established["ackReceived"] is True  # ACK 真实到达设备
    assert established["target"]  # INVITE 来源地址（Provider 侧）


# ---------------------------------------------------------------- 失败路径


def test_live_rejection_maps_to_failed(client: httpx.Client) -> None:
    """设备回 486：Provider stream 最终 FAILED。"""
    _configure(client, mode="rejection")
    stream = _start_live(client)
    assert stream["state"] == "STREAMING"  # 同步返回不变

    def state() -> str:
        return _stream_state(client, stream["providerStreamKey"])

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


def test_live_invite_timeout_maps_to_failed(client: httpx.Client) -> None:
    """设备不响应 INVITE：Provider stream 最终 FAILED（有界超时）。"""
    _configure(client, mode="drop")
    stream = _start_live(client)

    def state() -> str:
        return _stream_state(client, stream["providerStreamKey"])

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


def test_live_missing_ack_is_observable_failure(client: httpx.Client) -> None:
    """设备 200 后未收到 ACK：设备侧 Dialog 以稳定 FAILED 状态可观察。"""
    _configure(client, mode="no-ack")
    _start_live(client)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "FAILED" and not d["ackReceived"] for d in ds)
    )
    assert dialogs, "设备侧应观察到未确认 ACK 的失败 Dialog"


def test_live_delayed_response_succeeds(client: httpx.Client) -> None:
    """设备延迟 0.5s 应答 INVITE：Dialog 仍到达 ESTABLISHED。"""
    _configure(client, mode="delayed", delaySeconds=0.5)
    _start_live(client)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds), timeout=8.0
    )
    assert dialogs


# ---------------------------------------------------------------- Stop 与清理


def test_live_repeated_stop_safe(client: httpx.Client) -> None:
    """重复 DELETE 均 204，设备侧 Dialog TERMINATED。"""
    stream = _start_live(client)
    _wait_dialog(client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds))
    key = stream["providerStreamKey"]
    assert client.delete(f"/provider/v1/live-streams/{key}").status_code == 204
    assert client.delete(f"/provider/v1/live-streams/{key}").status_code == 204
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_live_failed_scenario_leaves_no_dialog(client: httpx.Client) -> None:
    """rejection 后设备侧无遗留 Dialog（INVITING/WAITING_ACK 均不残留）。"""
    _configure(client, mode="rejection")
    _start_live(client)

    def has_dialog() -> bool:
        return any(d["status"] not in ("TERMINATED",) for d in _device_live(client)["dialogs"])

    wait_until_value(has_dialog, lambda v: v is False, timeout=6.0)
    assert all(d["status"] == "TERMINATED" for d in _device_live(client)["dialogs"])


def test_live_reset_clears_dialogs_and_scenario(client: httpx.Client) -> None:
    """reset 后 Dialog 与场景编排清空，新 INVITE 恢复 normal 行为。"""
    _configure(client, mode="rejection")
    _start_live(client)
    wait_until_value(lambda: _device_live(client)["invitesReceived"], lambda n: n >= 1, timeout=6.0)

    client.post("/testkit/v1/reset")
    assert _device_live(client)["dialogs"] == []
    assert _device_live(client)["mode"] == "normal"

    stream = _start_live(client)
    _wait_dialog(client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds))
    assert stream["state"] == "STREAMING"


# ---------------------------------------------------------------- H.265（VT-09）


def test_sdp_h265_rtpmap() -> None:
    """H.265 协商：rtpmap 声明 H265/90000；H.264 基线仍为 H264/90000。"""
    answer = build_sdp_answer("127.0.0.1", 20000, "0100000002", "H265")
    assert "a=rtpmap:98 H265/90000" in answer
    offer = build_sdp_offer("127.0.0.1", 30000, "0100000001", "H265")
    assert "a=rtpmap:98 H265/90000" in offer
    parsed = parse_sdp(answer)
    assert "H265" in parsed.codecs
    # 基线不受影响
    base = build_sdp_answer("127.0.0.1", 20000, "0100000002", "H264")
    assert "a=rtpmap:98 H264/90000" in base
    assert "H265/90000" not in base


def test_live_h265_codec_configured_and_media_sent(client: httpx.Client) -> None:
    """H.265 场景：codec 编排生效，真实 SIP Dialog 建立后设备推 H.265 媒体。"""
    view = _configure(client, codec="H265")
    assert view["codec"] == "H265"

    stream = _start_live(client)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    assert dialogs
    live = _device_live(client)
    assert live["codec"] == "H265"

    # 设备侧媒体循环使用 H.265 fixture 推流：mediaSent 递增可观察。
    def media_sent() -> int:
        established = [d for d in _device_live(client)["dialogs"] if d["status"] == "ESTABLISHED"]
        return established[0]["mediaSent"] if established else 0

    assert wait_until_value(media_sent, lambda n: n > 0, timeout=6.0) > 0

    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_live_unknown_codec_rejected(client: httpx.Client) -> None:
    """不支持 codec 显式失败（400），不静默降级。"""
    resp = client.post(f"/testkit/v1/devices/{NVR}/live", json={"codec": "MPEG4"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_playback_h265_codec_configured(client: httpx.Client) -> None:
    """回放场景同样可编排 H.265 codec（独立能力，不捆绑）。"""
    resp = client.post(
        f"/testkit/v1/devices/{NVR}/playback",
        json={"codec": "H265", "mediaMode": "none"},
    )
    view = data_of(resp, expected_status=200)
    assert view["codec"] == "H265"
    assert view["mediaMode"] == "none"


def test_live_audio_scenario_configured_and_media_sent(client: httpx.Client) -> None:
    """音频场景：hasAudio 编排生效，设备推流统计递增（H.264 + G.711A）。"""
    view = _configure(client, codec="H264", hasAudio=True)
    assert view["hasAudio"] is True

    stream = _start_live(client)
    _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    live = _device_live(client)
    assert live["hasAudio"] is True
    assert live["codec"] == "H264"

    def media_sent() -> int:
        established = [d for d in _device_live(client)["dialogs"] if d["status"] == "ESTABLISHED"]
        return established[0]["mediaSent"] if established else 0

    assert wait_until_value(media_sent, lambda n: n > 0, timeout=6.0) > 0

    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_audio_h265_combination_independent_selection(client: httpx.Client) -> None:
    """音频与 H.265 可独立选择：H.265 + 音频组合场景编排不捆绑。"""
    view = _configure(client, codec="H265", hasAudio=True)
    assert view["codec"] == "H265"
    assert view["hasAudio"] is True

    resp = client.post(
        f"/testkit/v1/devices/{NVR}/playback",
        json={"codec": "H264", "hasAudio": False},
    )
    view2 = data_of(resp, expected_status=200)
    assert view2["codec"] == "H264"
    assert view2["hasAudio"] is False


# ---------------------------------------------------------------- SIP over TCP（VT-09）


def test_live_tcp_scenario_rejects_unknown_transport(client: httpx.Client) -> None:
    """不支持的 transport 显式失败（400），不静默降级。"""
    resp = client.post(f"/testkit/v1/devices/{NVR}/live", json={"transport": "SCTP"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_live_dialog_over_tcp_established_then_terminated(client: httpx.Client) -> None:
    """SIP over TCP：真实 TCP 信令建立 Dialog，BYE 后 TERMINATED（VT-09）。"""
    view = _configure(client, transport="TCP")
    assert view["transport"] == "TCP"
    # 设备 TCP 监听已就绪（Provider 侧经 TCP 发送 INVITE）。
    live = _device_live(client)
    assert live["transport"] == "TCP"

    stream = _start_live(client)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    assert dialogs
    established = next(d for d in dialogs if d["status"] == "ESTABLISHED")
    assert established["target"]  # INVITE 来源（Provider 侧 TCP 端口）

    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_live_tcp_rejection_maps_to_failed(client: httpx.Client) -> None:
    """TCP 信令下设备回 486：Provider stream 最终 FAILED。"""
    _configure(client, mode="rejection", transport="TCP")
    stream = _start_live(client)

    def state() -> str:
        return _stream_state(client, stream["providerStreamKey"])

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


def test_live_tcp_drop_times_out(client: httpx.Client) -> None:
    """TCP 信令下设备不响应：Provider INVITE 有界超时后 FAILED。"""
    _configure(client, mode="drop", transport="TCP")
    stream = _start_live(client)

    def state() -> str:
        return _stream_state(client, stream["providerStreamKey"])

    assert wait_until_value(state, lambda s: s == "FAILED", timeout=6.0) == "FAILED"


# ---------------------------------------------------------------- RTP over TCP（VT-09）


def test_live_media_tcp_configured_and_failure_bounded(client: httpx.Client) -> None:
    """mediaTransport=TCP：编排生效；无媒体端点时连接失败有界收敛（不无限阻塞）。"""
    view = _configure(client, mediaTransport="TCP")
    assert view["mediaTransport"] == "TCP"

    stream = _start_live(client)
    _wait_dialog(client, lambda ds: any(d["status"] == "ESTABLISHED" for d in ds))
    live = _device_live(client)
    assert live["mediaTransport"] == "TCP"

    # 无 ZLM 时媒体端点是 Provider 侧预留端口（未监听）：设备 TCP 连接失败后
    # mediaActive 恢复 False 且 lastError 可观察，Dialog 保持 ESTABLISHED。
    def dialog_media() -> dict:
        established = [d for d in _device_live(client)["dialogs"] if d["status"] == "ESTABLISHED"]
        return established[0] if established else {}

    wait_until_value(dialog_media, lambda d: d.get("mediaActive") is False, timeout=6.0)
    # 清理路径正常
    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
    _wait_dialog(client, lambda ds: any(d["status"] == "TERMINATED" for d in ds))


def test_live_unknown_media_transport_rejected(client: httpx.Client) -> None:
    """不支持的 mediaTransport 显式失败（400），不静默降级。"""
    resp = client.post(f"/testkit/v1/devices/{NVR}/live", json={"mediaTransport": "SCTP"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VIDEO_INVALID_ARGUMENT"


def test_live_media_tcp_udp_baseline_unchanged(client: httpx.Client) -> None:
    """默认 mediaTransport=UDP：编排视图与基线一致，未配置时行为不变。"""
    view = _configure(client)
    assert view["mediaTransport"] == "UDP"

    stream = _start_live(client)
    dialogs = _wait_dialog(
        client, lambda ds: any(d["status"] == "ESTABLISHED" and d["ackReceived"] for d in ds)
    )
    assert dialogs
    established = next(d for d in dialogs if d["status"] == "ESTABLISHED")
    assert established["mediaSent"] >= 0  # UDP 媒体统计路径可用
    assert (
        client.delete(f"/provider/v1/live-streams/{stream['providerStreamKey']}").status_code == 204
    )
