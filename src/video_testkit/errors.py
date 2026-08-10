"""共同契约稳定错误码。

调用方只依赖 ``code``、HTTP 状态与 ``retryable``。
诊断子码以 ``VIDEO_*_NOT_FOUND`` 形式给出，业务判断仍应使用稳定码。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VIDEO_INVALID_ARGUMENT = "VIDEO_INVALID_ARGUMENT"
    VIDEO_PROVIDER_AUTH_FAILED = "VIDEO_PROVIDER_AUTH_FAILED"
    VIDEO_IDEMPOTENCY_CONFLICT = "VIDEO_IDEMPOTENCY_CONFLICT"
    VIDEO_PROVIDER_UNAVAILABLE = "VIDEO_PROVIDER_UNAVAILABLE"
    VIDEO_PROVIDER_TIMEOUT = "VIDEO_PROVIDER_TIMEOUT"
    VIDEO_DEVICE_NOT_FOUND = "VIDEO_DEVICE_NOT_FOUND"
    VIDEO_DEVICE_OFFLINE = "VIDEO_DEVICE_OFFLINE"
    VIDEO_CHANNEL_NOT_FOUND = "VIDEO_CHANNEL_NOT_FOUND"
    VIDEO_CATALOG_TIMEOUT = "VIDEO_CATALOG_TIMEOUT"
    VIDEO_STREAM_START_FAILED = "VIDEO_STREAM_START_FAILED"
    VIDEO_STREAM_STOP_FAILED = "VIDEO_STREAM_STOP_FAILED"
    VIDEO_STREAM_NOT_FOUND = "VIDEO_STREAM_NOT_FOUND"
    VIDEO_MEDIA_UNAVAILABLE = "VIDEO_MEDIA_UNAVAILABLE"
    VIDEO_UNSUPPORTED_CODEC = "VIDEO_UNSUPPORTED_CODEC"
    VIDEO_RECORD_QUERY_TIMEOUT = "VIDEO_RECORD_QUERY_TIMEOUT"
    VIDEO_RECORD_NOT_FOUND = "VIDEO_RECORD_NOT_FOUND"
    VIDEO_RECORD_MISMATCH = "VIDEO_RECORD_MISMATCH"
    VIDEO_PLAYBACK_START_FAILED = "VIDEO_PLAYBACK_START_FAILED"
    VIDEO_CAPABILITY_NOT_SUPPORTED = "VIDEO_CAPABILITY_NOT_SUPPORTED"
    # 诊断子码（Provider 私有扩展，不属于稳定码表）
    VIDEO_OPERATION_NOT_FOUND = "VIDEO_OPERATION_NOT_FOUND"
    VIDEO_QUERY_NOT_FOUND = "VIDEO_QUERY_NOT_FOUND"


# (http_status, retryable)
_DEFAULT_MAP: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.VIDEO_INVALID_ARGUMENT: (400, False),
    ErrorCode.VIDEO_PROVIDER_AUTH_FAILED: (401, False),
    ErrorCode.VIDEO_IDEMPOTENCY_CONFLICT: (409, False),
    ErrorCode.VIDEO_PROVIDER_UNAVAILABLE: (503, True),
    ErrorCode.VIDEO_PROVIDER_TIMEOUT: (504, True),
    ErrorCode.VIDEO_DEVICE_NOT_FOUND: (404, False),
    ErrorCode.VIDEO_DEVICE_OFFLINE: (422, False),
    ErrorCode.VIDEO_CHANNEL_NOT_FOUND: (404, False),
    ErrorCode.VIDEO_CATALOG_TIMEOUT: (504, True),
    ErrorCode.VIDEO_STREAM_START_FAILED: (502, False),
    ErrorCode.VIDEO_STREAM_STOP_FAILED: (502, True),
    ErrorCode.VIDEO_STREAM_NOT_FOUND: (404, False),
    ErrorCode.VIDEO_MEDIA_UNAVAILABLE: (503, True),
    ErrorCode.VIDEO_UNSUPPORTED_CODEC: (422, False),
    ErrorCode.VIDEO_RECORD_QUERY_TIMEOUT: (504, True),
    ErrorCode.VIDEO_RECORD_NOT_FOUND: (404, False),
    ErrorCode.VIDEO_RECORD_MISMATCH: (409, False),
    ErrorCode.VIDEO_PLAYBACK_START_FAILED: (502, False),
    ErrorCode.VIDEO_CAPABILITY_NOT_SUPPORTED: (422, False),
    ErrorCode.VIDEO_OPERATION_NOT_FOUND: (404, False),
    ErrorCode.VIDEO_QUERY_NOT_FOUND: (404, False),
}


@dataclass
class ProviderError(Exception):
    """可序列化为共同契约错误 envelope 的异常。"""

    code: ErrorCode
    message: str
    details: Mapping[str, Any] | None = None
    http_status: int | None = None
    retryable: bool | None = None

    def __post_init__(self) -> None:
        status, retry = _DEFAULT_MAP[self.code]
        self.http_status = self.http_status if self.http_status is not None else status
        self.retryable = self.retryable if self.retryable is not None else retry
        super().__init__(f"{self.code.value}: {self.message}")


def invalid_argument(message: str, details: Mapping[str, Any] | None = None) -> ProviderError:
    return ProviderError(ErrorCode.VIDEO_INVALID_ARGUMENT, message, details=details)


def provider_error(
    code: ErrorCode, message: str, details: Mapping[str, Any] | None = None
) -> ProviderError:
    return ProviderError(code, message, details=details)


@dataclass
class ErrorEnvelope:
    request_id: str
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            },
        }
