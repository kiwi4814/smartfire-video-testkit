"""Provider 侧实时流信令客户端：组合设备地址簿与 Registrar UAC 事务。

Fake Provider 通过该客户端向模拟设备发送真实 SIP INVITE/ACK/BYE，
并维护 providerStreamKey → Dialog 运行态映射（不进入 Provider Interface）。
"""

from __future__ import annotations

from typing import Protocol

from video_testkit.sip.registrar import LiveDialog, LiveInviteError


class DeviceAddrSource(Protocol):
    def device_listener_addr(self, device_id: str) -> tuple[str, int] | None: ...


class LiveRegistrar(Protocol):
    async def invite_device(
        self,
        device_id: str,
        target: tuple[str, int],
        timeout: float,
        sdp_media: tuple[str, int] | None = None,
        session_name: str = "Play",
    ) -> LiveDialog: ...

    async def send_bye(
        self, dialog: LiveDialog, target: tuple[str, int], timeout: float
    ) -> bool: ...


class LiveClient:
    """通过真实 SIP 信令建立/拆除实时流 Dialog 的黑盒客户端。"""

    def __init__(self, registrar: LiveRegistrar, simulator: DeviceAddrSource) -> None:
        self._registrar = registrar
        self._simulator = simulator
        # providerStreamKey → 进行中的 Dialog（stream 停止或 reset 时清理）。
        self._dialogs: dict[str, LiveDialog] = {}

    async def establish(
        self,
        device_id: str,
        timeout: float,
        sdp_media: tuple[str, int] | None = None,
        session_name: str = "Play",
    ) -> LiveDialog:
        target = self._simulator.device_listener_addr(device_id)
        if target is None:
            raise LiveInviteError(f"设备无监听地址: {device_id}")
        return await self._registrar.invite_device(
            device_id, target, timeout, sdp_media=sdp_media, session_name=session_name
        )

    async def teardown(self, device_id: str, dialog: LiveDialog, timeout: float) -> None:
        target = self._simulator.device_listener_addr(device_id)
        if target is None:
            return
        await self._registrar.send_bye(dialog, target, timeout)

    def attach_dialog(self, stream_key: str, dialog: LiveDialog) -> None:
        self._dialogs[stream_key] = dialog

    def dialog(self, stream_key: str) -> LiveDialog | None:
        return self._dialogs.get(stream_key)

    def detach_dialog(self, stream_key: str) -> None:
        self._dialogs.pop(stream_key, None)

    def reset(self) -> None:
        self._dialogs.clear()
