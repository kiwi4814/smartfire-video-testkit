"""SIP 注册（真实 UDP）：

1. 端到端：触发控制面 register → 真实 UDP 完成 REGISTER → 401 Digest(qop=auth)
   → Authorization → 200 OK，Registrar 请求日志可证；
2. 错误密码：独立 Registrar 校验失败（401 stale）→ REGISTER_FAILED；
3. 超时：静默 UDP sink 不响应 → 有界轮询超时 → REGISTER_FAILED。
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
from conftest import data_of, free_port, wait_until_value

from video_testkit.config import Settings
from video_testkit.sip.registrar import SipRegistrar
from video_testkit.sip.simulator import DeviceSimulator

NVR = "34020000001320000001"


class _SilentSink(asyncio.DatagramProtocol):
    """只接收不响应的 UDP sink：模拟无应答的注册目标。"""

    def datagram_received(self, data: bytes, addr: Any) -> None:
        pass


# 成功流程：HTTP 控制面 + 应用内 Registrar


def test_sip_register_401_to_200_via_real_udp(client: httpx.Client) -> None:
    resp = client.post(f"/testkit/v1/devices/{NVR}/register")
    assert data_of(resp) == {"externalDeviceId": NVR, "status": "REGISTERING"}

    def get_status() -> str:
        return data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))["status"]

    status = wait_until_value(
        get_status, lambda s: s in ("REGISTERED", "REGISTER_FAILED"), timeout=10.0
    )
    assert status == "REGISTERED", "REGISTER 应在有界时间内完成并成功"

    state = data_of(client.get(f"/testkit/v1/devices/{NVR}/status"))
    assert state["registered"] is True
    assert state["registeredAt"]
    assert state["expiresAt"]
    assert state["lastError"] is None

    # Registrar 请求日志应包含两次 REGISTER：401 挑战 + 200 确认（Digest qop=auth）
    items = data_of(client.get("/testkit/v1/sip/registrar/requests"))["items"]
    regs = [e for e in items if e["method"] == "REGISTER" and NVR in (e["fromUri"] or "")]
    assert len(regs) >= 2
    assert {e["status"] for e in regs} == {401, 200}
    assert any(e["authorized"] and e["authUsername"] == NVR for e in regs)

    registrations = data_of(client.get("/testkit/v1/sip/registrar/registrations"))["items"]
    assert any(r["username"] == NVR for r in registrations)


# 错误密码：独立 Registrar + 真实 UDP


async def test_sip_wrong_password_fails() -> None:
    reg_port = free_port(socket.SOCK_DGRAM)
    registrar = SipRegistrar(
        host="127.0.0.1", port=reg_port, realm="3402000000", password="correct-pw"
    )
    await registrar.start()
    try:
        settings = Settings(
            gb_registrar_addr=f"127.0.0.1:{reg_port}",
            gb_password="wrong-pw",
            gb_register_timeout=2.0,
        )
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        result = await simulator.trigger_register(NVR)
        assert result["status"] == "REGISTER_FAILED"
        assert result["registered"] is False
        assert "401" in (result["lastError"] or "")
        assert result["attemptCount"] == 1
    finally:
        await registrar.stop()


# 超时：静默 UDP sink


async def test_sip_register_timeout() -> None:
    port = free_port(socket.SOCK_DGRAM)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_SilentSink, local_addr=("127.0.0.1", port))
    try:
        settings = Settings(
            gb_registrar_addr=f"127.0.0.1:{port}",
            gb_register_timeout=0.5,
        )
        simulator = DeviceSimulator(settings)
        simulator.set_known_device(NVR)
        result = await simulator.trigger_register(NVR)
        assert result["status"] == "REGISTER_FAILED"
        assert result["registered"] is False
        assert "超时" in (result["lastError"] or "")
    finally:
        transport.close()
