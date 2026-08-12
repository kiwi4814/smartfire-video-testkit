"""GB28181 实时流 SDP 编解码（INVITE offer / 200 answer 所需最小字段）。

SDP 仅用于 SIP 信令层协商（IP、媒体端口、SSRC、编码）；这些值属于
Provider 内部运行态，只经 ``/testkit/v1`` 脱敏诊断暴露，绝不进入
Provider Interface（见 CONTRACT-03 Media Stream Reference 约束）。
"""

from __future__ import annotations

from dataclasses import dataclass

PROVIDER_SDP_IP = "127.0.0.1"


@dataclass(frozen=True)
class SdpData:
    connect_address: str
    media_ports: tuple[int, ...]
    ssrc: str | None
    codecs: tuple[str, ...]
    sendonly: bool = False
    recvonly: bool = False
    session_name: str = "Play"


def _rtpmap_for(codec: str) -> str:
    """GB28181 可选能力（VT-09）：H.265 以 H265/90000 协商，H.264 保持基线。"""
    return "H265/90000" if codec.upper() == "H265" else "H264/90000"


def build_sdp_offer(
    media_ip: str, media_port: int, ssrc: str, codec: str, session_name: str = "Play"
) -> str:
    """构造 Provider（UAC）→ 设备 的 SDP offer（recvonly，等待设备推流）。"""
    return "\r\n".join(
        [
            "v=0",
            f"o=34020000002000000001 0 0 IN IP4 {media_ip}",
            f"s={session_name}",
            f"c=IN IP4 {media_ip}",
            "t=0 0",
            f"m=video {media_port} RTP/AVP 96 98 97",
            "a=recvonly",
            "a=rtpmap:96 PS/90000",
            f"a=rtpmap:98 {_rtpmap_for(codec)}",
            "a=rtpmap:97 MPEG4/90000",
            f"y={ssrc}",
            "",
        ]
    )


def build_sdp_answer(
    media_ip: str, media_port: int, ssrc: str, codec: str, session_name: str = "Play"
) -> str:
    """构造设备（UAS）→ Provider 的 SDP answer（sendonly，设备侧推流端点）。"""
    return "\r\n".join(
        [
            "v=0",
            f"o=34020000001320000001 0 0 IN IP4 {media_ip}",
            f"s={session_name}",
            f"c=IN IP4 {media_ip}",
            "t=0 0",
            f"m=video {media_port} RTP/AVP 96 98 97",
            "a=sendonly",
            "a=rtpmap:96 PS/90000",
            f"a=rtpmap:98 {_rtpmap_for(codec)}",
            "a=rtpmap:97 MPEG4/90000",
            f"y={ssrc}",
            "",
        ]
    )


def parse_sdp(body: str) -> SdpData:
    """解析 SDP；缺关键字段（c=/m=）时抛 ``ValueError``。"""
    connect_address: str | None = None
    media_ports: list[int] = []
    codecs: list[str] = []
    ssrc: str | None = None
    sendonly = False
    recvonly = False
    session_name = "Play"
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        field, _, value = line.partition("=")
        field = field.strip()
        value = value.strip()
        if field == "s":
            session_name = value
        elif field == "c" and value.lower().startswith("in ip4"):
            connect_address = value.split()[-1]
        elif field == "m":
            parts = value.split()
            if len(parts) >= 2 and parts[1].isdigit():
                media_ports.append(int(parts[1]))
        elif field == "a":
            if value == "sendonly":
                sendonly = True
            elif value == "recvonly":
                recvonly = True
            elif value.startswith("rtpmap:"):
                _name = value.partition(" ")[2]
                codecs.append(_name.split("/", 1)[0])
        elif field == "y":
            ssrc = value.split()[0] if value else None

    if connect_address is None or not media_ports:
        raise ValueError(f"SDP 缺少 c= 或 m= 行: {body!r}")
    return SdpData(
        connect_address=connect_address,
        media_ports=tuple(media_ports),
        ssrc=ssrc,
        codecs=tuple(dict.fromkeys(codecs)),
        sendonly=sendonly,
        recvonly=recvonly,
        session_name=session_name,
    )
