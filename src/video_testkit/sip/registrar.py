"""内置 Fake SIP Registrar：真实 UDP 监听，完成 REGISTER 的 401 Digest 挑战与 200 确认。

不依赖 WVP / Gateway / ZLM；请求日志与注册表通过控制面可查。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from video_testkit.logging_conf import utc_z_now
from video_testkit.sip.digest import compute_response, generate_nonce, parse_params
from video_testkit.sip.message import SipMessage, build_message, parse_message

logger = logging.getLogger(__name__)

NONCE_TTL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


class _RegistrarProtocol(asyncio.DatagramProtocol):
    def __init__(self, registrar: SipRegistrar) -> None:
        self._registrar = registrar
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if self.transport is not None:
            self._registrar.handle_datagram(data, addr, self.transport)


class SipRegistrar:
    """UDP SIP 服务器：仅处理 REGISTER（401 Digest 挑战 -> 校验 -> 200）。"""

    def __init__(
        self,
        host: str,
        port: int,
        realm: str,
        password: str,
        log_limit: int = 500,
        nonce_ttl: timedelta = NONCE_TTL,
    ) -> None:
        self._host = host
        self._port = port
        self._realm = realm
        self._password = password
        self._log_limit = log_limit
        self._nonce_ttl = nonce_ttl
        self._transport: asyncio.DatagramTransport | None = None
        self._nonces: dict[str, datetime] = {}
        self._requests_log: deque[dict[str, Any]] = deque(maxlen=log_limit)
        self._registrations: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ 生命周期

    @property
    def listening(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        _, protocol = await loop.create_datagram_endpoint(
            lambda: _RegistrarProtocol(self), local_addr=(self._host, self._port)
        )
        self._transport = protocol.transport

    async def stop(self) -> None:
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()
            await asyncio.sleep(0)
        self._transport = None

    def reset(self) -> None:
        self._nonces.clear()
        self._requests_log.clear()
        self._registrations.clear()

    # ------------------------------------------------------------ 查询（控制面）

    def requests_log(self) -> list[dict[str, Any]]:
        return list(self._requests_log)

    def registrations(self) -> list[dict[str, Any]]:
        return list(self._registrations.values())

    # ------------------------------------------------------------ 处理

    def handle_datagram(
        self,
        data: bytes,
        addr: Any,
        transport: asyncio.DatagramTransport,
    ) -> None:
        try:
            msg = parse_message(data)
        except ValueError:
            logger.debug("registrar: 丢弃无法解析的 UDP 报文")
            return

        entry: dict[str, Any] = {
            "receivedAt": utc_z_now(),
            "method": msg.method(),
            "requestUri": msg.request_uri,
            "callId": msg.header("call-id"),
            "cseq": msg.header("cseq"),
            "fromUri": msg.header("from"),
            "toUri": msg.header("to"),
            "userAgent": msg.header("user-agent"),
            "sourceAddress": f"{addr[0]}:{addr[1]}",
            "authUsername": None,
            "authorized": False,
            "status": None,
        }
        if msg.method() != "REGISTER":
            entry["status"] = 405
            self._requests_log.append(entry)
            transport.sendto(self._not_implemented(msg), addr)
            return

        auth = msg.header("authorization")
        if auth is None:
            nonce = generate_nonce()
            self._nonces[nonce] = _now() + self._nonce_ttl
            entry["status"] = 401
            entry["authorized"] = False
            entry["stale"] = False
            self._requests_log.append(entry)
            transport.sendto(self._challenge(msg, nonce, stale=False), addr)
            return

        self._prune_nonces()
        creds = parse_params(auth)
        username = creds.get("username", "")
        entry["authUsername"] = username
        nonce = creds.get("nonce", "")
        nonce_ok = nonce in self._nonces
        expected = compute_response(
            username=username,
            realm=self._realm,
            password=self._password,
            nonce=nonce,
            method="REGISTER",
            uri=creds.get("uri", msg.request_uri or ""),
            nc=creds.get("nc", "00000001"),
            cnonce=creds.get("cnonce"),
            qop=creds.get("qop", "auth"),
        )
        response_ok = secrets.compare_digest(creds.get("response", ""), expected)
        if nonce_ok and response_ok and username:
            expires = self._expires_of(msg)
            if expires == 0:
                # GB28181 unregister：Expires: 0 表示注销，从注册表移除。
                self._registrations.pop(username, None)
            else:
                self._registrations[username] = {
                    "username": username,
                    "contact": msg.header("contact"),
                    "expires": expires,
                    "receivedAt": utc_z_now(),
                    "sourceAddress": f"{addr[0]}:{addr[1]}",
                }
            entry["status"] = 200
            entry["authorized"] = True
            entry["stale"] = False
            self._requests_log.append(entry)
            transport.sendto(self._ok(msg, username), addr)
        elif not nonce_ok:
            # nonce 未知或已过期：stale=true，客户端应换新 nonce 重试。
            new_nonce = generate_nonce()
            self._nonces[new_nonce] = _now() + self._nonce_ttl
            entry["status"] = 401
            entry["authorized"] = False
            entry["stale"] = True
            self._requests_log.append(entry)
            transport.sendto(self._challenge(msg, new_nonce, stale=True), addr)
        else:
            # 凭据错误（如密码错误）：nonce 有效但响应不匹配，重试无意义。
            entry["status"] = 401
            entry["authorized"] = False
            entry["stale"] = False
            self._requests_log.append(entry)
            transport.sendto(self._challenge(msg, nonce, stale=False), addr)

    # ------------------------------------------------------------ 内部

    def _prune_nonces(self) -> None:
        now = _now()
        expired = [n for n, exp in self._nonces.items() if exp <= now]
        for n in expired:
            self._nonces.pop(n, None)

    @staticmethod
    def _expires_of(msg: SipMessage) -> int | None:
        header = msg.header("expires")
        if header and header.isdigit():
            return int(header)
        contact = msg.header("contact") or ""
        if ";expires=" in contact:
            raw = contact.split(";expires=", 1)[1].split(";", 1)[0].strip(">")
            if raw.isdigit():
                return int(raw)
        return None

    @staticmethod
    def _echo(
        msg: SipMessage,
        response_line: str,
        extra: list[tuple[str, str]] | None = None,
    ) -> bytes:
        headers: list[tuple[str, str]] = []
        for name in ("via", "from", "to", "call-id", "cseq"):
            value = msg.header(name)
            if value:
                headers.append((name, value))
        to_tag = f";tag={secrets.token_hex(8)}"
        for i, (name, value) in enumerate(headers):
            if name == "to" and ";tag=" not in value:
                headers[i] = (name, value + to_tag)
        if extra:
            headers.extend(extra)
        return build_message(response_line, headers)

    def _challenge(self, msg: SipMessage, nonce: str, stale: bool) -> bytes:
        ww = (
            f'Digest realm="{self._realm}", nonce="{nonce}", qop="auth", '
            f"algorithm=MD5, stale={'true' if stale else 'false'}"
        )
        return self._echo(msg, "SIP/2.0 401 Unauthorized", [("WWW-Authenticate", ww)])

    def _ok(self, msg: SipMessage, username: str) -> bytes:
        contact = msg.header("contact")
        headers: list[tuple[str, str]] = []
        if contact:
            headers.append(("Contact", contact))
        return self._echo(msg, "SIP/2.0 200 OK", headers)

    def _not_implemented(self, msg: SipMessage) -> bytes:
        return self._echo(msg, "SIP/2.0 405 Method Not Allowed")
