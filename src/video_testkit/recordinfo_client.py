"""Provider 侧 RecordInfo 查询客户端：组合设备地址簿与 Registrar 查询事务。

Fake Provider 通过该客户端向模拟设备发送真实 SIP MESSAGE RecordInfo 查询
（body DeviceID 为通道 ID），设备地址来自 DeviceSimulator 的常驻监听。
仅依赖窄接口，便于与真实 Provider 行为对齐。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from video_testkit.sip.recordinfo import RecordInfoQueryError, RecordInfoQueryResult


class DeviceAddrSource(Protocol):
    def device_listener_addr(self, device_id: str) -> tuple[str, int] | None: ...


class RecordInfoRegistrar(Protocol):
    async def query_recordinfo(
        self,
        device_id: str,
        channel_id: str,
        target: tuple[str, int],
        start_time: datetime,
        end_time: datetime,
        record_type: str,
        timeout: float,
        settle_window: float,
    ) -> RecordInfoQueryResult: ...


class RecordInfoClient:
    """通过真实 SIP MESSAGE 向模拟设备查询录像目录的黑盒客户端。"""

    def __init__(self, registrar: RecordInfoRegistrar, simulator: DeviceAddrSource) -> None:
        self._registrar = registrar
        self._simulator = simulator

    async def query(
        self,
        device_id: str,
        channel_id: str,
        start_time: datetime,
        end_time: datetime,
        record_type: str,
        timeout: float,
        settle_window: float,
    ) -> RecordInfoQueryResult:
        target = self._simulator.device_listener_addr(device_id)
        if target is None:
            raise RecordInfoQueryError(f"设备无监听地址: {device_id}")
        return await self._registrar.query_recordinfo(
            device_id,
            channel_id,
            target,
            start_time,
            end_time,
            record_type,
            timeout,
            settle_window,
        )
