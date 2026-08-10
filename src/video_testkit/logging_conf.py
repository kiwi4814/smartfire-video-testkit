"""结构化日志：JSON 行、requestId 上下文、敏感字段脱敏。

约定：任何日志不得包含 Authorization、设备密码、Digest 响应哈希、token。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "authorization",
        "secret",
        "api_key",
        "apikey",
        "access_key",
        "x-auth-token",
    }
)


def redact(value: Any) -> Any:
    """递归脱敏：敏感键对应的值统一替换为 ``***``，其余结构原样返回。"""
    if isinstance(value, dict):
        masked = {k: "***" for k in value if str(k).lower() in _SENSITIVE_KEYS}
        rest = {k: redact(v) for k, v in value.items() if str(k).lower() not in _SENSITIVE_KEYS}
        return {**rest, **masked}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def utc_z_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": utc_z_now(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        request_id = request_id_var.get()
        if request_id:
            payload["requestId"] = request_id
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # 静默噪音来源
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_ctx(**fields: Any) -> dict[str, Any]:
    return fields
