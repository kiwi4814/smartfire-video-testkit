"""契约数据类型（Pydantic 模型）。

字段采用 camelCase（契约要求）；时间统一序列化为 RFC 3339 UTC ``Z`` 毫秒格式。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

UTC_Z_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_utc_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


AwDatetime = Annotated[
    datetime,
    PlainSerializer(lambda v: format_utc_z(v), return_type=str, when_used="json"),
]

OnlineStatus = Literal["ONLINE", "OFFLINE", "UNKNOWN"]
StreamState = Literal["STARTING", "STREAMING", "STOPPING", "STOPPED", "FAILED"]
CatalogOpStatus = Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "EXPIRED"]
RecordQueryStatus = Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"]
RecordType = Literal["ALL", "TIME", "ALARM", "MANUAL"]
StreamProfile = Literal["AUTO", "MAIN", "SUB"]

_T = TypeVar("_T")


class Ok(BaseModel, Generic[_T]):
    """成功响应 envelope。"""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(alias="requestId")
    data: _T


# ---------------------------------------------------------------- 健康/信息/能力


class HealthData(BaseModel):
    status: Literal["UP"]


class ReadyData(BaseModel):
    status: Literal["READY", "DEGRADED", "NOT_READY"]


class InfoData(BaseModel):
    provider_type: str = Field(alias="providerType")
    provider_instance_code: str = Field(alias="providerInstanceCode")
    contract_version: str = Field(alias="contractVersion")
    implementation_version: str = Field(alias="implementationVersion")
    build_commit: str = Field(alias="buildCommit")
    build_time: AwDatetime = Field(alias="buildTime")
    protocol_stack: str = Field(alias="protocolStack")
    record_types_supported: list[str] = Field(alias="recordTypesSupported", default=["ALL", "TIME"])
    auth_enabled: bool = Field(alias="authEnabled")


class CapabilityItem(BaseModel):
    code: str
    supported: bool
    constraints: dict[str, Any] = Field(default_factory=dict)


class CapabilitiesData(BaseModel):
    capabilities: list[CapabilityItem]


# ---------------------------------------------------------------- 设备与通道


class ProviderDevice(BaseModel):
    provider_instance_code: str = Field(alias="providerInstanceCode")
    external_device_id: str = Field(alias="externalDeviceId")
    source_name: str = Field(alias="sourceName")
    manufacturer: str
    model: str
    firmware_version: str = Field(alias="firmwareVersion")
    transport: str
    stream_mode: str = Field(alias="streamMode")
    charset: str
    online_status: OnlineStatus = Field(alias="onlineStatus")
    channel_count: int = Field(alias="channelCount")
    last_seen_at: AwDatetime | None = Field(alias="lastSeenAt")
    revision: str


class ProviderChannel(BaseModel):
    provider_instance_code: str = Field(alias="providerInstanceCode")
    external_device_id: str = Field(alias="externalDeviceId")
    external_channel_id: str = Field(alias="externalChannelId")
    parent_external_channel_id: str | None = Field(alias="parentExternalChannelId")
    source_name: str = Field(alias="sourceName")
    manufacturer: str
    model: str
    online_status: OnlineStatus = Field(alias="onlineStatus")
    resolution: str
    codec: str
    has_audio: bool = Field(alias="hasAudio")
    supports_ptz: bool = Field(alias="supportsPtz")
    supports_device_record: bool = Field(alias="supportsDeviceRecord")
    stream_identification: str | None = Field(alias="streamIdentification")
    revision: str


class DevicePage(BaseModel):
    items: list[ProviderDevice]
    page: int
    page_size: int = Field(alias="pageSize")
    total: int


class ChannelPage(BaseModel):
    items: list[ProviderChannel]
    page: int
    page_size: int = Field(alias="pageSize")
    total: int


class DeviceStatusData(BaseModel):
    external_device_id: str = Field(alias="externalDeviceId")
    online_status: OnlineStatus = Field(alias="onlineStatus")
    last_seen_at: AwDatetime | None = Field(alias="lastSeenAt")
    observed_at: AwDatetime = Field(alias="observedAt")
    revision: str


# ---------------------------------------------------------------- Catalog 同步


class CatalogSyncAccepted(BaseModel):
    operation_id: str = Field(alias="operationId")
    status: Literal["ACCEPTED"]
    submitted_at: AwDatetime = Field(alias="submittedAt")


class CatalogSyncResult(BaseModel):
    operation_id: str = Field(alias="operationId")
    external_device_id: str = Field(alias="externalDeviceId")
    status: CatalogOpStatus
    discovered_count: int | None = Field(alias="discoveredCount")
    submitted_at: AwDatetime = Field(alias="submittedAt")
    completed_at: AwDatetime | None = Field(alias="completedAt")
    error: dict[str, Any] | None = None


# ---------------------------------------------------------------- 实时流 / 回放流


class MediaReference(BaseModel):
    media_server_id: str = Field(alias="mediaServerId")
    vhost: str
    app: str
    stream_id: str = Field(alias="streamId")
    codec: str
    has_audio: bool = Field(alias="hasAudio")


class StreamSource(BaseModel):
    protocol: str
    url: str
    priority: int


class StreamView(BaseModel):
    provider_stream_key: str = Field(alias="providerStreamKey")
    stream_type: Literal["LIVE", "PLAYBACK"] = Field(alias="streamType")
    external_device_id: str = Field(alias="externalDeviceId")
    external_channel_id: str = Field(alias="externalChannelId")
    stream_profile: StreamProfile | None = Field(alias="streamProfile", default=None)
    state: StreamState
    media: MediaReference | None = None
    sources: list[StreamSource] = Field(default_factory=list)
    started_at: AwDatetime = Field(alias="startedAt")
    stopped_at: AwDatetime | None = Field(alias="stoppedAt")


class LiveStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_device_id: str = Field(alias="externalDeviceId", min_length=1)
    external_channel_id: str = Field(alias="externalChannelId", min_length=1)
    stream_profile: StreamProfile = Field(alias="streamProfile", default="AUTO")


class PlaybackStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_device_id: str = Field(alias="externalDeviceId", min_length=1)
    external_channel_id: str = Field(alias="externalChannelId", min_length=1)
    record_key: str = Field(alias="recordKey", min_length=1)
    start_time: AwDatetime | None = Field(alias="startTime", default=None)
    end_time: AwDatetime | None = Field(alias="endTime", default=None)

    @model_validator(mode="after")
    def _check_both_times(self) -> PlaybackStartRequest:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("startTime 与 endTime 必须同时提供或同时省略")
        if self.start_time is not None and self.end_time is not None:
            if self.end_time <= self.start_time:
                raise ValueError("endTime 必须晚于 startTime")
        return self


# ---------------------------------------------------------------- 设备录像查询

MAX_RECORD_QUERY_SPAN = timedelta(days=31)


class RecordQueryRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_device_id: str = Field(alias="externalDeviceId", min_length=1)
    external_channel_id: str = Field(alias="externalChannelId", min_length=1)
    start_time: AwDatetime = Field(alias="startTime")
    end_time: AwDatetime = Field(alias="endTime")
    record_type: RecordType = Field(alias="recordType", default="ALL")

    @model_validator(mode="after")
    def _validate_range(self) -> RecordQueryRequest:
        if self.end_time <= self.start_time:
            raise ValueError("endTime 必须晚于 startTime")
        span = self.end_time - self.start_time
        if span > MAX_RECORD_QUERY_SPAN:
            raise ValueError(f"查询区间不能超过 {MAX_RECORD_QUERY_SPAN.days} 天")
        return self


class RecordQueryAccepted(BaseModel):
    query_id: str = Field(alias="queryId")
    status: Literal["ACCEPTED"]
    submitted_at: AwDatetime = Field(alias="submittedAt")


class RecordItem(BaseModel):
    record_key: str = Field(alias="recordKey")
    external_device_id: str = Field(alias="externalDeviceId")
    external_channel_id: str = Field(alias="externalChannelId")
    start_time: AwDatetime = Field(alias="startTime")
    end_time: AwDatetime = Field(alias="endTime")
    record_type: Literal["TIME"] = Field(alias="recordType")
    size_bytes: int | None = Field(alias="sizeBytes")
    source_address: str | None = Field(alias="sourceAddress")


class RecordQueryResult(BaseModel):
    query_id: str = Field(alias="queryId")
    status: RecordQueryStatus
    items: list[RecordItem]
    submitted_at: AwDatetime = Field(alias="submittedAt")
    completed_at: AwDatetime | None = Field(alias="completedAt")
    error: dict[str, Any] | None = None
