"""内存状态存储：Fake Provider 的确定性内存状态。

所有变更都发生在 uvicorn 的单一事件循环内（HTTP 请求处理器与后台任务），
因此无需加锁；控制面 ``/testkit/v1/reset`` 重建整个 Store 实现确定性复位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from video_testkit.idempotency import IdempotencyEntry
from video_testkit.models import CatalogOpStatus, RecordQueryStatus, StreamState


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass
class ChannelState:
    external_device_id: str
    external_channel_id: str
    source_name: str
    manufacturer: str
    model: str
    online_status: str
    resolution: str
    codec: str
    has_audio: bool
    supports_ptz: bool
    supports_device_record: bool
    revision: str
    updated_at: datetime = field(default_factory=now_utc)


@dataclass
class DeviceState:
    external_device_id: str
    source_name: str
    manufacturer: str
    model: str
    firmware_version: str
    transport: str
    stream_mode: str
    charset: str
    online_status: str
    last_seen_at: datetime | None
    revision: str
    updated_at: datetime = field(default_factory=now_utc)
    # 最近一次 Keepalive 心跳时间；仅收到过心跳的设备参与离线超时收敛。
    keepalive_seen_at: datetime | None = None
    channels: dict[str, ChannelState] = field(default_factory=dict)

    @property
    def channel_count(self) -> int:
        return len(self.channels)


@dataclass
class LiveStreamState:
    provider_stream_key: str
    external_device_id: str
    external_channel_id: str
    stream_profile: str
    state: StreamState
    media: dict[str, object] | None
    sources: list[dict[str, object]]
    started_at: datetime
    stopped_at: datetime | None = None


@dataclass
class PlaybackStreamState:
    provider_stream_key: str
    external_device_id: str
    external_channel_id: str
    record_key: str
    state: StreamState
    media: dict[str, object] | None
    sources: list[dict[str, object]]
    started_at: datetime
    stopped_at: datetime | None = None


@dataclass
class CatalogOperation:
    operation_id: str
    external_device_id: str
    status: CatalogOpStatus
    submitted_at: datetime
    completed_at: datetime | None = None
    discovered_count: int | None = None
    error: dict[str, object] | None = None


@dataclass
class RecordItemState:
    record_key: str
    external_device_id: str
    external_channel_id: str
    start_time: datetime
    end_time: datetime
    record_type: str
    size_bytes: int | None = None
    source_address: str | None = None


@dataclass
class RecordQuery:
    query_id: str
    external_device_id: str
    external_channel_id: str
    status: RecordQueryStatus
    submitted_at: datetime
    completed_at: datetime | None = None
    items: list[RecordItemState] = field(default_factory=list)
    error: dict[str, object] | None = None


@dataclass
class ProviderEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    revision: str
    resource_device_id: str | None
    resource_channel_id: str | None
    data: dict[str, object]
    delivery_state: str  # PENDING / DELIVERED / FAILED / NOT_CONFIGURED
    attempts: int = 0
    last_error: str | None = None


class Store:
    """Fake Provider 的全部可复位状态。"""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceState] = {}
        self.live_streams: dict[str, LiveStreamState] = {}
        self.playback_streams: dict[str, PlaybackStreamState] = {}
        self._active_live_by_channel: dict[tuple[str, str], str] = {}
        self.catalog_operations: dict[str, CatalogOperation] = {}
        self.record_queries: dict[str, RecordQuery] = {}
        self.records: dict[str, RecordItemState] = {}
        self.events: list[ProviderEvent] = []
        self.idempotency: dict[str, IdempotencyEntry] = {}
        self.ready_override: bool | None = None
        self.event_seq: int = 0

    # ---- 辅助 ----
    def active_live_key(self, device_id: str, channel_id: str) -> str | None:
        return self._active_live_by_channel.get((device_id, channel_id))

    def set_active_live(self, device_id: str, channel_id: str, stream_key: str) -> None:
        self._active_live_by_channel[(device_id, channel_id)] = stream_key

    def drop_active_live(self, device_id: str, channel_id: str, stream_key: str) -> None:
        current = self._active_live_by_channel.get((device_id, channel_id))
        if current == stream_key:
            self._active_live_by_channel.pop((device_id, channel_id), None)

    def next_revision(self) -> str:
        self.event_seq += 1
        return str(self.event_seq)
