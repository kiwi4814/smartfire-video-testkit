"""Provider 侧 Catalog 查询客户端：组合设备地址簿与 Registrar 查询事务。

Fake Provider 通过该客户端向模拟设备发送真实 SIP MESSAGE Catalog 查询，
设备地址来自 DeviceSimulator 的常驻监听（模拟真实 Provider 依据设备注册
地址路由查询）。仅依赖窄接口，便于与真实 Provider 行为对齐。
"""

from __future__ import annotations

from typing import Protocol

from video_testkit.sip.catalog import CatalogQueryError, CatalogQueryResult


class DeviceAddrSource(Protocol):
    def device_listener_addr(self, device_id: str) -> tuple[str, int] | None: ...


class CatalogRegistrar(Protocol):
    async def query_catalog(
        self,
        device_id: str,
        target: tuple[str, int],
        timeout: float,
        settle_window: float,
    ) -> CatalogQueryResult: ...


class CatalogClient:
    """通过真实 SIP MESSAGE 向模拟设备查询目录的黑盒客户端。"""

    def __init__(self, registrar: CatalogRegistrar, simulator: DeviceAddrSource) -> None:
        self._registrar = registrar
        self._simulator = simulator

    async def query(
        self, device_id: str, timeout: float, settle_window: float
    ) -> CatalogQueryResult:
        target = self._simulator.device_listener_addr(device_id)
        if target is None:
            raise CatalogQueryError(f"设备无监听地址: {device_id}")
        return await self._registrar.query_catalog(device_id, target, timeout, settle_window)
