"""媒体打包（VT-06）：H.264 fixture 元数据与 PS/RTP 确定性单元测试。

不依赖 ZLM；真实媒体到达由 test_live_media_zlm.py 在显式配置 ZLM 时冒烟。
"""

from __future__ import annotations

import hashlib

from video_testkit.media.fixture import FIXTURE_METADATA, H264_FIXTURE_PATH, fixture_sha256
from video_testkit.media.rtp_ps import PsRtpPacketizer, encode_pts

SSRC = 0x01020304


def test_fixture_metadata_and_checksum() -> None:
    """fixture 存在、checksum 匹配，且记录来源/许可/codec/分辨率/duration。"""
    assert H264_FIXTURE_PATH.exists()
    assert fixture_sha256() == FIXTURE_METADATA["sha256"]
    for key in ("source", "license", "codec", "resolution", "durationSeconds", "frames"):
        assert FIXTURE_METADATA[key], f"fixture 元数据缺少 {key}"
    assert FIXTURE_METADATA["durationSeconds"] == 1.0
    assert FIXTURE_METADATA["frames"] == 25


def test_fixture_checksum_constant_matches() -> None:
    raw = H264_FIXTURE_PATH.read_bytes()
    expected = "f6d951611bf49c4522e0e04deec88fadfb7fac9d9fbf425f3efa9231c87be67d"
    assert hashlib.sha256(raw).hexdigest() == expected


def test_ps_mux_contains_pack_and_pes_markers() -> None:
    pkt = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    frames = list(pkt.frames())
    assert len(frames) == 25  # 25 fps × 1s
    ps = b"".join(packet.payload for packet in frames[0])
    # MPEG-2 pack header 固定 14 字节，随后是 System Header 和 E0 视频 PES。
    assert ps[:4] == b"\x00\x00\x01\xba"
    assert ps[14:18] == b"\x00\x00\x01\xbb"
    assert ps[29:33] == b"\x00\x00\x01\xe0"
    # PES 内必须保留 Annex-B NAL 边界，ZLM 才能识别 SPS/H.264 track。
    assert b"\x00\x00\x00\x01\x67" in ps


def test_rtp_sequence_timestamp_marker_ssrc() -> None:
    pkt = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    packets = [p for frame in pkt.frames() for p in frame]
    assert packets

    seqs = [p.sequence for p in packets]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # sequence 连续

    # 每帧 25fps @ 90kHz → 每帧 3600 tick
    frame_stamps = {p.timestamp for p in packets}
    assert all(t % 3600 == 0 for t in frame_stamps)
    assert max(frame_stamps) - min(frame_stamps) == 24 * 3600

    assert all(p.ssrc == SSRC for p in packets)
    # marker 只在每帧最后一个包
    for frame in pkt.frames():
        assert frame[-1].marker is True
        assert all(not p.marker for p in frame[:-1])


def test_rtp_packetization_is_reproducible() -> None:
    a = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    b = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    encoded_a = [p.encode() for frame in a.frames() for p in frame]
    encoded_b = [p.encode() for frame in b.frames() for p in frame]
    assert encoded_a == encoded_b  # 完全可重复


def test_large_frame_fragmented_by_mtu() -> None:
    pkt = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC, mtu=600)
    packets = [p for frame in pkt.frames() for p in frame]
    assert all(len(p.payload) <= 600 for p in packets)
    assert any(len(p.payload) == 600 for p in packets)  # 确实发生分片


def test_encode_pts_roundtrip() -> None:
    pts = 3600
    encoded = encode_pts(pts)
    assert len(encoded) == 5
    # 第 0 字节含 marker 位
    assert encoded[0] & 0x20
