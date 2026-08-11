"""GB28181 注册生命周期（真实 UDP + 控制面）：

1. 到期前自动刷新且不产生重复 Protocol Source Identity；
2. ``Expires: 0`` unregister 形成可观察离线状态；
3. Provider 重启后设备以同一身份确定性重注册；
4. stale nonce 挑战自动用新 nonce 重试；
5. wrong realm、重复响应、延迟响应均可复现；
6. reset 释放 socket、任务与运行态，轮询全部有界。
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from typing import Any

import httpx
from conftest import data_of, free_port, wait_until_value

from video_testkit.config import Settings
from video_testkit.sip.digest import build_authorization_header, generate_cnonce, parse_params
from video_testkit.sip.message import SipMessage, build_message, parse_message
from video_testkit.sip.registrar import SipRegistrar
from video_testkit.sip.simulator import DeviceSimulator

NVR = "34020000001320000001"
REALM = "3402000000"
PASSWORD = "12345678"


# ---------------------------------------------------------------- UDP 辅助


class _UdpProxy(asyncio.DatagramProtocol):
    """双向 UDP 代理：可注入响应延迟、重复响应、篡改首个 nonce（复现网络异常）。"""

    def __init__(
        self,
        target_addr: tuple[str, int],
        delay: float = 0.0,
        delay_target: bool = False,
        duplicate: bool = False,
        tamper_first_nonce: bool = False,
    ) -> None:
        self._target = target_addr
        self._delay = delay
        # True：延迟 client→target（请求）方向；False：延迟 target→client（响应）方向
        self._delay_target = delay_target
        self._duplicate = duplicate
        self._tamper_first_nonce = tamper_first_nonce
        self._tampered = False
        self._client_addr: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if addr == self._target:
            self._forward(data, self._client_addr, delay=not self._delay_target, from_target=True)
        else:
            self._client_addr = addr
            self._forward(data, self._target, delay=self._delay_target, from_target=False)

    def _forward(
        self,
        data: bytes,
        to_addr: tuple[str, int] | None,
        delay: bool,
        from_target: bool,
    ) -> None:
        if to_addr is None:
            return

        async def _send() -> None:
            if delay and self._delay > 0:
                await asyncio.sleep(self._delay)
            if self.transport is None:
                return
            out = data
            if from_target and self._tamper_first_nonce and not self._tampered:
                out = self._tamper_nonce(data)
                if out is not data:
                    self._tampered = True
            self.transport.sendto(out, to_addr)
            if from_target and self._duplicate:
                self.transport.sendto(out, to_addr)

        asyncio.create_task(_send())

    @staticmethod
    def _tamper_nonce(data: bytes) -> bytes:
        """将首个 401 挑战的 nonce 替换为无效值，迫使对端走 stale 重试。"""
        text = data.decode("utf-8", errors="replace")
        marker = 'nonce="'
        idx = text.find(marker)
        if idx == -1:
            return data
        end = text.find('"', idx + len(marker))
        if end == -1:
            return data
        return (text[: idx + len(marker)] + "f" * 32 + text[end:]).encode("utf-8")


class _ProbeProtocol(asyncio.DatagramProtocol):
    """一次性 UDP 探测客户端：发送报文并按 Via branch 匹配响应。"""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.queue.put_nowait(data)


async def _probe_request(
    addr: tuple[str, int],
    data: bytes,
    branch: str,
    timeout: float = 2.0,
) -> SipMessage:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _ProbeProtocol, local_addr=("127.0.0.1", 0)
    )
    try:
        transport.sendto(data, addr)
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"等待 SIP 响应超时（{timeout:.1f}s）")
            packet = await asyncio.wait_for(protocol.queue.get(), timeout=remaining)
            resp = parse_message(packet)
            if branch in (resp.header("via") or ""):
                return resp
    finally:
        transport.close()


def _probe_headers(
    uri: str,
    branch: str,
    device_id: str,
    local_port: int,
    cseq: int,
    expires: int = 3600,
) -> list[tuple[str, str]]:
    return [
        ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport"),
        ("From", f"<{uri}>;tag={uuid.uuid4().hex[:12]}"),
        ("To", f"<{uri}>"),
        ("Call-ID", uuid.uuid4().hex),
        ("CSeq", f"{cseq} REGISTER"),
        ("Contact", f"<sip:{device_id}@127.0.0.1:{local_port}>"),
        ("Max-Forwards", "70"),
        ("Expires", str(expires)),
    ]


async def _wait_async_attempts(
    simulator: DeviceSimulator,
    min_attempts: int,
    timeout: float = 6.0,
    extra: Any = None,
) -> dict[str, Any]:
    """异步轮询 Simulator 状态，等待 attemptCount 增长与可选额外条件（让维护循环有机会运行）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        state = simulator.status(NVR)
        if (
            state is not None
            and state["attemptCount"] >= min_attempts
            and (extra is None or extra(state))
        ):
            return state
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"等待 Simulator 达到 {min_attempts} 次尝试超时（{timeout:.1f}s）")
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------- 1. 到期自动刷新


async def test_refresh_before_expiry_single_identity() -> None:
    """注册到期前自动刷新；Registrar 注册表同一 username 只有一条，不产生重复身份。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        settings = Settings(
            gb_registrar_addr=f"127.0.0.1:{reg_port}",
            gb_expires=2,
            gb_refresh_margin=0.5,
            gb_register_timeout=2.0,
        )
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        simulator.start_maintenance()
        try:
            first = await simulator.trigger_register(NVR)
            assert first["status"] == "REGISTERED"
            assert first["attemptCount"] == 1

            # 维护循环在 expires(2s) - margin(0.5s) 后自动刷新
            def _refreshed(state: dict[str, Any]) -> bool:
                return state["attemptCount"] >= 2 and state["status"] == "REGISTERED"

            state = await _wait_async_attempts(simulator, min_attempts=2, extra=_refreshed)
            assert state["status"] == "REGISTERED"
            assert state["registered"] is True
            assert state["expiresAt"] is not None

            # 刷新不产生重复 Protocol Source Identity
            regs = [r for r in registrar.registrations() if r["username"] == NVR]
            assert len(regs) == 1
            assert regs[0]["expires"] == 2
        finally:
            await simulator.stop_maintenance()
    finally:
        await registrar.stop()


# ---------------------------------------------------------------- 2. unregister


def test_unregister_expires_zero_offline(client: httpx.Client) -> None:
    """``Expires: 0`` unregister 后：Simulator 离线、Registrar 移除身份、Provider 可观察离线。"""
    client.post("/testkit/v1/reset")
    client.post(f"/testkit/v1/devices/{NVR}/register")
    status = wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("REGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert status == "REGISTERED"

    resp = client.post(f"/testkit/v1/devices/{NVR}/unregister")
    assert data_of(resp) == {"externalDeviceId": NVR, "status": "UNREGISTERING"}

    status = wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("UNREGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert status == "UNREGISTERED"

    sim = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert sim["registered"] is False
    assert sim["expiresAt"] is None

    # Registrar 注册表移除该身份
    regs = data_of(client.get("/testkit/v1/sip/registrar/registrations"))["items"]
    assert not any(r["username"] == NVR for r in regs)

    # Provider 侧可观察离线状态
    prov = data_of(client.get(f"/provider/v1/devices/{NVR}/status"))
    assert prov["onlineStatus"] == "OFFLINE"


# ---------------------------------------------------------------- 3. Provider 重启重注册


def test_provider_restart_reregister_same_identity(client: httpx.Client) -> None:
    """Provider/TestKit 重启（reset）后，设备以同一身份确定性重注册且不产生重复身份。"""
    client.post("/testkit/v1/reset")
    client.post(f"/testkit/v1/devices/{NVR}/register")
    status = wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("REGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert status == "REGISTERED"
    identity_before = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["lastIdentity"]
    assert identity_before

    # 模拟进程重启：reset 清空 Simulator/Registrar/Store 运行态
    client.post("/testkit/v1/reset")
    sim = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert sim["status"] == "IDLE"
    assert sim["registered"] is False
    assert sim["attemptCount"] == 0

    client.post(f"/testkit/v1/devices/{NVR}/register")
    status = wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("REGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert status == "REGISTERED"
    identity_after = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["lastIdentity"]
    assert identity_after == identity_before

    regs = data_of(client.get("/testkit/v1/sip/registrar/registrations"))["items"]
    same = [r for r in regs if r["username"] == NVR]
    assert len(same) == 1


# ---------------------------------------------------------------- 4. stale nonce 重试


async def test_stale_nonce_auto_retry() -> None:
    """首个 nonce 失效时，Simulator 自动使用新 nonce 重试并最终注册成功。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        proxy_port = free_port(socket.SOCK_DGRAM)
        proxy = _UdpProxy(target_addr=("127.0.0.1", reg_port), tamper_first_nonce=True)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: proxy, local_addr=("127.0.0.1", proxy_port))

        settings = Settings(gb_registrar_addr=f"127.0.0.1:{proxy_port}", gb_register_timeout=3.0)
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        result = await simulator.trigger_register(NVR)
        assert result["status"] == "REGISTERED"
        assert result["registered"] is True

        entries = registrar.requests_log()
        assert any(e.get("stale") is True for e in entries), "缺少 stale=true 挑战"
        assert any(e["status"] == 200 and e["authorized"] for e in entries)
    finally:
        await registrar.stop()


# ---------------------------------------------------------------- 5. wrong realm


async def test_wrong_realm_rejected() -> None:
    """使用错误 realm 计算 Digest 响应会被 Registrar 拒绝（401 且不登记身份）。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        addr = ("127.0.0.1", reg_port)
        uri = f"sip:{NVR}@127.0.0.1:{reg_port}"
        local_port = 51999  # 仅用于构造 Via/Contact，探测端实际端口由协议层决定

        # 1. 无 Authorization 的 REGISTER → 401 挑战
        branch1 = "z9hG4bK" + uuid.uuid4().hex[:20]
        msg1 = build_message(
            f"REGISTER {uri} SIP/2.0",
            _probe_headers(uri, branch1, NVR, local_port, 1),
        )
        resp1 = await _probe_request(addr, msg1, branch1)
        assert resp1.status_code() == 401
        params = parse_params(resp1.header("www-authenticate") or "")
        nonce = params["nonce"]
        assert nonce

        # 2. 用错误 realm 计算 Digest → 401 stale，且不登记身份
        wrong_realm = "WRONG-REALM"
        cnonce = generate_cnonce()
        auth = build_authorization_header(
            username=NVR,
            realm=wrong_realm,
            nonce=nonce,
            uri=uri,
            method="REGISTER",
            password=PASSWORD,
            cnonce=cnonce,
        )
        branch2 = "z9hG4bK" + uuid.uuid4().hex[:20]
        msg2 = build_message(
            f"REGISTER {uri} SIP/2.0",
            _probe_headers(uri, branch2, NVR, local_port, 2) + [("Authorization", auth)],
        )
        resp2 = await _probe_request(addr, msg2, branch2)
        assert resp2.status_code() == 401
        assert not registrar.registrations(), "wrong realm 不应产生注册身份"
    finally:
        await registrar.stop()


# ---------------------------------------------------------------- 6. 重复响应


async def test_duplicate_response_ignored() -> None:
    """UDP 重复响应（每包复制一份）不影响注册结果，且只登记一个身份。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        proxy_port = free_port(socket.SOCK_DGRAM)
        proxy = _UdpProxy(target_addr=("127.0.0.1", reg_port), duplicate=True)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: proxy, local_addr=("127.0.0.1", proxy_port))

        settings = Settings(gb_registrar_addr=f"127.0.0.1:{proxy_port}", gb_register_timeout=3.0)
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        result = await simulator.trigger_register(NVR)
        assert result["status"] == "REGISTERED"
        assert len(registrar.registrations()) == 1
    finally:
        await registrar.stop()


# ---------------------------------------------------------------- 7. 延迟响应


async def test_delayed_response_within_timeout() -> None:
    """响应延迟（超时内）不影响注册：0.3s 延迟、3s 超时仍成功。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        proxy_port = free_port(socket.SOCK_DGRAM)
        proxy = _UdpProxy(target_addr=("127.0.0.1", reg_port), delay=0.3)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: proxy, local_addr=("127.0.0.1", proxy_port))

        settings = Settings(gb_registrar_addr=f"127.0.0.1:{proxy_port}", gb_register_timeout=3.0)
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        start = time.monotonic()
        result = await simulator.trigger_register(NVR)
        elapsed = time.monotonic() - start
        assert result["status"] == "REGISTERED"
        assert elapsed < 3.0, "延迟响应应在有界超时内完成"
    finally:
        await registrar.stop()


async def test_delayed_response_beyond_timeout_fails() -> None:
    """响应延迟超过超时 → REGISTER_FAILED（有界失败，不悬挂）。"""
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(host="127.0.0.1", port=reg_port, realm=REALM, password=PASSWORD)
    await registrar.start()
    try:
        proxy_port = free_port(socket.SOCK_DGRAM)
        proxy = _UdpProxy(target_addr=("127.0.0.1", reg_port), delay=1.5)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: proxy, local_addr=("127.0.0.1", proxy_port))

        settings = Settings(gb_registrar_addr=f"127.0.0.1:{proxy_port}", gb_register_timeout=0.5)
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        result = await simulator.trigger_register(NVR)
        assert result["status"] == "REGISTER_FAILED"
        assert result["registered"] is False
    finally:
        await registrar.stop()


# ---------------------------------------------------------------- 8. reset 运行态干净


def test_reset_releases_runtime_state(client: httpx.Client) -> None:
    """register 后 reset：Simulator 回到 IDLE，事件/注册表清空，重新注册正常。"""
    client.post("/testkit/v1/reset")
    client.post(f"/testkit/v1/devices/{NVR}/register")
    wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("REGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert data_of(client.get("/testkit/v1/sip/registrar/registrations"))["items"]

    client.post("/testkit/v1/reset")
    sim = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert sim["status"] == "IDLE"
    assert sim["registered"] is False
    assert sim["attemptCount"] == 0
    assert data_of(client.get("/testkit/v1/sip/registrar/registrations"))["items"] == []

    # 重新注册仍可确定性成功
    client.post(f"/testkit/v1/devices/{NVR}/register")
    status = wait_until_value(
        lambda: data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"],
        lambda s: s in ("REGISTERED", "REGISTER_FAILED"),
        timeout=10.0,
    )
    assert status == "REGISTERED"
