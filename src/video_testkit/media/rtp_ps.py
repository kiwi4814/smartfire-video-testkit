"""GB28181 实时媒体：H.264 → MPEG-PS → RTP 打包（确定性、可重复）。

打包输出只依赖输入字节与 SSRC/MTU：相同输入产生逐字节相同的 RTP 序列，
sequence 连续、timestamp 按 90kHz/帧率递增、marker 落在每帧最后一个包。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

RTP_PT_PS = 96
CLOCK_RATE = 90_000

# 标准 MPEG-2 Program Stream 头（与 ffmpeg mpeg2program/vob 输出对齐，
# system header 声明 video stream 0xE0，ZLM 解析依赖该 codecid 登记）。
_PS_PACK_HEADER = bytes.fromhex("000001ba4400040004018666cff8")
_PS_SYSTEM_HEADER = bytes.fromhex("000001bb0009c333670021ffe0e0e6")
_PES_VIDEO_START = b"\x00\x00\x01\xe0"
_NAL_START = b"\x00\x00\x01"

NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8
NAL_TYPE_SEI = 6
NAL_TYPE_IDR = 5


def encode_pts(pts: int) -> bytes:
    """编码 33 位 PTS 为 5 字节（MPEG-2 PES 格式）。"""
    return bytes(
        [
            0x20 | ((pts >> 30) & 0x07),
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 0x01,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 0x01,
        ]
    )


def _parse_annex_b(data: bytes) -> list[bytes]:
    """按 Annex-B 起始码切分 NAL；跳过 SEI，保留 SPS/PPS 供关键帧携带。"""
    nals: list[tuple[int, bytes]] = []
    i = 0
    size = len(data)
    while i < size - 3:
        if data[i : i + 3] == _NAL_START:
            start = i + 3
            j = start
            while j < size - 3:
                if data[j : j + 3] == _NAL_START or data[j : j + 4] == b"\x00\x00\x00\x01":
                    break
                j += 1
            if start < size:
                nal = data[start:j]
                nals.append((nal[0] & 0x1F, nal))
            i = max(j, i + 1)
        else:
            i += 1
    return [nal for ntype, nal in nals if ntype != NAL_TYPE_SEI]


@dataclass(frozen=True)
class RtpPacket:
    sequence: int
    timestamp: int
    marker: bool
    ssrc: int
    payload: bytes

    def encode(self) -> bytes:
        """编码为 12 字节 RTP 头 + payload（V=2，无扩展，PT=96）。"""
        header = bytearray(12)
        header[0] = 0x80
        header[1] = RTP_PT_PS | (0x80 if self.marker else 0)
        header[2:4] = self.sequence.to_bytes(2, "big")
        header[4:8] = self.timestamp.to_bytes(4, "big")
        header[8:12] = self.ssrc.to_bytes(4, "big")
        return bytes(header) + self.payload


class PsRtpPacketizer:
    """将 H.264 裸流打包为逐帧 PS-over-RTP 包序列。"""

    def __init__(
        self,
        h264: bytes,
        ssrc: int,
        mtu: int = 1200,
        fps: int = 25,
        start_sequence: int = 0,
    ) -> None:
        self._ssrc = ssrc
        self._mtu = mtu
        self._fps = fps
        self._sequence = start_sequence
        self._h264 = h264
        self._frames = self._group_frames()

    # ------------------------------------------------------------ 帧分组

    def _group_frames(self) -> list[list[bytes]]:
        nals = _parse_annex_b(self._h264)
        sps = next((n for n in nals if n[0] & 0x1F == NAL_TYPE_SPS), None)
        pps = next((n for n in nals if n[0] & 0x1F == NAL_TYPE_PPS), None)
        sps = sps if sps is not None else b""
        pps = pps if pps is not None else b""

        frames: list[list[bytes]] = []
        for nal in nals:
            ntype = nal[0] & 0x1F
            if ntype in (NAL_TYPE_SPS, NAL_TYPE_PPS):
                continue
            if ntype == NAL_TYPE_IDR:
                # H.264 elementary stream 保留 Annex-B NAL 边界，供 ZLM 识别 codec。
                frames.append([sps, pps, nal])
            else:
                frames.append([nal])
        return frames

    # ------------------------------------------------------------ 打包

    def frames(self) -> Iterator[list[RtpPacket]]:
        """单轮逐帧产出 RTP 包列表；重复调用输出完全一致（确定性）。"""
        self._sequence = 0
        pts = 0
        tick = CLOCK_RATE // self._fps
        for frame_nals in self._frames:
            ps = self._mux_frame(frame_nals, pts)
            yield self._packetize(ps, pts)
            pts += tick

    def frame_iterator(self) -> Iterator[list[RtpPacket]]:
        """连续推流迭代器：循环播放 fixture，sequence/PTS 持续递增不回绕。"""
        pts = 0
        tick = CLOCK_RATE // self._fps
        while True:
            for frame_nals in self._frames:
                ps = self._mux_frame(frame_nals, pts)
                yield self._packetize(ps, pts)
                pts += tick

    def _mux_frame(self, frame_nals: list[bytes], pts: int) -> bytes:
        payload = b"".join(b"\x00\x00\x00\x01" + nal for nal in frame_nals)
        # 与 ffmpeg mpeg2program 输出对齐的 PES 头：PTS + 4 字节 extension。
        pes_header = (
            _PES_VIDEO_START
            + (len(payload) + 12).to_bytes(2, "big")
            + b"\x80\x81\x09"
            + encode_pts(pts)
            + b"\x10\x60\xe6\xff"
        )
        return _PS_PACK_HEADER + _PS_SYSTEM_HEADER + pes_header + payload

    def _packetize(self, ps: bytes, timestamp: int) -> list[RtpPacket]:
        packets: list[RtpPacket] = []
        if len(ps) <= self._mtu:
            packets.append(
                RtpPacket(
                    sequence=self._next_seq(),
                    timestamp=timestamp,
                    marker=True,
                    ssrc=self._ssrc,
                    payload=ps,
                )
            )
            return packets
        for offset in range(0, len(ps), self._mtu):
            chunk = ps[offset : offset + self._mtu]
            last = offset + self._mtu >= len(ps)
            packets.append(
                RtpPacket(
                    sequence=self._next_seq(),
                    timestamp=timestamp,
                    marker=last,
                    ssrc=self._ssrc,
                    payload=chunk,
                )
            )
        return packets

    def _next_seq(self) -> int:
        seq = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return seq
