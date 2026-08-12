"""Catalog 发现（VT-04）：真实 SIP MESSAGE 驱动 Provider 目录发现。

公共 seam：``/testkit/v1`` 安排设备目录场景，``/provider/v1`` catalog-sync
与 device/channel 视图是结果 seam，真实 SIP MESSAGE（MANSCDP+xml）是协议 seam。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

from video_testkit.sip.catalog import (
    CatalogItemData,
    build_catalog_query_xml,
    build_catalog_response_xml,
    parse_catalog_query,
    parse_catalog_response,
)

NVR = "34020000001320000001"
IPC = "34020000001320000002"
NVR_CHANNELS = [
    "34020000001310000001",
    "34020000001310000002",
    "34020000001310000003",
    "34020000001310000004",
]
IPC_CHANNEL = "34020000001310000021"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _sync(client: httpx.Client, device_id: str) -> dict:
    resp = client.post(
        f"/provider/v1/devices/{device_id}/catalog-syncs",
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    accepted = data_of(resp, expected_status=202)
    op_id = accepted["operationId"]

    def get_operation() -> dict:
        return data_of(client.get(f"/provider/v1/catalog-syncs/{op_id}"))

    return wait_until_value(
        get_operation,
        lambda d: d["status"] in ("SUCCEEDED", "PARTIAL", "FAILED", "EXPIRED"),
        timeout=8.0,
    )


def _configure(client: httpx.Client, device_id: str, **kwargs: object) -> dict:
    resp = client.post(f"/testkit/v1/devices/{device_id}/catalog", json=kwargs)
    return data_of(resp, expected_status=200)


def _channels(client: httpx.Client, device_id: str) -> list[str]:
    page = data_of(client.get(f"/provider/v1/devices/{device_id}/channels"))
    return [item["externalChannelId"] for item in page["items"]]


# ---------------------------------------------------------------- XML 编解码


def test_catalog_xml_roundtrip() -> None:
    xml = build_catalog_query_xml("34020000001320000001", 7)
    query = parse_catalog_query(xml.encode("utf-8"))
    assert query.device_id == "34020000001320000001"
    assert query.sn == 7

    items = [
        CatalogItemData(
            device_id="34020000001310000001",
            name="走廊东门",
            manufacturer="TESTKIT",
            model="CH-MOCK-1080P",
            status="ON",
            ptz_type=0,
            parental=0,
            resolution="1920x1080",
            codec="H264",
            has_audio=True,
            supports_ptz=False,
            supports_device_record=True,
        )
    ]
    xml = build_catalog_response_xml(7, "34020000001320000001", items, sum_num=1)
    parsed = parse_catalog_response(xml.encode("utf-8"))
    assert parsed.sn == 7
    assert parsed.device_id == "34020000001320000001"
    assert parsed.sum_num == 1
    assert parsed.items[0].device_id == "34020000001310000001"
    assert parsed.items[0].name == "走廊东门"


def test_catalog_response_gb2312_chinese_name() -> None:
    items = [
        CatalogItemData(
            device_id="34020000001310000001",
            name="车间A区",
            manufacturer="TESTKIT",
            model="IPC-MOCK",
            status="ON",
            ptz_type=1,
            parental=0,
            resolution="1280x720",
            codec="H264",
            has_audio=True,
            supports_ptz=True,
            supports_device_record=True,
        )
    ]
    xml = build_catalog_response_xml(3, IPC, items, sum_num=1, charset="GB2312")
    parsed = parse_catalog_response(xml.encode("gb2312"))
    assert parsed.items[0].name == "车间A区"


def test_catalog_response_malformed_rejected() -> None:
    with pytest.raises(ValueError):
        parse_catalog_response(b"NOT-VALID-XML{{{")
    with pytest.raises(ValueError):
        parse_catalog_response(
            b'<?xml version="1.0"?><Response><CmdType>Catalog</CmdType><SN>1</SN></Response>'
        )


# ---------------------------------------------------------------- 正常发现


def test_catalog_sync_discovers_nvr_channels(client: httpx.Client) -> None:
    """一次 catalog-sync 通过真实 SIP 发现 4 通道 NVR，稳定 ID、数量与分页正确。"""
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4
    assert _channels(client, NVR) == NVR_CHANNELS


def test_catalog_sync_discovers_ipc_single_channel(client: httpx.Client) -> None:
    result = _sync(client, IPC)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 1
    assert _channels(client, IPC) == [IPC_CHANNEL]


# ---------------------------------------------------------------- 多消息/乱序/重复


def test_multi_message_pagination_aggregates_all(client: httpx.Client) -> None:
    """设备分 2 条消息（每条 2 通道）返回目录，Provider 聚合出 4 通道。"""
    _configure(client, NVR, mode="multi", pageSize=2)
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4
    assert _channels(client, NVR) == NVR_CHANNELS


def test_out_of_order_multi_message_aggregates_all(client: httpx.Client) -> None:
    """设备乱序发送分页消息，Provider 按身份聚合不丢失、不重复。"""
    _configure(client, NVR, mode="out-of-order", pageSize=2)
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4
    assert _channels(client, NVR) == NVR_CHANNELS


def test_duplicate_catalog_responses_not_duplicated(client: httpx.Client) -> None:
    """设备重复发送目录消息，Provider 去重，不产生重复资源。"""
    _configure(client, NVR, mode="duplicate")
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4
    assert _channels(client, NVR) == NVR_CHANNELS


def test_repeated_catalog_sync_is_idempotent(client: httpx.Client) -> None:
    """连续两次 catalog-sync，通道总数保持不变（重复 Catalog 不产生重复资源）。"""
    first = _sync(client, NVR)
    second = _sync(client, NVR)
    assert first["discoveredCount"] == 4
    assert second["discoveredCount"] == 4
    assert len(_channels(client, NVR)) == 4


# ---------------------------------------------------------------- PARTIAL / 失败路径


def test_missing_channel_yields_partial_with_progress(client: httpx.Client) -> None:
    """设备目录缺 1 通道：PARTIAL 保留有效项并暴露确定性 progress/discoveredCount。"""
    _configure(client, NVR, mode="missing", missingChannelIds=[NVR_CHANNELS[2]])
    result = _sync(client, NVR)
    assert result["status"] == "PARTIAL"
    assert result["discoveredCount"] == 3
    channels = _channels(client, NVR)
    assert NVR_CHANNELS[2] in channels  # 非破坏性：已有通道不被删除


def test_timeout_no_response_fails(client: httpx.Client) -> None:
    """设备不响应查询：catalog-sync 最终 FAILED。"""
    _configure(client, NVR, mode="timeout")
    result = _sync(client, NVR)
    assert result["status"] == "FAILED"
    assert result["error"] is not None


def test_malformed_catalog_response_fails(client: httpx.Client) -> None:
    """设备返回畸形目录 XML：catalog-sync 最终 FAILED。"""
    _configure(client, NVR, mode="malformed")
    result = _sync(client, NVR)
    assert result["status"] == "FAILED"


def test_delayed_catalog_response_succeeds(client: httpx.Client) -> None:
    """设备延迟 0.5s 响应：仍在有界窗口内，成功发现全部通道。"""
    _configure(client, NVR, mode="delayed", delaySeconds=0.5)
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4


# ---------------------------------------------------------------- 缺失通道重现


def test_missing_channel_reappears_same_identity(client: httpx.Client) -> None:
    """缺失通道恢复后以同一 Protocol Source Identity 再出现。"""
    _configure(client, NVR, mode="missing", missingChannelIds=[NVR_CHANNELS[0]])
    partial = _sync(client, NVR)
    assert partial["status"] == "PARTIAL"

    _configure(client, NVR, mode="normal")
    full = _sync(client, NVR)
    assert full["status"] == "SUCCEEDED"
    assert full["discoveredCount"] == 4
    assert _channels(client, NVR) == NVR_CHANNELS


# ---------------------------------------------------------------- reset 与 SIP seam


def test_catalog_scenario_reset_restores_default(client: httpx.Client) -> None:
    """reset 恢复默认目录场景，重复 catalog-sync 正常成功。"""
    _configure(client, NVR, mode="timeout")
    client.post("/testkit/v1/reset")
    result = _sync(client, NVR)
    assert result["status"] == "SUCCEEDED"
    assert result["discoveredCount"] == 4


def test_catalog_uses_real_sip_messages(client: httpx.Client) -> None:
    """SIP seam 证据：Registrar 收到过 MANSCDP+xml 目录响应 MESSAGE，设备侧有查询/响应统计。"""
    _configure(client, NVR, mode="multi", pageSize=2)
    _sync(client, NVR)

    messages = data_of(client.get("/testkit/v1/sip/registrar/requests"))["items"]
    catalog_responses = [
        m
        for m in messages
        if m.get("method") == "MESSAGE"
        and (m.get("contentType") or "").lower() == "application/manscdp+xml"
    ]
    assert len(catalog_responses) >= 2  # 分页至少 2 条响应

    status = data_of(client.get(f"/testkit/v1/devices/{NVR}/catalog"))
    assert status["queriesReceived"] >= 1
    assert status["responsesSent"] >= 2
