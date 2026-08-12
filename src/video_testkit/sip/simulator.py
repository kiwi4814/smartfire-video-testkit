"""GB28181 Device Simulator：通过真实 UDP 完成 REGISTER -> 401 -> Authorization -> 200。

有界超时；每次触发后状态与最后错误可通过控制面查询。本切片不做 Catalog/RTP。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from video_testkit.config import Settings
from video_testkit.logging_conf import utc_z_now
from video_testkit.sip.catalog import (
    DEFAULT_CHARSET,
    PROVIDER_SIP_ID,
    CatalogItemData,
    build_catalog_response_xml,
    parse_catalog_query,
)
from video_testkit.sip.digest import (
    build_authorization_header,
    generate_cnonce,
    parse_params,
)
from video_testkit.sip.keepalive import CONTENT_TYPE
from video_testkit.sip.message import SipMessage, build_message, parse_message
from video_testkit.sip.sdp import build_sdp_answer, parse_sdp

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
    # Keepalive 心跳状态。
    keepalive_active: bool = False
    keepalive_sn: int = 0
    last_keepalive_at: datetime | None = None
    last_keepalive_error: str | None = None


@dataclass
class CatalogScenario:
    """设备侧目录场景：内容、响应模式与发送统计。

    响应模式：normal（单消息）、multi（分页多消息）、duplicate（重复发送）、
    delayed（延迟响应）、missing（缺失通道）、malformed（畸形 XML）、
    out-of-order（分页乱序）、timeout（不响应）。
    """

    items: list[CatalogItemData]
    mode: str = "normal"
    page_size: int = 0
    delay_seconds: float = 0.0
    missing_channel_ids: set[str] = field(default_factory=set)
    charset: str = DEFAULT_CHARSET
    revision: int = 0
    queries_received: int = 0
    responses_sent: int = 0
    last_error: str | None = None
    last_query_sn: int | None = None
    last_query_at: str | None = None


# 默认设备目录（与 Provider 侧 seed 场景的稳定 GB 身份对齐）。
DEFAULT_NVR_CATALOG = [
    CatalogItemData(
        "34020000001310000001",
        "走廊东门",
        "TESTKIT",
        "CH-MOCK-1080P",
        "ON",
        0,
        0,
        "1920x1080",
        "H264",
        True,
        False,
        True,
    ),
    CatalogItemData(
        "34020000001310000002",
        "走廊西门",
        "TESTKIT",
        "CH-MOCK-1080P",
        "ON",
        0,
        0,
        "1280x720",
        "H264",
        False,
        False,
        True,
    ),
    CatalogItemData(
        "34020000001310000003",
        "停车场入口",
        "TESTKIT",
        "CH-MOCK-1080P",
        "ON",
        0,
        0,
        "1920x1080",
        "H265",
        False,
        False,
        True,
    ),
    CatalogItemData(
        "34020000001310000004",
        "消防通道",
        "TESTKIT",
        "CH-MOCK-1080P",
        "ON",
        0,
        0,
        "1920x1080",
        "H264",
        True,
        True,
        True,
    ),
]
DEFAULT_IPC_CATALOG = [
    CatalogItemData(
        "34020000001310000021",
        "车间A区",
        "TESTKIT",
        "IPC-MOCK",
        "ON",
        1,
        0,
        "1280x720",
        "H264",
        True,
        True,
        True,
    ),
]


def _default_catalog(device_id: str) -> list[CatalogItemData]:
    if device_id == "34020000001320000001":
        return list(DEFAULT_NVR_CATALOG)
    if device_id == "34020000001320000002":
        return list(DEFAULT_IPC_CATALOG)
    return []


@dataclass
class DialogState:
    """设备侧（UAS）一次实时流 Dialog 状态。"""

    call_id: str
    from_tag: str
    to_tag: str
    device_id: str
    status: str  # WAITING_ACK / ESTABLISHED / TERMINATED / FAILED
    ssrc: str | None = None
    media_port: int | None = None
    target: str | None = None
    ack_received: bool = False
    bye_received: bool = False
    retransmit_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    timeout_task: asyncio.Task[None] | None = None


@dataclass
class LiveScenario:
    """设备侧实时流应答场景（响应 Provider 的 INVITE）。

    normal（立即 200 → 等 ACK）、rejection（回 4xx）、delayed（延迟 200）、
    no-ack（200 后收不到 ACK 则 Dialog FAILED）、drop（不响应 → Provider 超时）。
    """

    mode: str = "normal"
    delay_seconds: float = 0.0
    reject_code: int = 486
    ack_timeout: float = 1.5
    revision: int = 0
    invites_received: int = 0
    last_error: str | None = None
    dialogs: dict[str, DialogState] = field(default_factory=dict)


def _dialog_to_tag() -> str:
    return secrets.token_hex(8)


class _ListenerProtocol(asyncio.DatagramProtocol):
    """设备常驻 UDP 监听：接收 Provider 的 Catalog 查询 MESSAGE。"""

    def __init__(self, simulator: DeviceSimulator, device_id: str) -> None:
        self._simulator = simulator
        self._device_id = device_id
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        asyncio.create_task(self._simulator._handle_listener_datagram(self._device_id, data, addr))


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
        self._keepalive_tasks: dict[str, asyncio.Task[None]] = {}
        self._drop_next: set[str] = set()
        # 设备常驻 UDP 监听（接收 Provider 的 Catalog 查询）。
        self._listeners: dict[str, _ListenerProtocol] = {}
        # 设备侧目录场景与响应统计。
        self._catalog_scenarios: dict[str, CatalogScenario] = {}
        self._catalog_cseq: dict[str, int] = {}
        # 设备侧实时流应答场景与 Dialog 状态。
        self._live_scenarios: dict[str, LiveScenario] = {}

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        for task in self._keepalive_tasks.values():
            task.cancel()
        self._keepalive_tasks.clear()
        self._drop_next.clear()
        self.close_listeners()
        self._catalog_scenarios.clear()
        self._catalog_cseq.clear()
        self._live_scenarios.clear()
        self._devices.clear()
        self._known_ids.clear()

    def close_listeners(self) -> None:
        """关闭全部设备常驻监听（reset 与进程退出时释放 UDP socket）。"""
        for protocol in self._listeners.values():
            if protocol.transport is not None and not protocol.transport.is_closing():
                protocol.transport.close()
        self._listeners.clear()

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
        if device_id not in self._catalog_scenarios:
            self._catalog_scenarios[device_id] = CatalogScenario(items=_default_catalog(device_id))
        if device_id not in self._live_scenarios:
            self._live_scenarios[device_id] = LiveScenario()

    # ------------------------------------------------------------ 常驻监听与目录

    async def _ensure_listener(self, device_id: str) -> None:
        """为该设备创建常驻 UDP 监听（幂等）；Provider 借此向其发送 Catalog 查询。"""
        if device_id in self._listeners:
            return
        loop = asyncio.get_running_loop()
        _, protocol = await loop.create_datagram_endpoint(
            lambda: _ListenerProtocol(self, device_id), local_addr=("127.0.0.1", 0)
        )
        self._listeners[device_id] = protocol

    async def ensure_all_listeners(self) -> None:
        """为所有已知设备创建常驻监听（启动与 reset 后重建）。"""
        for device_id in sorted(self._known_ids):
            await self._ensure_listener(device_id)

    def device_listener_addr(self, device_id: str) -> tuple[str, int] | None:
        """设备常驻监听地址（Provider 查询目标）；无监听返回 None。"""
        protocol = self._listeners.get(device_id)
        if protocol is None or protocol.transport is None:
            return None
        sockname = protocol.transport.get_extra_info("sockname")
        return str(sockname[0]), int(sockname[1])

    async def _listener_port(self, device_id: str) -> int:
        await self._ensure_listener(device_id)
        addr = self.device_listener_addr(device_id)
        if addr is None:
            raise SimulatorError(f"设备监听未就绪: {device_id}")
        return addr[1]

    def configure_catalog(
        self,
        device_id: str,
        *,
        mode: str = "normal",
        page_size: int = 0,
        delay_seconds: float = 0.0,
        missing_channel_ids: list[str] | None = None,
        charset: str | None = None,
    ) -> dict[str, Any]:
        """安排该设备的目录响应场景；每次安排 revision 递增。"""
        self._require_known(device_id)
        scenario = self._catalog_scenarios[device_id]
        scenario.mode = mode
        scenario.page_size = int(page_size)
        scenario.delay_seconds = float(delay_seconds)
        scenario.missing_channel_ids = set(missing_channel_ids or [])
        if charset:
            scenario.charset = charset
        scenario.last_error = None
        scenario.revision += 1
        return self.catalog_status(device_id)

    def catalog_status(self, device_id: str) -> dict[str, Any]:
        """设备目录场景与响应统计（控制面可查）。"""
        self._require_known(device_id)
        scenario = self._catalog_scenarios[device_id]
        return {
            "externalDeviceId": device_id,
            "mode": scenario.mode,
            "pageSize": scenario.page_size,
            "delaySeconds": scenario.delay_seconds,
            "missingChannelIds": sorted(scenario.missing_channel_ids),
            "charset": scenario.charset,
            "revision": str(scenario.revision),
            "itemCount": len(scenario.items),
            "queriesReceived": scenario.queries_received,
            "responsesSent": scenario.responses_sent,
            "lastQuerySn": scenario.last_query_sn,
            "lastQueryAt": scenario.last_query_at,
            "lastError": scenario.last_error,
        }

    async def _handle_listener_datagram(self, device_id: str, data: bytes, addr: Any) -> None:
        """处理设备收到的 UDP 报文：实时流信令（INVITE/ACK/BYE）或 Catalog 查询。"""
        try:
            msg = parse_message(data)
        except ValueError:
            return
        method = msg.method()
        if method == "INVITE":
            await self._handle_invite(device_id, msg, addr)
            return
        if method == "ACK":
            self._handle_ack(device_id, msg)
            return
        if method == "BYE":
            await self._handle_bye(device_id, msg, addr)
            return
        if method != "MESSAGE":
            return
        scenario = self._catalog_scenarios.get(device_id)
        if scenario is None:
            return
        try:
            query = parse_catalog_query(msg.body_bytes)
        except ValueError:
            return  # 非 Catalog 查询（如 Keepalive 等）不处理

        scenario.queries_received += 1
        scenario.last_query_sn = query.sn
        scenario.last_query_at = utc_z_now()

        if scenario.mode == "timeout":
            return  # 模拟设备不响应查询
        if scenario.mode == "delayed" and scenario.delay_seconds > 0:
            await asyncio.sleep(scenario.delay_seconds)
        try:
            for body, charset in self._catalog_responses(device_id, query.sn, scenario):
                await self._send_catalog_response(device_id, addr, body, charset)
                scenario.responses_sent += 1
            scenario.last_error = None
        except Exception as exc:  # noqa: BLE001  # 上报控制面而非中断监听
            scenario.last_error = str(exc)
            logger.exception("Catalog 响应发送失败", extra={"deviceId": device_id})

    # ------------------------------------------------------------ 实时流信令（UAS）

    async def _handle_invite(self, device_id: str, msg: SipMessage, addr: Any) -> None:
        """设备作为 UAS 处理 INVITE：按场景应答 SDP/200、486 或保持静默。"""
        scenario = self._live_scenarios.get(device_id)
        if scenario is None:
            return
        scenario.invites_received += 1
        if scenario.mode == "drop":
            return  # 静默：Provider 侧 INVITE 超时
        try:
            parse_sdp(msg.body)
        except ValueError:
            scenario.last_error = "INVITE 缺少有效 SDP"
            return  # 无有效 offer：Provider 侧超时
        if scenario.mode == "rejection":
            await self._send_invite_response(
                device_id, msg, addr, status=scenario.reject_code, reason="Busy Here"
            )
            return
        if scenario.mode == "delayed" and scenario.delay_seconds > 0:
            await asyncio.sleep(scenario.delay_seconds)

        call_id = msg.header("call-id") or uuid.uuid4().hex
        from_tag = msg.header("from") or ""
        to_tag = _dialog_to_tag()
        dialog = DialogState(
            call_id=call_id,
            from_tag=from_tag,
            to_tag=to_tag,
            device_id=device_id,
            status="WAITING_ACK",
            ssrc=self._device_ssrc(device_id),
            media_port=self._device_media_port(device_id),
            target=f"{addr[0]}:{addr[1]}",
            created_at=utc_z_now(),
            updated_at=utc_z_now(),
        )
        scenario.dialogs[call_id] = dialog
        await self._send_invite_ok(device_id, msg, addr, dialog)
        if scenario.mode == "no-ack":
            dialog.timeout_task = asyncio.create_task(
                self._ack_timeout(device_id, call_id, scenario.ack_timeout)
            )

    def _handle_ack(self, device_id: str, msg: SipMessage) -> None:
        """设备收到 ACK：Dialog 进入 ESTABLISHED；no-ack 场景模拟 ACK 丢失。"""
        scenario = self._live_scenarios.get(device_id)
        if scenario is None or scenario.mode == "no-ack":
            return
        call_id = msg.header("call-id")
        if not call_id:
            return
        dialog = scenario.dialogs.get(call_id)
        if dialog is None or dialog.status != "WAITING_ACK":
            return
        dialog.ack_received = True
        dialog.status = "ESTABLISHED"
        dialog.updated_at = utc_z_now()
        if dialog.timeout_task is not None:
            dialog.timeout_task.cancel()
            dialog.timeout_task = None

    async def _handle_bye(self, device_id: str, msg: SipMessage, addr: Any) -> None:
        """设备收到 BYE：Dialog 进入 TERMINATED 并回 200。"""
        scenario = self._live_scenarios.get(device_id)
        if scenario is None:
            return
        call_id = msg.header("call-id")
        if call_id:
            dialog = scenario.dialogs.get(call_id)
            if dialog is not None:
                dialog.bye_received = True
                dialog.status = "TERMINATED"
                dialog.updated_at = utc_z_now()
                if dialog.timeout_task is not None:
                    dialog.timeout_task.cancel()
                    dialog.timeout_task = None
        await self._send_invite_response(device_id, msg, addr, status=200, reason="OK")

    async def _ack_timeout(self, device_id: str, call_id: str, timeout: float) -> None:
        """no-ack 场景：200 后未收到 ACK，Dialog 以 FAILED 收敛。"""
        await asyncio.sleep(timeout)
        scenario = self._live_scenarios.get(device_id)
        if scenario is None:
            return
        dialog = scenario.dialogs.get(call_id)
        if dialog is not None and dialog.status == "WAITING_ACK":
            dialog.status = "FAILED"
            dialog.updated_at = utc_z_now()

    async def _send_invite_ok(
        self, device_id: str, msg: SipMessage, addr: Any, dialog: DialogState
    ) -> None:
        sdp = build_sdp_answer("127.0.0.1", dialog.media_port or 0, dialog.ssrc or "", "H264")
        await self._send_invite_response(
            device_id, msg, addr, status=200, reason="OK", to_tag=dialog.to_tag, body=sdp
        )

    async def _send_invite_response(
        self,
        device_id: str,
        msg: SipMessage,
        addr: Any,
        *,
        status: int,
        reason: str,
        to_tag: str | None = None,
        body: str | None = None,
    ) -> None:
        """回显 INVITE/BYE 的 Via/From/To/Call-ID/CSeq 并返回 SIP 响应。"""
        protocol = self._listeners.get(device_id)
        if protocol is None or protocol.transport is None:
            return
        to_tag = to_tag or _dialog_to_tag()
        headers: list[tuple[str, str]] = []
        for name in ("via", "from", "to", "call-id", "cseq"):
            value = msg.header(name)
            if value:
                headers.append((name, value))
        for i, (name, value) in enumerate(headers):
            if name == "to" and ";tag=" not in value:
                headers[i] = (name, f"{value};tag={to_tag}")
        if body is not None:
            headers.append(("Content-Type", "application/sdp"))
        protocol.transport.sendto(
            build_message(f"SIP/2.0 {status} {reason}", headers, body or "", body_encoding="utf-8"),
            tuple(addr),
        )

    @staticmethod
    def _device_ssrc(device_id: str) -> str:
        return f"01000000{len(device_id):02d}"

    @staticmethod
    def _device_media_port(device_id: str) -> int:
        return 20000 + (sum(ord(c) for c in device_id) % 1000)

    # ------------------------------------------------------------ 实时流控制面

    def configure_live(
        self,
        device_id: str,
        *,
        mode: str = "normal",
        delay_seconds: float = 0.0,
        reject_code: int = 486,
        ack_timeout: float = 1.5,
    ) -> dict[str, Any]:
        """安排设备实时流应答场景；每次安排 revision 递增。"""
        self._require_known(device_id)
        scenario = self._live_scenarios[device_id]
        scenario.mode = mode
        scenario.delay_seconds = float(delay_seconds)
        scenario.reject_code = int(reject_code)
        scenario.ack_timeout = float(ack_timeout)
        scenario.last_error = None
        scenario.revision += 1
        return self.live_status(device_id)

    def live_status(self, device_id: str) -> dict[str, Any]:
        """设备实时流场景与 Dialog 诊断（控制面可查，脱敏视图）。"""
        self._require_known(device_id)
        scenario = self._live_scenarios[device_id]
        return {
            "externalDeviceId": device_id,
            "mode": scenario.mode,
            "delaySeconds": scenario.delay_seconds,
            "rejectCode": scenario.reject_code,
            "ackTimeoutSeconds": scenario.ack_timeout,
            "revision": str(scenario.revision),
            "invitesReceived": scenario.invites_received,
            "lastError": scenario.last_error,
            "dialogs": [self._dialog_view(d) for d in scenario.dialogs.values()],
        }

    @staticmethod
    def _dialog_view(dialog: DialogState) -> dict[str, Any]:
        """Dialog 脱敏诊断视图：Call-ID 截断，不暴露完整 SIP 运行态身份。"""
        return {
            "callId": f"{dialog.call_id[:8]}…",
            "deviceId": dialog.device_id,
            "status": dialog.status,
            "ssrc": dialog.ssrc,
            "mediaPort": dialog.media_port,
            "target": dialog.target,
            "ackReceived": dialog.ack_received,
            "byeReceived": dialog.bye_received,
            "createdAt": dialog.created_at,
            "updatedAt": dialog.updated_at,
        }

    def _catalog_responses(
        self, device_id: str, query_sn: int, scenario: CatalogScenario
    ) -> list[tuple[str, str]]:
        """按场景模式生成响应 (body, charset) 列表。"""
        sent_items = [
            item for item in scenario.items if item.device_id not in scenario.missing_channel_ids
        ]
        sum_num = len(scenario.items)  # 总量包含缺失项，驱动 Provider 侧 PARTIAL
        charset = scenario.charset

        if scenario.mode == "malformed":
            return [("NOT-VALID-XML{{{", charset)]
        if scenario.mode in ("multi", "out-of-order"):
            page = scenario.page_size or 2
            batches = [sent_items[i : i + page] for i in range(0, len(sent_items), page)]
            if scenario.mode == "out-of-order" and len(batches) > 1:
                batches = [batches[-1], *batches[:-1]]  # 末批先发，其余顺序后发
            bodies = [
                build_catalog_response_xml(query_sn, device_id, batch, sum_num, charset)
                for batch in batches
            ]
            if scenario.mode == "out-of-order":
                return [(b, charset) for b in bodies]
            return [(b, charset) for b in bodies]
        body = build_catalog_response_xml(query_sn, device_id, sent_items, sum_num, charset)
        if scenario.mode == "duplicate":
            return [(body, charset), (body, charset)]
        return [(body, charset)]

    async def _send_catalog_response(
        self,
        device_id: str,
        target_addr: Any,
        body: str,
        charset: str,
    ) -> None:
        """从设备常驻监听 socket 向查询方（Provider）发送 Catalog 响应 MESSAGE。"""
        protocol = self._listeners.get(device_id)
        if protocol is None or protocol.transport is None:
            raise SimulatorError(f"设备监听已关闭: {device_id}")
        local_port = protocol.transport.get_extra_info("sockname")[1]
        uri = f"sip:{PROVIDER_SIP_ID}@{target_addr[0]}:{target_addr[1]}"
        branch = "z9hG4bK" + uuid.uuid4().hex[:20]
        self._catalog_cseq[device_id] = self._catalog_cseq.get(device_id, 0) + 1
        msg = build_message(
            f"MESSAGE {uri} SIP/2.0",
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport"),
                ("From", f"<sip:{device_id}@127.0.0.1:{local_port}>;tag={uuid.uuid4().hex[:12]}"),
                ("To", f"<{uri}>"),
                ("Call-ID", uuid.uuid4().hex),
                ("CSeq", f"{self._catalog_cseq[device_id]} MESSAGE"),
                ("Max-Forwards", "70"),
                ("Content-Type", CONTENT_TYPE),
                ("User-Agent", USER_AGENT),
            ],
            body,
            body_encoding=charset,
        )
        protocol.transport.sendto(msg, tuple(target_addr))

    # ------------------------------------------------------------ Keepalive 控制

    def start_keepalive(self, device_id: str) -> dict[str, Any]:
        """启动该设备的周期 Keepalive 发送循环（真实 SIP MESSAGE）。幂等。"""
        self._require_known(device_id)
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        if state.keepalive_active:
            return self._view(device_id, state)
        state.keepalive_active = True
        state.last_keepalive_error = None
        task = asyncio.create_task(self._keepalive_loop(device_id))
        self._keepalive_tasks[device_id] = task
        return self._view(device_id, state)

    def stop_keepalive(self, device_id: str) -> dict[str, Any]:
        """暂停该设备的 Keepalive 发送（模拟心跳中断）。"""
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        state.keepalive_active = False
        task = self._keepalive_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()
        return self._view(device_id, state)

    def resume_keepalive(self, device_id: str) -> dict[str, Any]:
        """恢复该设备的 Keepalive 发送。"""
        return self.start_keepalive(device_id)

    def drop_next_keepalive(self, device_id: str) -> dict[str, Any]:
        """跳过下一次 Keepalive 发送（模拟单次丢包）。"""
        self._require_known(device_id)
        self._drop_next.add(device_id)
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        return self._view(device_id, state)

    async def send_keepalive(self, device_id: str, malformed: bool = False) -> dict[str, Any]:
        """立即发送一次 Keepalive（真实 UDP），返回 Provider 响应结果。"""
        self._require_known(device_id)
        state = self._devices.setdefault(device_id, SimulatorDeviceState())
        state.last_keepalive_error = None

        timeout = self._settings.gb_register_timeout
        reg_host, reg_port = self._split_addr(self._settings.effective_gb_registrar_addr)
        uri = f"sip:{device_id}@{reg_host}:{reg_port}"
        try:
            status_code = await self._keepalive_trip(
                device_id, uri, (reg_host, reg_port), timeout, malformed, state
            )
            state.last_keepalive_at = self._now_fn()
            return {
                "externalDeviceId": device_id,
                "statusCode": status_code,
                "malformed": malformed,
            }
        except (TimeoutError, SimulatorError, ValueError) as exc:
            state.last_keepalive_error = str(exc)
            return {
                "externalDeviceId": device_id,
                "statusCode": None,
                "malformed": malformed,
                "error": str(exc),
            }
        except OSError as exc:
            state.last_keepalive_error = f"UDP 发送失败: {exc}"
            return {
                "externalDeviceId": device_id,
                "statusCode": None,
                "malformed": malformed,
                "error": str(exc),
            }

    async def _keepalive_loop(self, device_id: str) -> None:
        """周期发送 Keepalive；支持 pause（cancel）与单次 drop 标记。"""
        interval = self._settings.gb_keepalive_interval
        state = self._devices.get(device_id)
        while state is not None and state.keepalive_active:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if device_id in self._drop_next:
                self._drop_next.discard(device_id)
                continue
            try:
                await self.send_keepalive(device_id)
            except Exception:
                logger.exception("Keepalive 发送失败", extra={"deviceId": device_id})
            state = self._devices.get(device_id)

    async def _keepalive_trip(
        self,
        device_id: str,
        uri: str,
        target: tuple[str, int],
        timeout: float,
        malformed: bool,
        state: SimulatorDeviceState,
    ) -> int:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _ClientProtocol, local_addr=("127.0.0.1", 0)
        )
        try:
            local_port = transport.get_extra_info("sockname")[1]
            state.keepalive_sn += 1
            branch = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg = self._keepalive_message(
                device_id, uri, local_port, branch, state.keepalive_sn, malformed
            )
            transport.sendto(msg, target)
            resp = await self._await_response(protocol, branch, timeout)
            code = resp.status_code()
            if code is None or code != 200:
                raise SimulatorError(f"Keepalive 预期 200，实际 {code}")
            return int(code)
        finally:
            transport.close()

    def _keepalive_message(
        self,
        device_id: str,
        uri: str,
        local_port: int,
        branch: str,
        sn: int,
        malformed: bool,
    ) -> bytes:
        from video_testkit.sip.keepalive import CONTENT_TYPE, build_keepalive_xml

        body = "NOT-VALID-XML{{{" if malformed else build_keepalive_xml(device_id, sn)
        headers: list[tuple[str, str]] = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport"),
            ("From", f"<{uri}>;tag={uuid.uuid4().hex[:12]}"),
            ("To", f"<{uri}>"),
            ("Call-ID", uuid.uuid4().hex),
            ("CSeq", f"{sn + 1000} MESSAGE"),
            ("Max-Forwards", "70"),
            ("Content-Type", CONTENT_TYPE),
            ("User-Agent", USER_AGENT),
        ]
        return build_message(f"MESSAGE {uri} SIP/2.0", headers, body)

    def _require_known(self, device_id: str) -> None:
        if device_id not in self._known_ids:
            raise KeyError(f"未知设备: {device_id}")

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
            "keepaliveActive": state.keepalive_active,
            "keepaliveSn": state.keepalive_sn,
            "lastKeepaliveAt": (
                state.last_keepalive_at.isoformat().replace("+00:00", "Z")
                if state.last_keepalive_at
                else None
            ),
            "lastKeepaliveError": state.last_keepalive_error,
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
            contact_port = await self._listener_port(device_id)
            call_id = uuid.uuid4().hex
            from_tag = uuid.uuid4().hex[:12]

            # 1. 无 Authorization 的 REGISTER
            branch1 = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg1 = build_message(
                f"REGISTER {uri} SIP/2.0",
                self._base_headers(
                    branch1,
                    device_id,
                    uri,
                    call_id,
                    from_tag,
                    1,
                    local_port,
                    contact_port=contact_port,
                ),
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
                    self._base_headers(
                        branch,
                        device_id,
                        uri,
                        call_id,
                        from_tag,
                        cseq,
                        local_port,
                        contact_port=contact_port,
                    )
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
            contact_port = await self._listener_port(device_id)
            call_id = uuid.uuid4().hex
            from_tag = uuid.uuid4().hex[:12]

            # 1. 无 Authorization 的 REGISTER（Expires: 0）
            branch1 = "z9hG4bK" + uuid.uuid4().hex[:20]
            msg1 = build_message(
                f"REGISTER {uri} SIP/2.0",
                self._base_headers(
                    branch1,
                    device_id,
                    uri,
                    call_id,
                    from_tag,
                    1,
                    local_port,
                    expires=0,
                    contact_port=contact_port,
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
                    branch2,
                    device_id,
                    uri,
                    call_id,
                    from_tag,
                    2,
                    local_port,
                    expires=0,
                    contact_port=contact_port,
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
        contact_port: int | None = None,
    ) -> list[tuple[str, str]]:
        expires_value = self._settings.gb_expires if expires is None else expires
        contact_port = contact_port or local_port
        return [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport"),
            ("From", f"<{uri}>;tag={from_tag}"),
            ("To", f"<{uri}>"),
            ("Call-ID", call_id),
            ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:{device_id}@127.0.0.1:{contact_port}>"),
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
