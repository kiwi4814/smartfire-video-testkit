"""幂等键存储。

写操作必须携带 ``Idempotency-Key``：相同 Key + 相同规范化请求返回同一结果；
相同 Key + 不同请求返回 409 ``VIDEO_IDEMPOTENCY_CONFLICT``。
记录保留 24 小时（覆盖"最大下游超时 + 调用方重试窗口"的基线建议）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_TTL = timedelta(hours=24)


class IdempotencyConflict(Exception):
    """相同 Idempotency-Key 被用于语义不同的请求。"""


class IdempotencyKeyMissing(Exception):
    """写操作缺少 Idempotency-Key。"""


@dataclass
class IdempotencyEntry:
    key: str
    fingerprint: str
    resource_ref: str  # 关联结果，例如 streamKey / operationId / queryId
    created_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) - self.created_at >= DEFAULT_TTL


def fingerprint_of(payload: dict[str, Any]) -> str:
    """规范化请求指纹：key 排序 + 稳定 JSON + SHA-256。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self._entries: dict[str, IdempotencyEntry] = {}
        self._ttl = ttl

    def put(self, key: str, fingerprint: str, resource_ref: str) -> IdempotencyEntry:
        entry = IdempotencyEntry(
            key=key,
            fingerprint=fingerprint,
            resource_ref=resource_ref,
            created_at=datetime.now(UTC),
        )
        self._entries[key] = entry
        return entry

    def get(self, key: str) -> IdempotencyEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if datetime.now(UTC) - entry.created_at >= self._ttl:
            self._entries.pop(key, None)
            return None
        return entry

    def resolve(self, key: str | None, fingerprint: str) -> tuple[IdempotencyEntry, bool]:
        """按 Key 解析幂等语义。

        返回 ``(entry, created)``；Key 已存在且指纹不同时抛
        :class:`IdempotencyConflict`。
        """
        if key is None:
            raise IdempotencyKeyMissing()
        existing = self.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflict()
            return existing, False
        entry = self.put(key, fingerprint, "")
        return entry, True

    def clear(self) -> None:
        self._entries.clear()
