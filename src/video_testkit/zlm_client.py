"""ZLMediaKit HTTP API 客户端（VT-06）。

用于：动态开启/关闭 RTP 接收端口（openRtpServer/closeRtpServer）、
查询流在线状态（isMediaOnline/getMediaList），以真实媒体到达作为
Provider STREAMING 的证据。所有调用有界超时；关闭操作幂等。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx

APP_RTP = "rtp"


class ZlmError(Exception):
    """ZLM API 调用失败（非 0 code、HTTP 错误、超时）。"""


class ZlmClient:
    def __init__(
        self,
        api_url: str,
        secret: str,
        rtp_host: str,
        rtp_port_range: tuple[int, int],
        timeout: float = 3.0,
    ) -> None:
        self._api = api_url.rstrip("/")
        self._secret = secret
        self._rtp_host = rtp_host
        self._port_range = rtp_port_range
        self._timeout = timeout
        self._next_port = rtp_port_range[0]
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ RTP 端口

    async def open_rtp_server(
        self,
        stream_id: str,
        port: int | None = None,
        ssrc: int | None = None,
    ) -> int:
        """开启 RTP 接收端口并绑定 stream_id/可选 SSRC；返回实际端口。"""
        params: dict[str, Any] = {
            "stream_id": stream_id,
            "tcp_mode": 0,
            "port": port if port is not None else self.next_rtp_port(),
        }
        if ssrc is not None:
            params["ssrc"] = ssrc
        data = await self._call("openRtpServer", **params)
        actual = data.get("port")
        if not isinstance(actual, int):
            raise ZlmError(f"openRtpServer 未返回端口: {data}")
        self._mark_port_used(actual)
        return actual

    async def close_rtp_server(self, stream_id: str) -> None:
        """关闭 RTP 端口并释放流；对不存在的流幂等（hit=0）。"""
        await self._call("closeRtpServer", stream_id=stream_id)

    # ------------------------------------------------------------ 流状态

    async def stream_online(self, stream_id: str) -> bool:
        """查询 app=rtp 下指定流的在线状态。"""
        data = await self._call(
            "isMediaOnline",
            app=APP_RTP,
            stream=stream_id,
            schema="rtsp",
            vhost="__defaultVhost__",
        )
        return bool(data.get("online"))

    async def wait_stream_online(
        self, stream_id: str, timeout: float, interval: float = 0.2
    ) -> bool:
        """有界轮询 stream-online；超时返回 False。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if await self.stream_online(stream_id):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(interval, remaining))

    async def media_list(self) -> list[dict[str, Any]]:
        """当前全部媒体流（用于清理断言）。"""
        data = await self._call("getMediaList")
        return cast(list[dict[str, Any]], data.get("data", []))

    # ------------------------------------------------------------ 内部

    def next_rtp_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        if self._next_port > self._port_range[1]:
            self._next_port = self._port_range[0]
        return port

    def _mark_port_used(self, port: int) -> None:
        # 递增分配游标，避免与显式打开的端口冲突。
        if self._port_range[0] <= port < self._next_port:
            self._next_port = port + 1
            if self._next_port > self._port_range[1]:
                self._next_port = self._port_range[0]

    async def _call(self, action: str, **params: Any) -> dict[str, Any]:
        if self._client is None:
            raise ZlmError("ZlmClient 未启动")
        query = {"secret": self._secret}
        query.update(params)
        try:
            resp = await self._client.get(f"{self._api}/index/api/{action}", params=query)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ZlmError(f"ZLM API {action} 调用失败: {exc}") from exc
        if data.get("code") != 0:
            raise ZlmError(f"ZLM API {action} 返回错误: {data}")
        return cast(dict[str, Any], data)
