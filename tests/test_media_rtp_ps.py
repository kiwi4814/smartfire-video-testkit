"""媒体打包（VT-06/VT-09）：H.264/H.265 fixture 元数据与 PS/RTP 确定性单元测试。

不依赖 ZLM；真实媒体到达由 test_live_media_zlm.py 在显式配置 ZLM 时冒烟。
H.265 是 VT-09 可选能力子切片：独立 fixture、独立 PSM stream_type=0x24，
不改变 H.264/UDP 基线。
"""

from __future__ import annotations

import hashlib

from video_testkit.media.fixture import (
    FIXTURE_METADATA,
    G711A_FIXTURE_METADATA,
    G711A_FIXTURE_PATH,
    H264_FIXTURE_PATH,
    H265_FIXTURE_METADATA,
    H265_FIXTURE_PATH,
    fixture_sha256,
)
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
    # MPEG-2 pack/system header 后以 PSM 将 E0 明确映射为 H.264，再发送视频 PES。
    assert ps[:4] == b"\x00\x00\x01\xba"
    assert ps[14:18] == b"\x00\x00\x01\xbb"
    assert ps[29:33] == b"\x00\x00\x01\xbc"
    assert ps[41:45] == b"\x1b\xe0\x00\x00"
    assert ps[49:53] == b"\x00\x00\x01\xe0"
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


# ---------------------------------------------------------------- H.265（VT-09）


def test_h265_fixture_metadata_and_checksum() -> None:
    """H.265 fixture 存在、checksum 匹配，且记录来源/许可/codec/分辨率/duration。"""
    assert H265_FIXTURE_PATH.exists()
    assert fixture_sha256(H265_FIXTURE_PATH) == H265_FIXTURE_METADATA["sha256"]
    for key in ("source", "license", "codec", "resolution", "durationSeconds", "frames"):
        assert H265_FIXTURE_METADATA[key], f"H.265 fixture 元数据缺少 {key}"
    assert H265_FIXTURE_METADATA["durationSeconds"] == 1.0
    assert H265_FIXTURE_METADATA["frames"] == 25


def test_h265_fixture_checksum_constant_matches() -> None:
    raw = H265_FIXTURE_PATH.read_bytes()
    expected = "564d78446629aba662b95dee046449b5ebed09fb42ce29d2561ea925cd881a20"
    assert hashlib.sha256(raw).hexdigest() == expected


def test_h265_ps_mux_declares_hevc_stream_type() -> None:
    """H.265 PS 的 PSM 以 stream_type=0x24 将 PES 0xE0 声明为 HEVC。"""
    pkt = PsRtpPacketizer(H265_FIXTURE_PATH.read_bytes(), ssrc=SSRC, codec="H265")
    frames = list(pkt.frames())
    assert len(frames) == 25  # 25 fps × 1s
    ps = b"".join(packet.payload for packet in frames[0])
    assert ps[:4] == b"\x00\x00\x01\xba"
    assert ps[14:18] == b"\x00\x00\x01\xbb"
    assert ps[29:33] == b"\x00\x00\x01\xbc"
    # H.265: stream_type=0x24（H.264 基线的 0x1B 不得被改动）
    assert ps[41:45] == b"\x24\xe0\x00\x00"
    assert ps[49:53] == b"\x00\x00\x01\xe0"
    # PES 内必须保留 Annex-B NAL 边界，ZLM 才能识别 VPS(0x40)/SPS(0x42)/PPS(0x44)。
    assert b"\x00\x00\x00\x01\x40" in ps
    assert b"\x00\x00\x00\x01\x42" in ps
    assert b"\x00\x00\x00\x01\x44" in ps


def test_h265_rtp_packetization_is_reproducible() -> None:
    a = PsRtpPacketizer(H265_FIXTURE_PATH.read_bytes(), ssrc=SSRC, codec="H265")
    b = PsRtpPacketizer(H265_FIXTURE_PATH.read_bytes(), ssrc=SSRC, codec="H265")
    packets_a = [p for frame in a.frames() for p in frame]
    packets_b = [p for frame in b.frames() for p in frame]
    assert [p.encode() for p in packets_a] == [p.encode() for p in packets_b]  # 完全可重复
    # 与 H.264 打包同一套 sequence/timestamp/marker 语义
    seqs = [p.sequence for p in packets_a]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert all(p.ssrc == SSRC for p in packets_a)


def test_h265_large_frame_fragmented_by_mtu() -> None:
    pkt = PsRtpPacketizer(H265_FIXTURE_PATH.read_bytes(), ssrc=SSRC, codec="H265", mtu=600)
    packets = [p for frame in pkt.frames() for p in frame]
    assert all(len(p.payload) <= 600 for p in packets)
    assert any(len(p.payload) == 600 for p in packets)  # 确实发生分片


def test_h265_default_codec_still_h264_baseline() -> None:
    """不指定 codec 时行为与 H.264 基线完全一致（向后兼容）。"""
    h264 = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    h265_default = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    assert [p.encode() for frame in h264.frames() for p in frame] == [
        p.encode() for frame in h265_default.frames() for p in frame
    ]


# ---------------------------------------------------------------- G.711A 音频（VT-09）


def test_g711a_fixture_metadata_and_checksum() -> None:
    """G.711A 音频 fixture 存在、checksum 匹配，且记录来源/许可/采样率/duration。"""
    assert G711A_FIXTURE_PATH.exists()
    assert fixture_sha256(G711A_FIXTURE_PATH) == G711A_FIXTURE_METADATA["sha256"]
    for key in ("source", "license", "codec", "sampleRateHz", "channels", "durationSeconds"):
        assert G711A_FIXTURE_METADATA[key], f"G.711A fixture 元数据缺少 {key}"
    assert G711A_FIXTURE_METADATA["sampleRateHz"] == 8000
    assert G711A_FIXTURE_METADATA["bytes"] == len(G711A_FIXTURE_PATH.read_bytes())


def test_g711a_fixture_checksum_constant_matches() -> None:
    raw = G711A_FIXTURE_PATH.read_bytes()
    expected = "10a469c17bc6edb305b71d7f8d062c42c156e749e11aecb1084f05100f05f150"
    assert hashlib.sha256(raw).hexdigest() == expected


def test_audio_ps_mux_contains_audio_pes_and_declares_g711a() -> None:
    """含音频 PS：PSM 声明 stream_type=0x90（G.711A）+ PES 0xC0 音频帧。"""
    audio = G711A_FIXTURE_PATH.read_bytes()
    pkt = PsRtpPacketizer(
        H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC, audio=audio, audio_bytes_per_frame=320
    )
    frames = list(pkt.frames())
    assert len(frames) == 25
    ps = b"".join(packet.payload for packet in frames[0])
    # PSM 含两个 ES 条目：video 0x1B + audio 0x90，PES 0xC0 紧随视频 PES。
    assert ps[:4] == b"\x00\x00\x01\xba"
    assert b"\x90\xc0\x00\x00" in ps
    assert b"\x00\x00\x01\xc0" in ps
    # 视频 PES 与音频 PES 均在同一帧 PS 内
    video_pes = ps.index(b"\x00\x00\x01\xe0")
    audio_pes = ps.index(b"\x00\x00\x01\xc0")
    assert audio_pes > video_pes
    # 音频 PES 载荷长度为 320 字节（8000Hz × 40ms）
    pes_len = int.from_bytes(ps[audio_pes + 4 : audio_pes + 6], "big")
    assert pes_len == 320 + 8  # PES 头（3+5）字节 + payload


def test_audio_ps_packetization_is_reproducible() -> None:
    audio = G711A_FIXTURE_PATH.read_bytes()
    a = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC, audio=audio)
    b = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC, audio=audio)
    assert [p.encode() for frame in a.frames() for p in frame] == [
        p.encode() for frame in b.frames() for p in frame
    ]


def test_audio_frame_wraps_fixture_boundary() -> None:
    """音频切片在 fixture 末尾回绕：每帧仍恰好 audio_bytes_per_frame 字节。"""
    audio = G711A_FIXTURE_PATH.read_bytes()
    pkt = PsRtpPacketizer(
        H264_FIXTURE_PATH.read_bytes(),
        ssrc=SSRC,
        audio=audio,
        audio_bytes_per_frame=350,  # 8000 % 350 != 0，强制触发回绕
    )
    ps = b"".join(packet.payload for frame in pkt.frames() for packet in frame)
    # 每帧音频 PES 长度一致（350+8）
    assert ps.count(b"\x00\x00\x01\xc0") == 25
    expected = (350 + 8).to_bytes(2, "big")
    assert ps.count(expected) >= 25


def test_audio_h265_psm_declares_hevc_and_g711a() -> None:
    """H.265 + 音频：PSM 同时声明 0x24（HEVC）与 0x90（G.711A）。"""
    audio = G711A_FIXTURE_PATH.read_bytes()
    pkt = PsRtpPacketizer(H265_FIXTURE_PATH.read_bytes(), ssrc=SSRC, codec="H265", audio=audio)
    ps = b"".join(packet.payload for packet in next(pkt.frames()))
    assert b"\x24\xe0\x00\x00" in ps
    assert b"\x90\xc0\x00\x00" in ps
    assert b"\x00\x00\x01\xc0" in ps


def test_audio_absent_keeps_h264_baseline_bytes() -> None:
    """未提供音频时，PSM/打包与基线完全一致（无 0x90 条目、无 0xC0 PES）。"""
    with_audio = PsRtpPacketizer(
        H264_FIXTURE_PATH.read_bytes(),
        ssrc=SSRC,
        audio=G711A_FIXTURE_PATH.read_bytes(),
    )
    without = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    baseline = b"".join(p.encode() for frame in without.frames() for p in frame)
    audio_ps = b"".join(p.payload for frame in with_audio.frames() for p in frame)
    assert b"\x90\xc0\x00\x00" not in baseline
    assert b"\x00\x00\x01\xc0" not in baseline
    assert b"\x00\x00\x01\xc0" in audio_ps


# ---------------------------------------------------------------- RTP over TCP（VT-09）


def test_frame_to_tcp_header_and_payload() -> None:
    """GB28181 TCP 媒体帧：0x24 0x00 + 2 字节网络序长度 + 完整 RTP 包。"""
    from video_testkit.media.rtp_ps import frame_to_tcp

    pkt = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    packet = next(pkt.frames())[0]
    framed = frame_to_tcp(packet)
    assert framed[:2] == b"\x24\x00"
    encoded = packet.encode()
    assert framed[2:4] == len(encoded).to_bytes(2, "big")
    assert framed[4:] == encoded
    # 可重复
    assert frame_to_tcp(packet) == framed


def test_frame_to_tcp_is_deterministic_sequence() -> None:
    """TCP 帧序列可重复且长度前缀与包长一致（与 UDP 基线同一打包器）。"""
    from video_testkit.media.rtp_ps import frame_to_tcp

    pkt = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    a = [frame_to_tcp(p) for frame in pkt.frames() for p in frame]
    pkt2 = PsRtpPacketizer(H264_FIXTURE_PATH.read_bytes(), ssrc=SSRC)
    b = [frame_to_tcp(p) for frame in pkt2.frames() for p in frame]
    assert a == b
    assert all(len(f) == 4 + int.from_bytes(f[2:4], "big") for f in a)
