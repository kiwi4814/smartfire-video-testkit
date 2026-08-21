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
    dev_0003 = DeviceState(
        external_device_id="34020000001320000003",
        source_name="总部高层监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0003.channels["34020000001310000022"] = _channel("34020000001320000003", "34020000001310000022", "总部消控室在岗检测枪机", "1920x1080", "H264", False, False)
    dev_0003.channels["34020000001310000023"] = _channel("34020000001320000003", "34020000001310000023", "总部电梯厅电瓶车检测枪机A", "1920x1080", "H264", False, False)
    dev_0003.channels["34020000001310000024"] = _channel("34020000001320000003", "34020000001310000024", "总部电梯厅电瓶车检测枪机B", "1920x1080", "H264", False, False)
    dev_0003.channels["34020000001310000025"] = _channel("34020000001320000003", "34020000001310000025", "总部疏散通道消防枪机", "1920x1080", "H264", False, False)
    dev_0003.channels["34020000001310000026"] = _channel("34020000001320000003", "34020000001310000026", "总部地下车库热成像球机", "1280x720", "H264", False, True)
    dev_0003.channels["34020000001310000027"] = _channel("34020000001320000003", "34020000001310000027", "总部屋面高空瞭望鹰眼", "1920x1080", "H264", False, True)
    dev_0003.channels["34020000001310000028"] = _channel("34020000001320000003", "34020000001310000028", "总部大堂全景枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000003"] = dev_0003

    dev_0004 = DeviceState(
        external_device_id="34020000001320000004",
        source_name="研发办公楼监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0004.channels["34020000001310000029"] = _channel("34020000001320000004", "34020000001310000029", "研发楼电梯厅电瓶车检测枪机", "1920x1080", "H264", False, False)
    dev_0004.channels["34020000001310000030"] = _channel("34020000001320000004", "34020000001310000030", "研发楼疏散通道枪机", "1920x1080", "H264", False, False)
    dev_0004.channels["34020000001310000031"] = _channel("34020000001320000004", "34020000001310000031", "研发楼地下车库热成像球机", "1280x720", "H264", False, True)
    store.devices["34020000001320000004"] = dev_0004

    dev_0005 = DeviceState(
        external_device_id="34020000001320000005",
        source_name="智能制造一号厂房监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0005.channels["34020000001310000032"] = _channel("34020000001320000005", "34020000001310000032", "一号厂房明火烟雾检测枪机", "1920x1080", "H264", False, False)
    dev_0005.channels["34020000001310000033"] = _channel("34020000001320000005", "34020000001310000033", "一号厂房配电间热成像仪", "1280x720", "H264", False, True)
    dev_0005.channels["34020000001310000034"] = _channel("34020000001320000005", "34020000001310000034", "一号厂房消防通道占用枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000005"] = dev_0005

    dev_0006 = DeviceState(
        external_device_id="34020000001320000006",
        source_name="装配测试二号厂房监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0006.channels["34020000001310000035"] = _channel("34020000001320000006", "34020000001310000035", "二号厂房明火烟雾检测枪机", "1920x1080", "H264", False, False)
    dev_0006.channels["34020000001310000036"] = _channel("34020000001320000006", "34020000001310000036", "二号厂房配电间热成像仪", "1280x720", "H264", False, True)
    dev_0006.channels["34020000001310000037"] = _channel("34020000001320000006", "34020000001310000037", "二号厂房装配区行为分析枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000006"] = dev_0006

    dev_0007 = DeviceState(
        external_device_id="34020000001320000007",
        source_name="综合仓储一号楼监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0007.channels["34020000001310000038"] = _channel("34020000001320000007", "34020000001310000038", "综合仓储堆垛热成像云台", "1280x720", "H264", False, True)
    dev_0007.channels["34020000001310000039"] = _channel("34020000001320000007", "34020000001310000039", "综合仓储消防通道占用枪机", "1920x1080", "H264", False, False)
    dev_0007.channels["34020000001310000040"] = _channel("34020000001320000007", "34020000001310000040", "综合仓储卸货区全景枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000007"] = dev_0007

    dev_0008 = DeviceState(
        external_device_id="34020000001320000008",
        source_name="成品仓储二号楼监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0008.channels["34020000001310000041"] = _channel("34020000001320000008", "34020000001310000041", "成品仓储堆垛热成像云台", "1280x720", "H264", False, True)
    dev_0008.channels["34020000001310000042"] = _channel("34020000001320000008", "34020000001310000042", "成品仓储消防疏散通道枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000008"] = dev_0008

    dev_0009 = DeviceState(
        external_device_id="34020000001320000009",
        source_name="能源与消防水泵中心监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0009.channels["34020000001310000043"] = _channel("34020000001320000009", "34020000001310000043", "消防水泵房巡检枪机", "1920x1080", "H264", False, False)
    dev_0009.channels["34020000001310000044"] = _channel("34020000001320000009", "34020000001310000044", "高压配电室热成像仪", "1280x720", "H264", False, True)
    dev_0009.channels["34020000001310000045"] = _channel("34020000001320000009", "34020000001310000045", "消防泵房明火检测枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000009"] = dev_0009

    dev_0010 = DeviceState(
        external_device_id="34020000001320000010",
        source_name="物流调度与门卫中心监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0010.channels["34020000001310000046"] = _channel("34020000001320000010", "34020000001310000046", "园区西门出入口枪机", "1920x1080", "H264", False, False)
    dev_0010.channels["34020000001310000047"] = _channel("34020000001320000010", "34020000001310000047", "消防车通道占用检测枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000010"] = dev_0010

    dev_0011 = DeviceState(
        external_device_id="34020000001320000011",
        source_name="高位仓储中心监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0011.channels["34020000001310000048"] = _channel("34020000001320000011", "34020000001310000048", "高位仓储堆垛热成像云台", "1280x720", "H264", False, True)
    dev_0011.channels["34020000001310000049"] = _channel("34020000001320000011", "34020000001310000049", "高位仓储消防通道占用枪机", "1920x1080", "H264", False, False)
    dev_0011.channels["34020000001310000050"] = _channel("34020000001320000011", "34020000001310000050", "高位仓储顶棚高空瞭望", "1920x1080", "H264", False, True)
    store.devices["34020000001320000011"] = dev_0011

    dev_0012 = DeviceState(
        external_device_id="34020000001320000012",
        source_name="物流分拨中心监控NVR",
        manufacturer=MANUFACTURER,
        model="NVR-MOCK",
        firmware_version="1.0.0",
        transport="UDP",
        stream_mode="UDP",
        charset="GB2312",
        online_status="ONLINE",
        last_seen_at=now,
        revision="1",
    )
    dev_0012.channels["34020000001310000051"] = _channel("34020000001320000012", "34020000001310000051", "分拨作业区全景枪机", "1920x1080", "H264", False, False)
    store.devices["34020000001320000012"] = dev_0012
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
