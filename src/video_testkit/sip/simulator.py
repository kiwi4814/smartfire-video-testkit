"""GB28181 Device Simulator：通过真实 UDP 完成 REGISTER -> 401 -> Authorization -> 200。

有界超时；每次触发后状态与最后错误可通过控制面查询。本切片不做 Catalog/RTP。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from video_testkit.config import Settings
from video_testkit.sip.digest import (
    build_authorization_header,
    generate_cnonce,
    parse_params,
)
from video_testkit.sip.message import SipMessage, build_message, parse_message

logger = logging.getLogger(__name__)

USER_AGENT = "SmartFire-TestKit-GB-Simulator/0.1.0"


@dataclass
class SimulatorDeviceState:
    registered: bool = False
    # IDLE / REGISTERING / REGISTERED / UNREGISTERING / UNREGISTERED / REGISTER_FAILED
    status: str = "IDLE"
    registered_at: datetime | None = None
    expires_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    attempt_count: int = 0
    # 最近一次成功注册使用的身份（username + contact），用于验证身份确定性。
    last_identity: str | None = None


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.queue: asyncio.Queue[tuple[bytes, Any]] = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.queue.put_nowait((data, addr))


class SimulatorError(Exception):
    """注册流程中出现的可预期错误（非超时）。"""


class DeviceSimulator:
    """为场景中的每台设备维护 SIP 注册状态，并按需触发 REGISTER/UNREGISTER 流程。"""

    def __init__(
        self,
        settings: Settings,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._devices: dict[str, SimulatorDeviceState] = {}
        self._known_ids: set[str] = set()
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._stop_event: asyncio.Event | None = None
        self._maintenance_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        self._devices.clear()
        self._known_ids.clear()

    def start_maintenance(self) -> None:
        """启动注册维护循环：在 expiry 前自动刷新，避免注册过期。幂等。"""
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        self._stop_event = asyncio.Event()
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop_maintenance(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._maintenance_task
        self._maintenance_task = None
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _maintenance_loop(self) -> None:
        assert self._stop_event is not None
        margin = self._settings.gb_refresh_margin
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.2)
                return
            except TimeoutError:
                pass
            now = self._now_fn()
            for device_id, state in list(self._devices.items()):
                if not state.registered or state.expires_at is None:
                    continue
                remaining = (state.expires_at - now).total_seconds()
                if remaining <= margin:
                    try:
                        await self.trigger_register(device_id)
                    except Exception:
                        logger.exception("自动刷新注册失败", extra={"deviceId": device_id})

    def set_known_device(self, device_id: str) -> None:
        self._known_ids.add(device_id)

    def status(self, device_id: str) -> dict[str, Any] | None:
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        return self._view(device_id, state)

    def statuses(self) -> dict[str, dict[str, Any]]:
        return {did: self._view(did, st) for did, st in sorted(self._devices.items())}

    def _view(self, device_id: str, state: SimulatorDeviceState) -> dict[str, Any]:
        return {
            "externalDeviceId": device_id,
            "registered": state.registered,
            "status": state.status,
            "registeredAt": (
                state.registered_at.isoformat().replace("+00:00", "Z")
                if state.registered_at
                else None
            ),
            "expiresAt": (
                state.expires_at.isoformat().replace("+00:00", "Z") if state.expires_at else None
            ),
            "lastAttemptAt": (
                state.last_attempt_at.isoformat().replace("+00:00", "Z")
                if state.last_attempt_at
                else None
            ),
            "lastError": state.last_error,
            "attemptCount": state.attempt_count,
            "lastIdentity": state.last_identity,
        }

    # ------------------------------------------------------------ 触发注册

    async def trigger_register(self, device_id: str) -> dict[str, Any]:
        if device_id not in self._known_ids:
            raise KeyError(f"未知设备: {device_id}")
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        state.last_error = None
        state.registered = False
        state.status = "REGISTERING"
        state.last_attempt_at = self._now_fn()
        state.attempt_count += 1

        timeout = self._settings.gb_register_timeout
        reg_host, reg_port = self._split_addr(self._settings.effective_gb_registrar_addr)
        uri = f"sip:{device_id}@{reg_host}:{reg_port}"
        try:
            await self._register_flow(device_id, uri, (reg_host, reg_port), timeout, state)
        except (TimeoutError, SimulatorError, ValueError) as exc:
            state.status = "REGISTER_FAILED"
            state.last_error = str(exc)
        except OSError as exc:
            state.status = "REGISTER_FAILED"
            state.last_error = f"UDP 发送失败: {exc}"
        logger.info(
            "simulator register result",
            extra={"deviceId": device_id, "status": state.status},
        )
        return self._view(device_id, state)

    async def trigger_unregister(self, device_id: str) -> dict[str, Any]:
        if device_id not in self._known_ids:
            raise KeyError(f"未知设备: {device_id}")
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        state.last_error = None
        state.status = "UNREGISTERING"
        state.last_attempt_at = self._now_fn()
        state.attempt_count += 1

        timeout = self._settings.gb_register_timeout
        reg_host, reg_port = self._split_addr(self._settings.effective_gb_registrar_addr)
        uri = f"sip:{device_id}@{reg_host}:{reg_port}"
        try:
            await self._unregister_flow(device_id, uri, (reg_host, reg_port), timeout, state)
        except (TimeoutError, SimulatorError, ValueError) as exc:
            state.status = "REGISTER_FAILED"
            state.last_error = str(exc)
        except OSError as exc:
            state.status = "REGISTER_FAILED"
            state.last_error = f"UDP 发送失败: {exc}"
        logger.info(
            "simulator unregister result",
            extra={"deviceId": device_id, "status": state.status},
        )
        return self._view(device_id, state)

    async def _register_flow(
        self,
        device_id: str,
        uri: str,
        target: tuple[str, int],
        timeout: float,
        state: SimulatorDeviceState,
    ) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _ClientProtocol, local_addr=("127.0.0.1", 0)
        )
        try:
            local_port = transport.get_extra_info("sockname")[1]
            call_id = uuid.uuid4().hex
            from_tag = uuid.uuid4().hex[:12]

            # 1. 无 Authorization 的 REGISTER
            branch1 = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg1 = build_message(
                f"REGISTER {uri} SIP/2.0",
                self._base_headers(branch1, device_id, uri, call_id, from_tag, 1, local_port),
            )
            transport.sendto(msg1, target)
            resp1 = await self._await_response(protocol, branch1, timeout)
            if resp1.status_code() != 401:
                raise SimulatorError(f"首次 REGISTER 预期 401，实际 {resp1.status_code()}")

            challenge = resp1.header("www-authenticate") or ""
            params = parse_params(challenge)
            realm = params.get("realm", self._settings.gb_realm)
            nonce = params.get("nonce", "")
            if not nonce:
                raise SimulatorError("401 响应缺少 nonce")

            # 2. 携带 Digest 的 REGISTER；若返回 stale=true 的 401，用新 nonce 重试。
            cseq = 2
            cnonce = generate_cnonce()
            for _attempt in range(3):
                auth = build_authorization_header(
                    username=device_id,
                    realm=realm,
                    nonce=nonce,
                    uri=uri,
                    method="REGISTER",
                    password=self._settings.gb_password,
                    cnonce=cnonce,
                )
                branch = "z9hG4bK" + uuid.uuid4().hex[:20]
                msg = build_message(
                    f"REGISTER {uri} SIP/2.0",
                    self._base_headers(branch, device_id, uri, call_id, from_tag, cseq, local_port)
                    + [("Authorization", auth)],
                )
                transport.sendto(msg, target)
                resp = await self._await_response(protocol, branch, timeout)
                code = resp.status_code()
                if code == 200:
                    break
                if code == 401:
                    retry_params = parse_params(resp.header("www-authenticate") or "")
                    if retry_params.get("stale", "").lower() == "true" and retry_params.get(
                        "nonce"
                    ):
                        nonce = retry_params["nonce"]
                        cnonce = generate_cnonce()
                        cseq += 1
                        continue
                    raise SimulatorError("鉴权 REGISTER 预期 200，实际 401")
                raise SimulatorError(f"鉴权 REGISTER 预期 200，实际 {code}")
            else:
                raise SimulatorError("多次 stale 重试后仍未完成注册")

            now = self._now_fn()
            state.registered = True
            state.status = "REGISTERED"
            state.registered_at = now
            state.expires_at = now + timedelta(seconds=self._settings.gb_expires)
            state.last_identity = f"{device_id}@{uri}"
        finally:
            transport.close()

    async def _unregister_flow(
        self,
        device_id: str,
        uri: str,
        target: tuple[str, int],
        timeout: float,
        state: SimulatorDeviceState,
    ) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _ClientProtocol, local_addr=("127.0.0.1", 0)
        )
        try:
            local_port = transport.get_extra_info("sockname")[1]
            call_id = uuid.uuid4().hex
            from_tag = uuid.uuid4().hex[:12]

            # 1. 无 Authorization 的 REGISTER（Expires: 0）
            branch1 = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg1 = build_message(
                f"REGISTER {uri} SIP/2.0",
                self._base_headers(
                    branch1, device_id, uri, call_id, from_tag, 1, local_port, expires=0
                ),
            )
            transport.sendto(msg1, target)
            resp1 = await self._await_response(protocol, branch1, timeout)
            if resp1.status_code() != 401:
                raise SimulatorError(
                    f"unregister 首次 REGISTER 预期 401，实际 {resp1.status_code()}"
                )

            # 2. 携带 Digest 的 REGISTER（Expires: 0）
            challenge = resp1.header("www-authenticate") or ""
            params = parse_params(challenge)
            realm = params.get("realm", self._settings.gb_realm)
            nonce = params.get("nonce", "")
            if not nonce:
                raise SimulatorError("401 响应缺少 nonce")
            cnonce = generate_cnonce()
            auth = build_authorization_header(
                username=device_id,
                realm=realm,
                nonce=nonce,
                uri=uri,
                method="REGISTER",
                password=self._settings.gb_password,
                cnonce=cnonce,
            )
            branch2 = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg2 = build_message(
                f"REGISTER {uri} SIP/2.0",
                self._base_headers(
                    branch2, device_id, uri, call_id, from_tag, 2, local_port, expires=0
                )
                + [("Authorization", auth)],
            )
            transport.sendto(msg2, target)
            resp2 = await self._await_response(protocol, branch2, timeout)
            if resp2.status_code() != 200:
                raise SimulatorError(
                    f"unregister 鉴权 REGISTER 预期 200，实际 {resp2.status_code()}"
                )

            state.registered = False
            state.status = "UNREGISTERED"
            state.registered_at = None
            state.expires_at = None
        finally:
            transport.close()

    @staticmethod
    async def _await_response(protocol: _ClientProtocol, branch: str, timeout: float) -> SipMessage:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"等待 SIP 响应超时（{timeout:.1f}s）")
            try:
                data, _addr = await asyncio.wait_for(protocol.queue.get(), timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError(f"等待 SIP 响应超时（{timeout:.1f}s）") from exc
            try:
                resp = parse_message(data)
            except ValueError:
                continue
            via = resp.header("via") or ""
            if branch in via:
                return resp

    def _base_headers(
        self,
        branch: str,
        device_id: str,
        uri: str,
        call_id: str,
        from_tag: str,
        cseq: int,
        local_port: int,
        expires: int | None = None,
    ) -> list[tuple[str, str]]:
        expires_value = self._settings.gb_expires if expires is None else expires
        return [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport"),
            ("From", f"<{uri}>;tag={from_tag}"),
            ("To", f"<{uri}>"),
            ("Call-ID", call_id),
            ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:{device_id}@127.0.0.1:{local_port}>"),
            ("Max-Forwards", "70"),
            ("Expires", str(expires_value)),
            ("User-Agent", USER_AGENT),
        ]

    @staticmethod
    def _split_addr(addr: str) -> tuple[str, int]:
        host_part, _, port_part = addr.rpartition(":")
        if not port_part.isdigit():
            raise ValueError(f"无效地址: {addr!r}")
        return host_part, int(port_part)
