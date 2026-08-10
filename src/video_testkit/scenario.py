"""内置场景：1 台 4 通道 NVR + 1 台 IPC。

所有身份为 GB28181 风格 20 位数字 ID（契约示例同构）：
- NVR 设备 ``34020000001320000001``，通道 ``34020000001310000001..04``；
- IPC 设备 ``34020000001320000002``，单通道 ``34020000001310000021``。
"""

from __future__ import annotations

from video_testkit.state import ChannelState, DeviceState, Store, now_utc

NVR_DEVICE_ID = "34020000001320000001"
IPC_DEVICE_ID = "34020000001320000002"

NVR_CHANNEL_IDS = [
    "34020000001310000001",
    "34020000001310000002",
    "34020000001310000003",
    "34020000001310000004",
]
IPC_CHANNEL_ID = "34020000001310000021"

MANUFACTURER = "TESTKIT"


def _channel(
    device_id: str,
    channel_id: str,
    source_name: str,
    resolution: str,
    codec: str,
    has_audio: bool,
    supports_ptz: bool = False,
    revision: str = "1",
) -> ChannelState:
    return ChannelState(
        external_device_id=device_id,
        external_channel_id=channel_id,
        source_name=source_name,
        manufacturer=MANUFACTURER,
        model="CH-MOCK-1080P",
        online_status="ONLINE",
        resolution=resolution,
        codec=codec,
        has_audio=has_audio,
        supports_ptz=supports_ptz,
        supports_device_record=True,
        revision=revision,
    )


def seed_scenario(store: Store, provider_instance_code: str | None = None) -> Store:
    """用内置场景重建 Store。返回同一 Store 以便链式调用。"""
    now = now_utc()

    nvr_channels = [
        _channel(NVR_DEVICE_ID, NVR_CHANNEL_IDS[0], "走廊东门", "1920x1080", "H264", True),
        _channel(NVR_DEVICE_ID, NVR_CHANNEL_IDS[1], "走廊西门", "1280x720", "H264", False),
        _channel(NVR_DEVICE_ID, NVR_CHANNEL_IDS[2], "停车场入口", "1920x1080", "H265", False),
        _channel(
            NVR_DEVICE_ID,
            NVR_CHANNEL_IDS[3],
            "消防通道",
            "1920x1080",
            "H264",
            True,
            supports_ptz=True,
        ),
    ]
    nvr = DeviceState(
        external_device_id=NVR_DEVICE_ID,
        source_name="测试NVR-01",
        manufacturer=MANUFACTURER,
        model="NVR-4CH-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    for ch in nvr_channels:
        nvr.channels[ch.external_channel_id] = ch

    ipc_channel = _channel(
        IPC_DEVICE_ID, IPC_CHANNEL_ID, "车间A区", "1280x720", "H264", True, supports_ptz=True
    )
    ipc = DeviceState(
        external_device_id=IPC_DEVICE_ID,
        source_name="测试IPC-01",
        manufacturer=MANUFACTURER,
        model="IPC-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    ipc.channels[ipc_channel.external_channel_id] = ipc_channel

    store.devices[NVR_DEVICE_ID] = nvr
    store.devices[IPC_DEVICE_ID] = ipc
    return store


def scenario_summary(store: Store) -> dict[str, object]:
    return {
        "name": "ipc-nvr-4ch",
        "description": "1 台 4 通道 NVR + 1 台 IPC",
        "devices": {
            did: {
                "sourceName": d.source_name,
                "onlineStatus": d.online_status,
                "channelCount": d.channel_count,
            }
            for did, d in sorted(store.devices.items())
        },
    }
