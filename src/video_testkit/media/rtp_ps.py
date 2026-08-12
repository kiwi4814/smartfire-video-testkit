"""GB28181 实时媒体：H.264/H.265 → MPEG-PS → RTP 打包（确定性、可重复）。

打包输出只依赖输入字节与 SSRC/MTU：相同输入产生逐字节相同的 RTP 序列，
sequence 连续、timestamp 按 90kHz/帧率递增、marker 落在每帧最后一个包。

H.265 是 VT-09 可选能力：通过 ``codec="H265"`` 独立选择，PSM 以
stream_type=0x24 声明 HEVC（H.264 基线保持 stream_type=0x1B 与既有
``_PS_MAP`` 逐字节不变，实现向后兼容）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

RTP_PT_PS = 96
CLOCK_RATE = 90_000

# 标准 MPEG-2 Program Stream 头（与 ffmpeg mpeg2program/vob 输出对齐，
# system header 声明 video stream 0xE0，ZLM 解析依赖该 codecid 登记）。
_PS_PACK_HEADER = bytes.fromhex("000001ba4400040004018666cff8")
_PS_SYSTEM_HEADER = bytes.fromhex("000001bb0009c333670021ffe0e0e6")
# Program Stream Map：stream_type 0x1B 将 PES 0xE0 明确声明为 H.264。
# CRC-32/MPEG-2 覆盖 start code 至 elementary stream map，完整 PSM 余数为 0。
_PS_MAP_H264 = bytes.fromhex("000001bc000ee1ff000000041be00000744c1d22")
# H.265（HEVC）：stream_type 0x24，CRC-32/MPEG-2 由 _crc32_mpeg2 动态计算。
_PS_MAP_H265_PREFIX = bytes.fromhex("000001bc000ee1ff0000000424e00000")
# 含音频的 PSM 前缀（video + G.711A audio stream_type 0x90，PES 0xC0）：
# 两个 ES 条目使 elementary_stream_map_length=0x0008、program_stream_map_length=0x0012。
_PS_MAP_AUDIO_H264_PREFIX = bytes.fromhex("000001bc0012e1ff000000081be0000090c00000")
_PS_MAP_AUDIO_H265_PREFIX = bytes.fromhex("000001bc0012e1ff0000000824e0000090c00000")
_PES_VIDEO_START = b"\x00\x00\x01\xe0"
_PES_AUDIO_START = b"\x00\x00\x01\xc0"
# 音频 PES 头：'10'+PTS_DTS_flags=00 + PES_header_data_length=5（PTS 5 字节）。
_AUDIO_PES_HEADER = b"\x80\x80\x05"
_NAL_START = b"\x00\x00\x01"

# H.264 NAL 类型
NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8
NAL_TYPE_SEI = 6
NAL_TYPE_IDR = 5
# H.265 NAL 类型（2 字节 NAL header，类型取自首字节 bit6-1）
NAL_TYPE_VPS = 32
NAL_TYPE_H265_SPS = 33
NAL_TYPE_H265_PPS = 34
NAL_TYPE_H265_IDR = 19  # IDR_W_RADL
NAL_TYPE_H265_IDR_N = 20  # IDR_N_LP

Codec = Literal["H264", "H265"]


def _h264_nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F


def _h265_nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


def _crc32_mpeg2(data: bytes) -> bytes:
    """CRC-32/MPEG-2（poly 0x04C11DB7，init 0xFFFFFFFF，无反射/无异或）。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1)
    return crc.to_bytes(4, "big")


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


def _parse_annex_b(data: bytes, nal_type_fn: Callable[[bytes], int]) -> list[bytes]:
    """按 Annex-B 起始码切分 NAL；跳过 SEI，保留参数集供关键帧携带。"""
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
                nals.append((nal_type_fn(nal), nal))
            i = max(j, i + 1)
        else:
            i += 1
    sei_types = (NAL_TYPE_SEI,) if nal_type_fn is _h264_nal_type else (39, 40)  # H.265 SEI
    return [nal for ntype, nal in nals if ntype not in sei_types]


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
    """将 H.264/H.265 裸流打包为逐帧 PS-over-RTP 包序列（确定性）。

    ``audio`` 提供 G.711A 原始字节时，每帧 PS 内追加音频 PES（0xC0），
    PSM 以 stream_type=0x90 声明音频轨（VT-09 可选能力，不改变无音频基线）。
    """

    def __init__(
        self,
        es: bytes,
        ssrc: int,
        mtu: int = 1200,
        fps: int = 25,
        start_sequence: int = 0,
        codec: Codec = "H264",
        audio: bytes | None = None,
        audio_bytes_per_frame: int = 320,  # 8000Hz × 40ms（25fps 每帧）
    ) -> None:
        self._ssrc = ssrc
        self._mtu = mtu
        self._fps = fps
        self._sequence = start_sequence
        self._codec = codec
        self._es = es
        self._audio = audio
        self._audio_per_frame = audio_bytes_per_frame
        self._frames = self._group_frames()

    # ------------------------------------------------------------ 帧分组

    def _group_frames(self) -> list[list[bytes]]:
        param_set_types: tuple[int, ...]
        idr_types: tuple[int, ...]
        if self._codec == "H265":
            nal_type_fn = _h265_nal_type
            param_set_types = (NAL_TYPE_VPS, NAL_TYPE_H265_SPS, NAL_TYPE_H265_PPS)
            idr_types = (NAL_TYPE_H265_IDR, NAL_TYPE_H265_IDR_N)
        else:
            nal_type_fn = _h264_nal_type
            param_set_types = (NAL_TYPE_SPS, NAL_TYPE_PPS)
            idr_types = (NAL_TYPE_IDR,)
        nals = _parse_annex_b(self._es, nal_type_fn)
        param_sets = [nal for nal in nals if nal_type_fn(nal) in param_set_types]

        frames: list[list[bytes]] = []
        for nal in nals:
            ntype = nal_type_fn(nal)
            if ntype in param_set_types:
                continue
            if ntype in idr_types:
                # 关键帧前保留 Annex-B NAL 边界，供 ZLM 识别 codec。
                frames.append([*param_sets, nal])
            else:
                frames.append([nal])
        return frames

    # ------------------------------------------------------------ 打包

    def frames(self) -> Iterator[list[RtpPacket]]:
        """单轮逐帧产出 RTP 包列表；重复调用输出完全一致（确定性）。"""
        self._sequence = 0
        pts = 0
        tick = CLOCK_RATE // self._fps
        for frame_index, frame_nals in enumerate(self._frames):
            ps = self._mux_frame(frame_nals, pts, frame_index)
            yield self._packetize(ps, pts)
            pts += tick

    def frame_iterator(self) -> Iterator[list[RtpPacket]]:
        """连续推流迭代器：循环播放 fixture，sequence/PTS 持续递增不回绕。"""
        pts = 0
        tick = CLOCK_RATE // self._fps
        frame_index = 0
        while True:
            for frame_nals in self._frames:
                ps = self._mux_frame(frame_nals, pts, frame_index)
                yield self._packetize(ps, pts)
                pts += tick
                frame_index += 1

    def _mux_frame(self, frame_nals: list[bytes], pts: int, frame_index: int = 0) -> bytes:
        payload = b"".join(b"\x00\x00\x00\x01" + nal for nal in frame_nals)
        # 与 ffmpeg mpeg2program 输出对齐的 PES 头：PTS + 4 字节 extension。
        pes_header = (
            _PES_VIDEO_START
            + (len(payload) + 12).to_bytes(2, "big")
            + b"\x80\x81\x09"
            + encode_pts(pts)
            + b"\x10\x60\xe6\xff"
        )
        if self._codec == "H265":
            no_audio_psm = _PS_MAP_H265_PREFIX + _crc32_mpeg2(_PS_MAP_H265_PREFIX)
            ps_map = self._audio_ps_map(_PS_MAP_AUDIO_H265_PREFIX, no_audio_psm)
        else:
            ps_map = self._audio_ps_map(_PS_MAP_AUDIO_H264_PREFIX, _PS_MAP_H264)
        ps = _PS_PACK_HEADER + _PS_SYSTEM_HEADER + ps_map + pes_header + payload
        if self._audio is not None:
            ps += self._mux_audio_pes(pts, frame_index)
        return ps

    def _audio_ps_map(self, audio_prefix: bytes, no_audio_psm: bytes) -> bytes:
        """无音频时返回完整基线 PSM（逐字节不变）；有音频时动态计算含音频 PSM。"""
        if self._audio is None:
            return no_audio_psm
        return audio_prefix + _crc32_mpeg2(audio_prefix)

    def _mux_audio_pes(self, pts: int, frame_index: int) -> bytes:
        """G.711A 音频 PES：按帧循环切片，PTS 与视频帧同步（25fps）。"""
        assert self._audio is not None
        start = (frame_index * self._audio_per_frame) % len(self._audio)
        chunk = self._audio[start : start + self._audio_per_frame]
        if len(chunk) < self._audio_per_frame:  # fixture 非整帧长度时回绕
            chunk = chunk + self._audio[: self._audio_per_frame - len(chunk)]
        pes_header = (
            _PES_AUDIO_START
            + (len(chunk) + 8).to_bytes(2, "big")
            + _AUDIO_PES_HEADER
            + encode_pts(pts)
        )
        return pes_header + chunk

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


def frame_to_tcp(packet: RtpPacket) -> bytes:
    """GB28181 TCP 媒体帧：4 字节头（0x24 0x00 + 2 字节网络序长度）+ RTP 包。

    VT-09 可选能力：RTP over TCP 时每个 RTP 包前加该长度前缀（与 ZLM
    tcp 被动模式及国标设备一致），UDP 基线不受影响。
    """
    encoded = packet.encode()
    return b"\x24\x00" + len(encoded).to_bytes(2, "big") + encoded
