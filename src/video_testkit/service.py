"""ProviderService：Fake Provider 的全部业务规则与视图转换。

HTTP 路由只负责参数/响应形态，所有确定性逻辑（分页、过滤、幂等、复用、
录像生成、Catalog 操作机）都在这里。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from video_testkit.config import Settings
from video_testkit.errors import ErrorCode, invalid_argument, provider_error
from video_testkit.events import record_event
from video_testkit.idempotency import (
    IdempotencyConflict,
    IdempotencyEntry,
    IdempotencyKeyMissing,
    IdempotencyStore,
    fingerprint_of,
)
from video_testkit.models import (
    CapabilityItem,
    CatalogSyncAccepted,
    CatalogSyncResult,
    ChannelPage,
    DevicePage,
    DeviceStatusData,
    InfoData,
    MediaReference,
    OnlineStatus,
    PlaybackStartRequest,
    ProviderChannel,
    ProviderDevice,
    RecordItem,
    RecordQueryAccepted,
    RecordQueryRequest,
    RecordQueryResult,
    StreamProfile,
    StreamSource,
    StreamView,
)
from video_testkit.scenario import seed_scenario
from video_testkit.state import (
    CatalogOperation,
    ChannelState,
    DeviceState,
    LiveStreamState,
    PlaybackStreamState,
    RecordItemState,
    RecordQuery,
    Store,
    now_utc,
)

BUILD_COMMIT = "dev"
BUILD_TIME = datetime.now(UTC)

CAPABILITIES: list[CapabilityItem] = [
    CapabilityItem(code="DEVICE_DISCOVERY", supported=True),
    CapabilityItem(code="CATALOG_SYNC", supported=True),
    CapabilityItem(code="LIVE_STREAM", supported=True),
    CapabilityItem(code="DEVICE_RECORD_QUERY", supported=True),
    CapabilityItem(code="DEVICE_RECORD_PLAYBACK", supported=True),
    CapabilityItem(code="PROVIDER_EVENTS", supported=True),
    CapabilityItem(code="SNAPSHOT", supported=False),
    CapabilityItem(code="PTZ", supported=False),
    CapabilityItem(code="PLAYBACK_SEEK", supported=False),
    CapabilityItem(code="PLAYBACK_SPEED", supported=False),
    CapabilityItem(code="DEVICE_RECORD_DOWNLOAD", supported=False),
    CapabilityItem(code="TALK", supported=False),
    CapabilityItem(code="BROADCAST", supported=False),
    CapabilityItem(code="PLATFORM_CASCADE", supported=False),
]

ACTIVE_STATES = ("STARTING", "STREAMING")
SUPPORTED_RECORD_TYPES = ("ALL", "TIME")


class ProviderService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.idempotency = IdempotencyStore()

    # ------------------------------------------------------------ 资源查找

    def require_device(self, device_id: str) -> DeviceState:
        device = self.store.devices.get(device_id)
        if device is None:
            raise provider_error(
                ErrorCode.VIDEO_DEVICE_NOT_FOUND,
                "Device not found",
                {"externalDeviceId": device_id},
            )
        return device

    def require_online(self, device: DeviceState) -> None:
        if device.online_status != "ONLINE":
            raise provider_error(
                ErrorCode.VIDEO_DEVICE_OFFLINE,
                "Device is offline",
                {"externalDeviceId": device.external_device_id},
            )

    def require_channel(self, device: DeviceState, channel_id: str) -> ChannelState:
        channel = device.channels.get(channel_id)
        if channel is None:
            raise provider_error(
                ErrorCode.VIDEO_CHANNEL_NOT_FOUND,
                "Channel not found",
                {"externalDeviceId": device.external_device_id, "externalChannelId": channel_id},
            )
        return channel

    @staticmethod
    def _parse_updated_after(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise invalid_argument(
                "updatedAfter 必须为 RFC 3339 时间", {"field": "updatedAfter"}
            ) from None

    # ------------------------------------------------------------ 设备与通道

    def list_devices(
        self,
        page: int,
        page_size: int,
        query: str | None,
        online_status: str | None,
        updated_after: str | None,
    ) -> tuple[list[DeviceState], int]:
        after = self._parse_updated_after(updated_after)
        items = list(self.store.devices.values())
        if query:
            q = query.lower()
            items = [
                d for d in items if q in d.source_name.lower() or q in d.external_device_id.lower()
            ]
        if online_status:
            items = [d for d in items if d.online_status == online_status]
        if after is not None:
            items = [d for d in items if d.updated_at >= after]
        items.sort(key=lambda d: d.external_device_id)
        return self._page(items, page, page_size)

    def get_device(self, device_id: str) -> DeviceState:
        return self.require_device(device_id)

    def list_channels(
        self,
        device_id: str,
        page: int,
        page_size: int,
        query: str | None,
        online_status: str | None,
    ) -> tuple[list[ChannelState], int]:
        device = self.require_device(device_id)
        items = list(device.channels.values())
        if query:
            q = query.lower()
            items = [
                c for c in items if q in c.source_name.lower() or q in c.external_channel_id.lower()
            ]
        if online_status:
            items = [c for c in items if c.online_status == online_status]
        items.sort(key=lambda c: c.external_channel_id)
        return self._page(items, page, page_size)

    def device_status(self, device_id: str) -> DeviceStatusData:
        device = self.require_device(device_id)
        return DeviceStatusData(
            externalDeviceId=device.external_device_id,
            onlineStatus=cast(OnlineStatus, device.online_status),
            lastSeenAt=device.last_seen_at,
            observedAt=now_utc(),
            revision=device.revision,
        )

    @staticmethod
    def _page(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int]:
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    # ------------------------------------------------------------ Catalog 同步

    def submit_catalog_sync(self, device_id: str, idem_key: str) -> tuple[CatalogOperation, bool]:
        device = self.require_device(device_id)
        self.require_online(device)
        fingerprint = fingerprint_of({"externalDeviceId": device_id})
        entry, created = self._resolve(idem_key, fingerprint)
        if not created:
            op = self.store.catalog_operations.get(entry.resource_ref)
            if op is not None:
                return op, False
        op = CatalogOperation(
            operation_id=f"catalog-{uuid.uuid4().hex}",
            external_device_id=device_id,
            status="ACCEPTED",
            submitted_at=now_utc(),
        )
        self.store.catalog_operations[op.operation_id] = op
        entry.resource_ref = op.operation_id
        asyncio.get_running_loop().create_task(self._complete_catalog_sync(op, device))
        return op, True

    async def _complete_catalog_sync(self, op: CatalogOperation, device: DeviceState) -> None:
        await asyncio.sleep(0.05)
        op.status = "RUNNING"
        await asyncio.sleep(0.05)
        device.revision = self.store.next_revision()
        for channel in device.channels.values():
            channel.revision = self.store.next_revision()
            channel.updated_at = now_utc()
        op.discovered_count = device.channel_count
        op.status = "SUCCEEDED"
        op.completed_at = now_utc()
        record_event(
            self.store,
            self.settings,
            "CATALOG_CHANGED",
            device.external_device_id,
            None,
            {"discoveredCount": op.discovered_count},
        )

    def get_catalog_operation(self, operation_id: str) -> CatalogOperation:
        op = self.store.catalog_operations.get(operation_id)
        if op is None:
            raise provider_error(
                ErrorCode.VIDEO_OPERATION_NOT_FOUND,
                "Catalog sync operation not found",
                {"operationId": operation_id},
            )
        return op

    # ------------------------------------------------------------ 实时流

    def start_live_stream(self, req: Any, idem_key: str) -> tuple[LiveStreamState, bool]:
        fingerprint = fingerprint_of(req.model_dump(mode="json"))
        entry, created = self._resolve(idem_key, fingerprint)
        if not created:
            existing = self.store.live_streams.get(entry.resource_ref)
            if existing is not None and existing.state in ACTIVE_STATES:
                return existing, False

        device = self.require_device(req.external_device_id)
        self.require_online(device)
        channel = self.require_channel(device, req.external_channel_id)

        active_key = self.store.active_live_key(
            device.external_device_id, channel.external_channel_id
        )
        if active_key is not None:
            active = self.store.live_streams.get(active_key)
            if active is not None and active.state in ACTIVE_STATES:
                entry.resource_ref = active_key
                return active, False

        key = f"live-{uuid.uuid4().hex}"
        stream_id = f"{channel.external_channel_id}_live_{self.store.next_revision()}"
        base = self.settings.media_base_url.rstrip("/")
        stream = LiveStreamState(
            provider_stream_key=key,
            external_device_id=device.external_device_id,
            external_channel_id=channel.external_channel_id,
            stream_profile=req.stream_profile,
            state="STREAMING",
            media={
                "mediaServerId": "zlm-mock-01",
                "vhost": "__defaultVhost__",
                "app": "rtp",
                "streamId": stream_id,
                "codec": channel.codec,
                "hasAudio": channel.has_audio,
            },
            sources=[
                {"protocol": "WS_FLV", "url": f"{base}/rtp/{stream_id}.flv", "priority": 20},
                {"protocol": "HTTP_FLV", "url": f"{base}/rtp/{stream_id}.flv", "priority": 10},
            ],
            started_at=now_utc(),
        )
        self.store.live_streams[key] = stream
        self.store.set_active_live(device.external_device_id, channel.external_channel_id, key)
        entry.resource_ref = key
        return stream, True

    def get_live_stream(self, key: str) -> LiveStreamState:
        stream = self.store.live_streams.get(key)
        if stream is None:
            raise provider_error(
                ErrorCode.VIDEO_STREAM_NOT_FOUND,
                "Stream not found",
                {"providerStreamKey": key},
            )
        return stream

    def stop_live_stream(self, key: str) -> None:
        stream = self.store.live_streams.get(key)
        if stream is None:
            return  # 幂等 204
        stream.state = "STOPPED"
        stream.stopped_at = now_utc()
        self.store.drop_active_live(stream.external_device_id, stream.external_channel_id, key)

    # ------------------------------------------------------------ 设备录像查询

    def submit_record_query(
        self, req: RecordQueryRequest, idem_key: str
    ) -> tuple[RecordQuery, bool]:
        device = self.require_device(req.external_device_id)
        self.require_online(device)
        channel = self.require_channel(device, req.external_channel_id)
        if req.record_type not in SUPPORTED_RECORD_TYPES:
            raise invalid_argument(
                f"本实现仅支持 recordType={SUPPORTED_RECORD_TYPES}",
                {"recordType": req.record_type},
            )
        fingerprint = fingerprint_of(req.model_dump(mode="json"))
        entry, created = self._resolve(idem_key, fingerprint)
        if not created:
            existing = self.store.record_queries.get(entry.resource_ref)
            if existing is not None:
                return existing, False
        items = self._generate_records(
            device.external_device_id, channel.external_channel_id, req.start_time, req.end_time
        )
        for item in items:
            self.store.records[item.record_key] = item
        query = RecordQuery(
            query_id=f"record-query-{uuid.uuid4().hex}",
            external_device_id=device.external_device_id,
            external_channel_id=channel.external_channel_id,
            status="SUCCEEDED",
            submitted_at=now_utc(),
            completed_at=now_utc(),
            items=items,
        )
        self.store.record_queries[query.query_id] = query
        entry.resource_ref = query.query_id
        return query, True

    @staticmethod
    def _generate_records(
        device_id: str, channel_id: str, start: datetime, end: datetime
    ) -> list[RecordItemState]:
        chunk = timedelta(hours=1)
        cursor = start
        out: list[RecordItemState] = []
        while cursor < end:
            chunk_end = min(cursor + chunk, end)
            key = f"rec-{channel_id}-{cursor.strftime('%Y%m%dT%H%M%S')}Z"
            out.append(
                RecordItemState(
                    record_key=key,
                    external_device_id=device_id,
                    external_channel_id=channel_id,
                    start_time=cursor,
                    end_time=chunk_end,
                    record_type="TIME",
                )
            )
            cursor = chunk_end
        return out

    def get_record_query(self, query_id: str) -> RecordQuery:
        query = self.store.record_queries.get(query_id)
        if query is None:
            raise provider_error(
                ErrorCode.VIDEO_QUERY_NOT_FOUND,
                "Record query not found",
                {"queryId": query_id},
            )
        return query

    # ------------------------------------------------------------ 回放流

    def start_playback(
        self, req: PlaybackStartRequest, idem_key: str
    ) -> tuple[PlaybackStreamState, bool]:
        record = self.store.records.get(req.record_key)
        if record is None:
            raise provider_error(
                ErrorCode.VIDEO_RECORD_NOT_FOUND,
                "Record not found",
                {"recordKey": req.record_key},
            )
        if (
            record.external_device_id != req.external_device_id
            or record.external_channel_id != req.external_channel_id
        ):
            raise provider_error(
                ErrorCode.VIDEO_RECORD_MISMATCH,
                "Record device/channel mismatch",
                {"recordKey": req.record_key},
            )
        if req.start_time is not None and (
            req.start_time != record.start_time or req.end_time != record.end_time
        ):
            raise provider_error(
                ErrorCode.VIDEO_RECORD_MISMATCH,
                "Record time range mismatch",
                {"recordKey": req.record_key},
            )
        device = self.require_device(req.external_device_id)
        self.require_online(device)
        self.require_channel(device, req.external_channel_id)

        fingerprint = fingerprint_of(req.model_dump(mode="json"))
        entry, created = self._resolve(idem_key, fingerprint)
        if not created:
            existing = self.store.playback_streams.get(entry.resource_ref)
            if existing is not None and existing.state in ACTIVE_STATES:
                return existing, False

        key = f"pb-{uuid.uuid4().hex}"
        stream_id = f"{req.external_channel_id}_pb_{self.store.next_revision()}"
        base = self.settings.media_base_url.rstrip("/")
        stream = PlaybackStreamState(
            provider_stream_key=key,
            external_device_id=req.external_device_id,
            external_channel_id=req.external_channel_id,
            record_key=req.record_key,
            state="STREAMING",
            media={
                "mediaServerId": "zlm-mock-01",
                "vhost": "__defaultVhost__",
                "app": "playback",
                "streamId": stream_id,
                "codec": "H264",
                "hasAudio": True,
            },
            sources=[
                {"protocol": "WS_FLV", "url": f"{base}/playback/{stream_id}.flv", "priority": 20},
                {"protocol": "HTTP_FLV", "url": f"{base}/playback/{stream_id}.flv", "priority": 10},
            ],
            started_at=now_utc(),
        )
        self.store.playback_streams[key] = stream
        entry.resource_ref = key
        return stream, True

    def get_playback_stream(self, key: str) -> PlaybackStreamState:
        stream = self.store.playback_streams.get(key)
        if stream is None:
            raise provider_error(
                ErrorCode.VIDEO_STREAM_NOT_FOUND,
                "Playback stream not found",
                {"providerStreamKey": key},
            )
        return stream

    def stop_playback_stream(self, key: str) -> None:
        stream = self.store.playback_streams.get(key)
        if stream is None:
            return
        stream.state = "STOPPED"
        stream.stopped_at = now_utc()

    # ------------------------------------------------------------ 幂等

    def _resolve(self, idem_key: str, fingerprint: str) -> tuple[IdempotencyEntry, bool]:
        try:
            return self.idempotency.resolve(idem_key, fingerprint)
        except IdempotencyKeyMissing:
            raise invalid_argument(
                "写操作必须携带 Idempotency-Key", {"header": "Idempotency-Key"}
            ) from None
        except IdempotencyConflict:
            raise provider_error(
                ErrorCode.VIDEO_IDEMPOTENCY_CONFLICT,
                "Idempotency-Key reused for a different request",
                {"header": "Idempotency-Key"},
            ) from None

    # ------------------------------------------------------------ 控制面辅助

    def reset(self) -> dict[str, int]:
        s = self.store
        s.devices.clear()
        s.live_streams.clear()
        s.playback_streams.clear()
        s.catalog_operations.clear()
        s.record_queries.clear()
        s.records.clear()
        s.events.clear()
        s.idempotency.clear()
        s.ready_override = None
        s.event_seq = 0
        self.idempotency.clear()
        seed_scenario(s)
        return {"devices": len(s.devices)}

    def set_device_online_status(self, device_id: str, online_status: str) -> DeviceStatusData:
        device = self.require_device(device_id)
        if online_status not in ("ONLINE", "OFFLINE", "UNKNOWN"):
            raise invalid_argument(
                "onlineStatus 必须是 ONLINE/OFFLINE/UNKNOWN", {"onlineStatus": online_status}
            )
        device.online_status = online_status
        device.revision = self.store.next_revision()
        device.last_seen_at = now_utc()
        record_event(
            self.store,
            self.settings,
            "DEVICE_ONLINE" if online_status == "ONLINE" else "DEVICE_OFFLINE",
            device.external_device_id,
            None,
            {"onlineStatus": online_status},
        )
        return self.device_status(device_id)

    def ready_status(
        self, registrar_enabled: bool, registrar_listening: bool
    ) -> Literal["READY", "NOT_READY"]:
        override = self.store.ready_override
        if override is not None:
            return "READY" if override else "NOT_READY"
        if registrar_enabled and not registrar_listening:
            return "NOT_READY"
        return "READY"

    # ------------------------------------------------------------ 视图转换

    def device_view(self, d: DeviceState) -> ProviderDevice:
        return ProviderDevice(
            providerInstanceCode=self.settings.provider_instance_code,
            externalDeviceId=d.external_device_id,
            sourceName=d.source_name,
            manufacturer=d.manufacturer,
            model=d.model,
            firmwareVersion=d.firmware_version,
            transport=d.transport,
            streamMode=d.stream_mode,
            charset=d.charset,
            onlineStatus=cast(OnlineStatus, d.online_status),
            channelCount=d.channel_count,
            lastSeenAt=d.last_seen_at,
            revision=d.revision,
        )

    def channel_view(self, d: DeviceState, c: ChannelState) -> ProviderChannel:
        return ProviderChannel(
            providerInstanceCode=self.settings.provider_instance_code,
            externalDeviceId=d.external_device_id,
            externalChannelId=c.external_channel_id,
            parentExternalChannelId=None,
            sourceName=c.source_name,
            manufacturer=c.manufacturer,
            model=c.model,
            onlineStatus=cast(OnlineStatus, c.online_status),
            resolution=c.resolution,
            codec=c.codec,
            hasAudio=c.has_audio,
            supportsPtz=c.supports_ptz,
            supportsDeviceRecord=c.supports_device_record,
            streamIdentification=None,
            revision=c.revision,
        )

    def info_data(self) -> InfoData:
        return InfoData(
            providerType=self.settings.provider_type,
            providerInstanceCode=self.settings.provider_instance_code,
            contractVersion=self.settings.contract_version,
            implementationVersion=self.settings.implementation_version,
            buildCommit=BUILD_COMMIT,
            buildTime=BUILD_TIME,
            protocolStack="MOCK+SIP",
            recordTypesSupported=list(SUPPORTED_RECORD_TYPES),
            authEnabled=self.settings.auth_enabled,
        )

    def device_page_view(
        self, items: list[DeviceState], page: int, page_size: int, total: int
    ) -> DevicePage:
        return DevicePage(
            items=[self.device_view(d) for d in items],
            page=page,
            pageSize=page_size,
            total=total,
        )

    def channel_page_view(
        self, device: DeviceState, items: list[ChannelState], page: int, page_size: int, total: int
    ) -> ChannelPage:
        return ChannelPage(
            items=[self.channel_view(device, c) for c in items],
            page=page,
            pageSize=page_size,
            total=total,
        )

    def catalog_accepted_view(self, op: CatalogOperation) -> CatalogSyncAccepted:
        return CatalogSyncAccepted(
            operationId=op.operation_id,
            status="ACCEPTED",
            submittedAt=op.submitted_at,
        )

    def catalog_result_view(self, op: CatalogOperation) -> CatalogSyncResult:
        return CatalogSyncResult(
            operationId=op.operation_id,
            externalDeviceId=op.external_device_id,
            status=op.status,
            discoveredCount=op.discovered_count,
            submittedAt=op.submitted_at,
            completedAt=op.completed_at,
            error=op.error,
        )

    def record_query_accepted_view(self, q: RecordQuery) -> RecordQueryAccepted:
        return RecordQueryAccepted(
            queryId=q.query_id,
            status="ACCEPTED",
            submittedAt=q.submitted_at,
        )

    def record_query_result_view(self, q: RecordQuery) -> RecordQueryResult:
        return RecordQueryResult(
            queryId=q.query_id,
            status=q.status,
            items=[self.record_item_view(i) for i in q.items],
            submittedAt=q.submitted_at,
            completedAt=q.completed_at,
            error=q.error,
        )

    @staticmethod
    def record_item_view(item: RecordItemState) -> RecordItem:
        return RecordItem(
            recordKey=item.record_key,
            externalDeviceId=item.external_device_id,
            externalChannelId=item.external_channel_id,
            startTime=item.start_time,
            endTime=item.end_time,
            recordType=cast(Literal["TIME"], item.record_type),
            sizeBytes=item.size_bytes,
            sourceAddress=item.source_address,
        )

    def stream_view(self, s: LiveStreamState | PlaybackStreamState, stream_type: str) -> StreamView:
        return StreamView(
            providerStreamKey=s.provider_stream_key,
            streamType=cast(Literal["LIVE", "PLAYBACK"], stream_type),
            externalDeviceId=s.external_device_id,
            externalChannelId=s.external_channel_id,
            streamProfile=(
                cast(StreamProfile, s.stream_profile) if isinstance(s, LiveStreamState) else None
            ),
            state=s.state,
            media=(
                MediaReference(
                    mediaServerId=str(s.media["mediaServerId"]),
                    vhost=str(s.media["vhost"]),
                    app=str(s.media["app"]),
                    streamId=str(s.media["streamId"]),
                    codec=str(s.media["codec"]),
                    hasAudio=bool(s.media["hasAudio"]),
                )
                if s.media
                else None
            ),
            sources=[
                StreamSource(
                    protocol=str(src["protocol"]),
                    url=str(src["url"]),
                    priority=int(str(src["priority"])),
                )
                for src in s.sources
            ],
            startedAt=s.started_at,
            stoppedAt=s.stopped_at,
        )
