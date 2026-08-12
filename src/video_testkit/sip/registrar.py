"""内置 Fake SIP Registrar：真实 UDP 监听，完成 REGISTER 的 401 Digest 挑战与 200 确认。

不依赖 WVP / Gateway / ZLM；请求日志与注册表通过控制面可查。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from video_testkit.logging_conf import utc_z_now
from video_testkit.sip.catalog import (
    PROVIDER_SIP_ID,
    CatalogItemData,
    CatalogQueryError,
    CatalogQueryResult,
    build_catalog_query_xml,
    parse_catalog_response,
)
from video_testkit.sip.digest import compute_response, generate_nonce, parse_params
from video_testkit.sip.keepalive import CONTENT_TYPE, parse_keepalive_xml
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


class _CatalogSession:
    """一次进行中的 Catalog 查询会话：聚合响应直到收满或收尾窗口到期。"""

    def __init__(
        self,
        device_id: str,
        query_sn: int,
        settle_window: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.device_id = device_id
        self.query_sn = query_sn
        self.settle_window = settle_window
        self.items: dict[str, CatalogItemData] = {}
        self.sum_num: int | None = None
        self.complete = False
        self.future: asyncio.Future[CatalogQueryResult] = loop.create_future()
        self.settle_timer: asyncio.TimerHandle | None = None


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
        on_keepalive: Callable[[str, int], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._realm = realm
        self._password = password
        self._log_limit = log_limit
        self._nonce_ttl = nonce_ttl
        # Keepalive 回调：收到有效 MESSAGE 心跳时调用 (device_id, sn)。
        self._on_keepalive = on_keepalive
        self._transport: asyncio.DatagramTransport | None = None
        self._nonces: dict[str, datetime] = {}
        self._requests_log: deque[dict[str, Any]] = deque(maxlen=log_limit)
        self._registrations: dict[str, dict[str, Any]] = {}
        # Provider 侧 Catalog 查询会话（每设备最多一个进行中查询）。
        self._catalog_sessions: dict[str, _CatalogSession] = {}
        self._catalog_query_seq: dict[str, int] = {}

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
        for session in self._catalog_sessions.values():
            if session.settle_timer is not None:
                session.settle_timer.cancel()
            if not session.future.done():
                session.future.cancel()
        self._catalog_sessions.clear()
        self._catalog_query_seq.clear()

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
            "contentType": msg.header("content-type"),
            "authUsername": None,
            "authorized": False,
            "status": None,
        }
        method = msg.method()
        if method == "MESSAGE":
            self._handle_message(msg, entry, transport, addr)
            return
        if method != "REGISTER":
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

    def _handle_message(
        self,
        msg: SipMessage,
        entry: dict[str, Any],
        transport: asyncio.DatagramTransport,
        addr: Any,
    ) -> None:
        """分派 SIP MESSAGE：优先按 Catalog 响应处理，否则按 Keepalive。"""
        if self._handle_catalog_response(msg):
            entry["status"] = 200
            entry["authorized"] = False
            entry["stale"] = False
            self._requests_log.append(entry)
            transport.sendto(self._echo(msg, "SIP/2.0 200 OK"), addr)
            return
        self._handle_keepalive(msg, entry, transport, addr)

    # ------------------------------------------------------------ Catalog 查询

    async def query_catalog(
        self,
        device_id: str,
        target: tuple[str, int],
        timeout: float,
        settle_window: float,
    ) -> CatalogQueryResult:
        """向设备发送 Catalog 查询（真实 UDP MESSAGE）并聚合响应。

        收满 ``SumNum`` 立即返回；已收部分后经过 ``settle_window`` 无新响应
        返回 PARTIAL 结果；完全无响应时抛 ``CatalogQueryError``。
        """
        if self._transport is None:
            raise CatalogQueryError("Registrar 未启动")
        loop = asyncio.get_running_loop()
        self._catalog_query_seq[device_id] = self._catalog_query_seq.get(device_id, 0) + 1
        session = _CatalogSession(
            device_id, self._catalog_query_seq[device_id], settle_window, loop
        )
        self._catalog_sessions[device_id] = session
        try:
            self._send_catalog_query(device_id, target, session.query_sn)
            return await asyncio.wait_for(session.future, timeout=timeout)
        except TimeoutError:
            if session.items:
                # 有部分结果但未收满：按 PARTIAL 返回，保留有效项。
                return CatalogQueryResult(
                    device_id=device_id,
                    query_sn=session.query_sn,
                    items=list(session.items.values()),
                    sum_num=session.sum_num or 0,
                    complete=False,
                )
            raise CatalogQueryError(f"Catalog 查询超时且未收到任何响应: {device_id}") from None
        finally:
            if session.settle_timer is not None:
                session.settle_timer.cancel()
            self._catalog_sessions.pop(device_id, None)

    def _send_catalog_query(self, device_id: str, target: tuple[str, int], sn: int) -> None:
        assert self._transport is not None, "Registrar 未启动"
        host, port = target
        uri = f"sip:{device_id}@{host}:{port}"
        body = build_catalog_query_xml(device_id, sn)
        branch = "z9hG4bK" + uuid.uuid4().hex[:20]
        headers: list[tuple[str, str]] = [
            (
                "Via",
                f"SIP/2.0/UDP {self._host}:{self._port};branch={branch};rport",
            ),
            (
                "From",
                f"<sip:{PROVIDER_SIP_ID}@{self._host}:{self._port}>;tag={uuid.uuid4().hex[:12]}",
            ),
            ("To", f"<{uri}>"),
            ("Call-ID", uuid.uuid4().hex),
            ("CSeq", f"{sn + 1000} MESSAGE"),
            ("Max-Forwards", "70"),
            ("Content-Type", CONTENT_TYPE),
            ("User-Agent", "SmartFire-TestKit-Registrar/0.1.0"),
        ]
        self._transport.sendto(build_message(f"MESSAGE {uri} SIP/2.0", headers, body), target)

    def _handle_catalog_response(self, msg: SipMessage) -> bool:
        """解析 Catalog 响应 MESSAGE 并聚合到进行中的查询会话；非响应返回 False。"""
        if not msg.body_bytes:
            return False
        try:
            response = parse_catalog_response(msg.body_bytes)
        except ValueError:
            return False
        session = self._catalog_sessions.get(response.device_id)
        if session is None:
            return False
        for item in response.items:
            session.items[item.device_id] = item
        session.sum_num = response.sum_num
        if session.sum_num > 0 and len(session.items) >= session.sum_num:
            session.complete = True
            if not session.future.done():
                session.future.set_result(
                    CatalogQueryResult(
                        device_id=session.device_id,
                        query_sn=session.query_sn,
                        items=list(session.items.values()),
                        sum_num=session.sum_num,
                        complete=True,
                    )
                )
            return True
        # 未收满：重置收尾窗口；若收尾时仍不足则按 PARTIAL 返回。
        if session.settle_timer is not None:
            session.settle_timer.cancel()
        if not session.future.done():
            session.settle_timer = asyncio.get_running_loop().call_later(
                session.settle_window, self._settle_catalog_session, session
            )
        return True

    def _settle_catalog_session(self, session: _CatalogSession) -> None:
        if session.future.done() or not session.items:
            return  # 无有效项时等待总超时（避免把延迟响应误判为无响应）
        session.future.set_result(
            CatalogQueryResult(
                device_id=session.device_id,
                query_sn=session.query_sn,
                items=list(session.items.values()),
                sum_num=session.sum_num or 0,
                complete=False,
            )
        )

    def _handle_keepalive(
        self,
        msg: SipMessage,
        entry: dict[str, Any],
        transport: asyncio.DatagramTransport,
        addr: Any,
    ) -> None:
        """处理 SIP MESSAGE：解析 Keepalive XML 并通知上层（回调）。"""
        try:
            keepalive = parse_keepalive_xml(msg.body)
        except ValueError:
            entry["status"] = 400
            entry["authorized"] = False
            entry["stale"] = False
            self._requests_log.append(entry)
            logger.debug("registrar: 丢弃畸形 Keepalive 报文")
            transport.sendto(self._echo(msg, "SIP/2.0 400 Bad Request"), addr)
            return

        entry["status"] = 200
        entry["authorized"] = False
        entry["stale"] = False
        self._requests_log.append(entry)
        transport.sendto(self._echo(msg, "SIP/2.0 200 OK"), addr)

        if self._on_keepalive is not None:
            self._on_keepalive(keepalive.device_id, keepalive.sn)

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
