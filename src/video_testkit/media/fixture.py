"""媒体 fixture：可再分发的确定性 H.264/H.265 测试片段。

来源与许可：使用 FFmpeg 8.1.1 ``lavfi testsrc`` 合成图案生成（1280x720，
25fps，1 秒），不包含任何第三方受版权内容，可安全再分发。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MEDIA_DIR = Path(__file__).parent
H264_FIXTURE_PATH = MEDIA_DIR / "testkit-1s-720p.h264"
H265_FIXTURE_PATH = MEDIA_DIR / "testkit-1s-720p.h265"
# G.711A（PCM A-law）原始音频：8000Hz × 1 秒 × 单声道 × 1 字节采样 = 8000 字节。
G711A_FIXTURE_PATH = MEDIA_DIR / "testkit-1s-g711a.alaw"

FIXTURE_METADATA: dict[str, object] = {
    "source": "Generated with FFmpeg 8.1.1 lavfi testsrc (synthetic test pattern)",
    "license": "Redistribution-safe: synthetically generated, no third-party content",
    "sha256": "f6d951611bf49c4522e0e04deec88fadfb7fac9d9fbf425f3efa9231c87be67d",
    "codec": "H264 baseline (libx264, yuv420p)",
    "resolution": "1280x720",
    "durationSeconds": 1.0,
    "frames": 25,
    "nalStructure": "SPS/PPS/SEI/IDR + 24 P-frames (keyint=25)",
}

H265_FIXTURE_METADATA: dict[str, object] = {
    "source": "Generated with FFmpeg 8.1.1 lavfi testsrc (synthetic test pattern)",
    "license": "Redistribution-safe: synthetically generated, no third-party content",
    "sha256": "564d78446629aba662b95dee046449b5ebed09fb42ce29d2561ea925cd881a20",
    "codec": "H265 Main (libx265, yuv420p)",
    "resolution": "1280x720",
    "durationSeconds": 1.0,
    "frames": 25,
    "nalStructure": "VPS/SPS/PPS/SEI/IDR + 24 inter frames (keyint=25)",
}

G711A_FIXTURE_METADATA: dict[str, object] = {
    "source": "Generated with FFmpeg 8.1.1 lavfi sine (440Hz synthetic tone)",
    "license": "Redistribution-safe: synthetically generated, no third-party content",
    "sha256": "10a469c17bc6edb305b71d7f8d062c42c156e749e11aecb1084f05100f05f150",
    "codec": "G.711A (PCM A-law, ffmpeg pcm_alaw)",
    "sampleRateHz": 8000,
    "channels": 1,
    "durationSeconds": 1.0,
    "bytes": 8000,
}


def fixture_sha256(path: Path = H264_FIXTURE_PATH) -> str:
    """计算 fixture 当前 SHA-256（用于校验与报告）；默认校验 H.264 fixture。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_fixture(codec: str = "H264") -> None:
    """启动/打包前校验 fixture 完整性与 checksum；不匹配抛 RuntimeError。

    codec 取值：H264 / H265 / G711A；任一 fixture 缺失或 checksum 不匹配即失败。
    """
    if codec == "H265":
        path, metadata = H265_FIXTURE_PATH, H265_FIXTURE_METADATA
    elif codec == "G711A":
        path, metadata = G711A_FIXTURE_PATH, G711A_FIXTURE_METADATA
    else:
        path, metadata = H264_FIXTURE_PATH, FIXTURE_METADATA
    if not path.exists():
        raise RuntimeError(f"{codec} fixture 缺失: {path}")
    actual = fixture_sha256(path)
    expected = str(metadata["sha256"])
    if actual != expected:
        raise RuntimeError(f"{codec} fixture checksum 不匹配: expected={expected} actual={actual}")
