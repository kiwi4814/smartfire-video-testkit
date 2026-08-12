"""媒体 fixture：可再分发的确定性 H.264 测试片段。

来源与许可：使用 FFmpeg 8.1.1 ``lavfi testsrc`` 合成图案生成（1280x720，
25fps，1 秒），不包含任何第三方受版权内容，可安全再分发。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MEDIA_DIR = Path(__file__).parent
H264_FIXTURE_PATH = MEDIA_DIR / "testkit-1s-720p.h264"

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


def fixture_sha256() -> str:
    """计算 fixture 当前 SHA-256（用于校验与报告）。"""
    return hashlib.sha256(H264_FIXTURE_PATH.read_bytes()).hexdigest()


def verify_fixture() -> None:
    """启动/打包前校验 fixture 完整性与 checksum；不匹配抛 RuntimeError。"""
    if not H264_FIXTURE_PATH.exists():
        raise RuntimeError(f"H.264 fixture 缺失: {H264_FIXTURE_PATH}")
    actual = fixture_sha256()
    expected = str(FIXTURE_METADATA["sha256"])
    if actual != expected:
        raise RuntimeError(f"H.264 fixture checksum 不匹配: expected={expected} actual={actual}")
