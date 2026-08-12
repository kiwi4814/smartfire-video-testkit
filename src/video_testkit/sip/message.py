"""SIP 消息的最小解析/构建（本切片仅需 REGISTER/Keepalive/Catalog 事务所需字段）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SipMessage:
    start_line: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    # 原始 body 字节（解析时保留，供按 XML 声明编码解码的协议体使用）。
    body_bytes: bytes = b""

    @property
    def is_request(self) -> bool:
        return not self.start_line.startswith("SIP/2.0 ")

    def method(self) -> str | None:
        if not self.is_request:
            return None
        return self.start_line.split(" ", 1)[0]

    def status_code(self) -> int | None:
        if self.is_request:
            return None
        try:
            return int(self.start_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            return None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def request_uri(self) -> str | None:
        if not self.is_request:
            return None
        parts = self.start_line.split(" ", 2)
        return parts[1] if len(parts) >= 2 else None


def parse_message(data: bytes) -> SipMessage:
    text = data.decode("utf-8", errors="replace")
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        head, _, body = text.partition("\n\n")
        lines = head.split("\n")
    else:
        lines = head.split("\r\n")
    if not lines:
        raise ValueError("空 SIP 报文")
    start_line = lines[0].rstrip("\r")
    headers: dict[str, str] = {}
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if not line:
            continue
        if line[0] in (" ", "\t"):
            continue  # 折叠头字段：本切片不需要
        name, _, value = line.partition(":")
        if not name.strip():
            continue
        key = name.strip().lower()
        value = value.strip()
        if key in headers:
            headers[key] = f"{headers[key]}, {value}"
        else:
            headers[key] = value
    # 原始 body：head 是 str 视图，按字节数重新切取原始报文。
    raw_body = _raw_body(data)
    return SipMessage(start_line=start_line, headers=headers, body=body, body_bytes=raw_body)


def _raw_body(data: bytes) -> bytes:
    """从原始报文提取 body 字节（兼容 \\r\\n 与 \\n 行尾）。"""
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in data:
            return data.split(sep, 1)[1]
    return b""


def build_message(
    start_line: str,
    headers: list[tuple[str, str]],
    body: str = "",
    body_encoding: str = "utf-8",
) -> bytes:
    body_bytes = body.encode(body_encoding)
    lines = [start_line]
    for name, value in headers:
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body_bytes)}")
    head = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n"
    return head + body_bytes
