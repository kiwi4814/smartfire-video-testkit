"""ProviderService：Fake Provider 的全部业务规则与视图转换。

HTTP 路由只负责参数/响应形态，所有确定性逻辑（分页、过滤、幂等、复用、
录像生成、Catalog 操作机）都在这里。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from video_testkit.catalog_client import CatalogClient
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
from video_testkit.live_client import LiveClient
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
from video_testkit.recordinfo_client import RecordInfoClient
from video_testkit.scenario import seed_scenario
from video_testkit.sip.catalog import CatalogQueryError
from video_testkit.sip.recordinfo import RecordInfoQueryError
from video_testkit.sip.registrar import LiveInviteError
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
from video_testkit.zlm_client import ZlmClient, ZlmError

BUILD_COMMIT = "dev"
BUILD_TIME = datetime.now(UTC)

logger = logging.getLogger(__name__)

# VT-09 可选能力通过契约允许的 constraints 表达（CapabilityCode 枚举固定 14 项，
# 不新增枚举；constraints 为 additionalProperties: true 的自由对象）。
_MEDIA_CAPABILITY_CONSTRAINTS: dict[str, Any] = {
    "codecs": ["H264", "H265"],
    "audioCodecs": ["G711A"],
    "signalingTransports": ["UDP", "TCP"],
    "mediaTransports": ["UDP", "TCP"],
}

CAPABILITIES: list[CapabilityItem] = [
    CapabilityItem(code="DEVICE_DISCOVERY", supported=True),
    CapabilityItem(code="CATALOG_SYNC", supported=True),
    CapabilityItem(code="LIVE_STREAM", supported=True, constraints=_MEDIA_CAPABILITY_CONSTRAINTS),
    CapabilityItem(code="DEVICE_RECORD_QUERY", supported=True),
    CapabilityItem(
        code="DEVICE_RECORD_PLAYBACK", supported=True, constraints=_MEDIA_CAPABILITY_CONSTRAINTS
    ),
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
        # Provider 侧 Catalog 查询客户端（app 装配注入；registrar 关闭时为 None）。
        self.catalog_client: CatalogClient | None = None
        # Provider 侧实时流信令客户端（app 装配注入；registrar 关闭时为 None）。
        self.live_client: LiveClient | None = None
        # Provider 侧 RecordInfo 查询客户端（app 装配注入；registrar 关闭时为 None）。
        self.recordinfo_client: RecordInfoClient | None = None
        # ZLMediaKit 集成客户端（app 装配注入；zlm_api_url 为空时为 None）。
        self.zlm_client: ZlmClient | None = None
        # VT-11：inventory 快照轮次（snapshotToken → 轮次指纹）。token 绑定
        # inventory 内容指纹；目录变化后旧 token 视为过期（409 retryable）。
        self._snapshot_rounds: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ inventory 快照（VT-11）

    def _inventory_fingerprint(self, device_id: str | None = None) -> str:
        """目录内容指纹：设备（含通道）revision 摘要 + 全局变更序列。

        绑定 ``event_seq``（任何设备/通道状态或事件变化都会递增）与逐项
        revision，确保状态注入、Catalog 变更后旧 snapshotToken 过期。
        """
        digest = hashlib.sha256()
        digest.update(str(self.store.event_seq).encode())
        for device in sorted(self.store.devices.values(), key=lambda d: d.external_device_id):
            if device_id is not None and device.external_device_id != device_id:
                continue
            digest.update(device.external_device_id.encode())
            digest.update(str(device.revision).encode())
            for channel in sorted(device.channels.values(), key=lambda c: c.external_channel_id):
                digest.update(channel.external_channel_id.encode())
                digest.update(str(channel.revision).encode())
        return digest.hexdigest()

    def begin_or_continue_snapshot(self, token: str | None, device_id: str | None = None) -> str:
        """开启/延续 inventory 快照轮次并返回 token。

        无 token = 开启新轮次（返回新 token）；带 token = 校验轮次仍有效且
        目录指纹未变化，否则抛 409 VIDEO_CATALOG_SNAPSHOT_EXPIRED（retryable）。
        """
        fingerprint = self._inventory_fingerprint(device_id)
        if not token:
            new_token = uuid.uuid4().hex
            self._snapshot_rounds[new_token] = {"fingerprint": fingerprint}
            return new_token
        round_state = self._snapshot_rounds.get(token)
        if round_state is None or round_state["fingerprint"] != fingerprint:
            raise provider_error(
                ErrorCode.VIDEO_CATALOG_SNAPSHOT_EXPIRED,
                "Inventory snapshot expired",
                {"snapshotToken": token},
            )
        return token

    def clear_snapshots(self) -> None:
        """reset 或目录重建时清理全部快照轮次。"""
        self._snapshot_rounds.clear()

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
        """通过真实 SIP Catalog 查询发现设备目录并更新 Provider 视图。

        非破坏性 reconcile：仅 upsert 发现的通道，不把目录缺失断言为业务删除；
        重复 Catalog 按稳定 DeviceID 去重，不产生重复资源。
        """
        op.status = "RUNNING"
        client = self.catalog_client
        try:
            if client is None:
                raise CatalogQueryError("Catalog 客户端未装配（registrar 关闭）")
            result = await client.query(
                device.external_device_id,
                timeout=self.settings.gb_catalog_query_timeout,
                settle_window=self.settings.gb_catalog_settle_window,
            )
        except (CatalogQueryError, TimeoutError) as exc:
            op.status = "FAILED"
            op.error = {"reason": str(exc)}
            op.completed_at = now_utc()
            return

        device.revision = self.store.next_revision()
        for item in result.items:
            channel = device.channels.get(item.device_id)
            if channel is None:
                device.channels[item.device_id] = ChannelState(
                    external_device_id=device.external_device_id,
                    external_channel_id=item.device_id,
                    source_name=item.name,
                    manufacturer=item.manufacturer,
                    model=item.model,
                    online_status="ONLINE" if item.status == "ON" else "OFFLINE",
                    resolution=item.resolution or "1280x720",
                    codec=item.codec or "H264",
                    has_audio=item.has_audio,
                    supports_ptz=item.supports_ptz,
                    supports_device_record=item.supports_device_record,
                    revision=self.store.next_revision(),
                )
            else:
                channel.revision = self.store.next_revision()
                channel.updated_at = now_utc()
        op.discovered_count = len(result.items)
        op.status = "SUCCEEDED" if result.complete else "PARTIAL"
        op.completed_at = now_utc()
        record_event(
            self.store,
            self.settings,
            "CATALOG_CHANGED",
            device.external_device_id,
            None,
            {
                "discoveredCount": op.discovered_count,
                "complete": result.complete,
                "sumNum": result.sum_num,
            },
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
            # ZLM 集成模式下由媒体到达驱动 STREAMING；未集成保持 mock 即时 STREAMING。
            state="STARTING" if self.zlm_client is not None else "STREAMING",
            media={
                "mediaServerId": (
                    "zlm-mock-01" if self.zlm_client is None else self.settings.zlm_media_server_id
                ),
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
        # 后台经真实 SIP INVITE/ACK 建立 Dialog；ZLM 模式下等待 stream-online 再 STREAMING。
        asyncio.get_running_loop().create_task(self._establish_live_stream(stream))
        return stream, True

    async def _establish_live_stream(self, stream: LiveStreamState) -> None:
        """后台：预留端口 → INVITE → 按协商 SSRC 开 RTP → 等 online → STREAMING。

        失败路径收敛 FAILED 并在 finally 中强制关闭 ZLM RTP 端口（teardown）。
        """
        client = self.live_client
        zlm = self.zlm_client
        if client is None:
            return  # registrar 关闭：保持 mock 行为
        assert stream.media is not None
        stream_id = str(stream.media["streamId"])
        rtp_port: int | None = None
        dialog: Any = None
        try:
            if zlm is not None:
                rtp_port = zlm.next_rtp_port()
            dialog = await client.establish(
                stream.external_device_id,
                timeout=self.settings.gb_live_invite_timeout,
                sdp_media=(self.settings.zlm_rtp_host, rtp_port) if rtp_port else None,
            )
            if zlm is not None:
                await zlm.open_rtp_server(
                    stream_id,
                    port=rtp_port,
                    ssrc=int(dialog.ssrc, 10) & 0xFFFFFFFF,
                    tcp_mode=1 if getattr(dialog, "media_transport", "UDP") == "TCP" else 0,
                )
                online = await zlm.wait_stream_online(
                    stream_id, self.settings.zlm_stream_online_timeout
                )
                if not online:
                    stream.state = "FAILED"
                    logger.info(
                        "live stream no media",
                        extra={"providerStreamKey": stream.provider_stream_key},
                    )
                    return
                stream.state = "STREAMING"
        except (LiveInviteError, TimeoutError, ZlmError) as exc:
            stream.state = "FAILED"
            logger.info(
                "live stream establish failed",
                extra={"providerStreamKey": stream.provider_stream_key, "reason": str(exc)},
            )
            return
        finally:
            if zlm is not None and rtp_port is not None and stream.state != "STREAMING":
                await zlm.close_rtp_server(stream_id)

        if stream.state != "STREAMING":
            # start 后已被 stop：立即清理设备侧 Dialog，避免遗留。
            if dialog is not None:
                await client.teardown(
                    stream.external_device_id, dialog, self.settings.gb_live_bye_timeout
                )
            return
        client.attach_dialog(stream.provider_stream_key, dialog)
        stream.state = "STREAMING"

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
        stream = self.store.live_streams.pop(key, None)
        if stream is None:
            return  # 幂等 204
        stream.state = "STOPPED"
        stream.stopped_at = now_utc()
        self.store.drop_active_live(stream.external_device_id, stream.external_channel_id, key)
        client = self.live_client
        if client is None:
            return
        dialog = client.dialog(key)
        client.detach_dialog(key)
        if dialog is not None:
            # 后台经真实 BYE 拆除设备侧 Dialog；超时不影响 Provider 侧 204。
            asyncio.get_running_loop().create_task(self._teardown_live_stream(stream, dialog))

    async def _teardown_live_stream(self, stream: LiveStreamState, dialog: Any) -> None:
        client = self.live_client
        zlm = self.zlm_client
        # 幂等关闭 ZLM RTP 端口与流，避免遗留 orphan stream。
        if zlm is not None:
            assert stream.media is not None
            with contextlib.suppress(ZlmError):
                await zlm.close_rtp_server(str(stream.media["streamId"]))
        if client is not None:
            await client.teardown(
                stream.external_device_id, dialog, self.settings.gb_live_bye_timeout
            )

    # ------------------------------------------------------------ 设备录像查询

    def submit_record_query(
        self, req: RecordQueryRequest, idem_key: str
    ) -> tuple[RecordQuery, bool]:
        """提交设备录像目录查询：ACCEPTED 后后台经真实 SIP RecordInfo 收敛。

        相同 Idempotency-Key 复用既有查询，不向设备发起第二次查询。
        """
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
        query = RecordQuery(
            query_id=f"record-query-{uuid.uuid4().hex}",
            external_device_id=device.external_device_id,
            external_channel_id=channel.external_channel_id,
            status="ACCEPTED",
            submitted_at=now_utc(),
        )
        self.store.record_queries[query.query_id] = query
        entry.resource_ref = query.query_id
        asyncio.get_running_loop().create_task(
            self._complete_record_query(
                query, device, channel, req.start_time, req.end_time, req.record_type
            )
        )
        return query, True

    async def _complete_record_query(
        self,
        query: RecordQuery,
        device: DeviceState,
        channel: ChannelState,
        start_time: datetime,
        end_time: datetime,
        record_type: str,
    ) -> None:
        """通过真实 SIP RecordInfo 查询设备录像目录并生成 Provider 视图。

        设备响应按稳定时间区间聚合，重复/乱序不改变结果身份；PARTIAL/timeout
        保留已收集有效项；完全无响应收敛 FAILED。
        """
        query.status = "RUNNING"
        client = self.recordinfo_client
        try:
            if client is None:
                raise RecordInfoQueryError("RecordInfo 客户端未装配（registrar 关闭）")
            result = await client.query(
                device.external_device_id,
                channel.external_channel_id,
                start_time,
                end_time,
                record_type,
                timeout=self.settings.gb_recordinfo_query_timeout,
                settle_window=self.settings.gb_recordinfo_settle_window,
            )
        except (RecordInfoQueryError, TimeoutError) as exc:
            query.status = "FAILED"
            query.error = {"reason": str(exc)}
            query.completed_at = now_utc()
            return

        items: list[RecordItemState] = []
        for rec in result.items:
            # 不透明且稳定：recordKey 绑定通道与左闭右开区间起点，重复查询不变。
            key = f"rec-{channel.external_channel_id}-{rec.start_time.strftime('%Y%m%dT%H%M%S')}Z"
            item = RecordItemState(
                record_key=key,
                external_device_id=device.external_device_id,
                external_channel_id=channel.external_channel_id,
                start_time=rec.start_time,
                end_time=rec.end_time,
                record_type="TIME",
                size_bytes=rec.file_size or None,
            )
            items.append(item)
            self.store.records[item.record_key] = item
        query.items = items
        query.status = "SUCCEEDED" if result.complete else "PARTIAL"
        query.completed_at = now_utc()

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
            state="STARTING" if self.zlm_client is not None else "STREAMING",
            media={
                "mediaServerId": (
                    "zlm-mock-01" if self.zlm_client is None else self.settings.zlm_media_server_id
                ),
                "vhost": "__defaultVhost__",
                "app": "rtp" if self.zlm_client is not None else "playback",
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
        # 后台经真实 SIP INVITE(s=Playback)/ACK 建立 Dialog；
        # ZLM 模式下等待 stream-online 再收敛 STREAMING。
        asyncio.get_running_loop().create_task(self._establish_playback_stream(stream))
        return stream, True

    async def _establish_playback_stream(self, stream: PlaybackStreamState) -> None:
        """后台：预留端口 → INVITE(s=Playback) → 按协商 SSRC 开 RTP → 等 online → STREAMING。

        失败路径收敛 FAILED 并在 finally 中强制关闭 ZLM RTP 端口。
        """
        client = self.live_client
        zlm = self.zlm_client
        if client is None:
            return  # registrar 关闭：保持 mock 行为
        assert stream.media is not None
        stream_id = str(stream.media["streamId"])
        rtp_port: int | None = None
        dialog: Any = None
        try:
            if zlm is not None:
                rtp_port = zlm.next_rtp_port()
            dialog = await client.establish(
                stream.external_device_id,
                timeout=self.settings.gb_live_invite_timeout,
                sdp_media=(self.settings.zlm_rtp_host, rtp_port) if rtp_port else None,
                session_name="Playback",
            )
            if zlm is not None:
                await zlm.open_rtp_server(
                    stream_id,
                    port=rtp_port,
                    ssrc=int(dialog.ssrc, 10) & 0xFFFFFFFF,
                    tcp_mode=1 if getattr(dialog, "media_transport", "UDP") == "TCP" else 0,
                )
                online = await zlm.wait_stream_online(
                    stream_id, self.settings.zlm_stream_online_timeout
                )
                if not online:
                    stream.state = "FAILED"
                    logger.info(
                        "playback stream no media",
                        extra={"providerStreamKey": stream.provider_stream_key},
                    )
                    return
                stream.state = "STREAMING"
        except (LiveInviteError, TimeoutError, ZlmError) as exc:
            stream.state = "FAILED"
            logger.info(
                "playback stream establish failed",
                extra={"providerStreamKey": stream.provider_stream_key, "reason": str(exc)},
            )
            return
        finally:
            if zlm is not None and rtp_port is not None and stream.state != "STREAMING":
                await zlm.close_rtp_server(stream_id)

        if stream.state != "STREAMING":
            if dialog is not None:
                await client.teardown(
                    stream.external_device_id, dialog, self.settings.gb_live_bye_timeout
                )
            return
        client.attach_dialog(stream.provider_stream_key, dialog)
        stream.state = "STREAMING"

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
        stream = self.store.playback_streams.pop(key, None)
        if stream is None:
            return  # 幂等 204
        stream.state = "STOPPED"
        stream.stopped_at = now_utc()
        client = self.live_client
        if client is None:
            return
        dialog = client.dialog(key)
        client.detach_dialog(key)
        if dialog is not None:
            asyncio.get_running_loop().create_task(self._teardown_playback_stream(stream, dialog))

    async def _teardown_playback_stream(self, stream: PlaybackStreamState, dialog: Any) -> None:
        client = self.live_client
        zlm = self.zlm_client
        if zlm is not None:
            assert stream.media is not None
            with contextlib.suppress(ZlmError):
                await zlm.close_rtp_server(str(stream.media["streamId"]))
        if client is not None:
            await client.teardown(
                stream.external_device_id, dialog, self.settings.gb_live_bye_timeout
            )

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
        self.clear_snapshots()
        if self.live_client is not None:
            self.live_client.reset()
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

    # ------------------------------------------------------------ Keepalive 收敛

    def record_keepalive(self, device_id: str, sn: int) -> None:
        """Provider 收到设备 Keepalive 心跳：刷新心跳时间，离线设备恢复 ONLINE。"""
        device = self.store.devices.get(device_id)
        if device is None:
            # 未知设备不建立状态；心跳不驱动协议外身份。
            return
        now = now_utc()
        was_offline = device.online_status != "ONLINE"
        device.keepalive_seen_at = now
        device.last_seen_at = now
        device.revision = self.store.next_revision()
        device.updated_at = now
        if was_offline:
            device.online_status = "ONLINE"
            record_event(
                self.store,
                self.settings,
                "DEVICE_ONLINE",
                device_id,
                None,
                {"onlineStatus": "ONLINE", "keepaliveSn": sn},
            )

    def expire_stale_devices(self) -> int:
        """将超过 ``gb_keepalive_timeout`` 未心跳的在线设备置为 OFFLINE。返回变更数。"""
        now = now_utc()
        timeout_sec = self.settings.gb_keepalive_timeout
        changed = 0
        for device in self.store.devices.values():
            if device.online_status != "ONLINE" or device.keepalive_seen_at is None:
                continue
            if (now - device.keepalive_seen_at).total_seconds() <= timeout_sec:
                continue
            device.online_status = "OFFLINE"
            device.revision = self.store.next_revision()
            record_event(
                self.store,
                self.settings,
                "DEVICE_OFFLINE",
                device.external_device_id,
                None,
                {"onlineStatus": "OFFLINE"},
            )
            changed += 1
        return changed

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
            providerEpoch=self.settings.provider_epoch,
        )

    def device_page_view(
        self,
        items: list[DeviceState],
        page: int,
        page_size: int,
        total: int,
        snapshot_token: str = "",
    ) -> DevicePage:
        return DevicePage(
            items=[self.device_view(d) for d in items],
            page=page,
            pageSize=page_size,
            total=total,
            snapshotToken=snapshot_token,
        )

    def channel_page_view(
        self,
        device: DeviceState,
        items: list[ChannelState],
        page: int,
        page_size: int,
        total: int,
        snapshot_token: str = "",
    ) -> ChannelPage:
        return ChannelPage(
            items=[self.channel_view(device, c) for c in items],
            page=page,
            pageSize=page_size,
            total=total,
            snapshotToken=snapshot_token,
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
            discoveredCount=op.discovered_count or 0,
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
