"""RecordInfo 查询（VT-07）：真实 SIP MESSAGE 驱动设备录像目录查询。

公共 seam：``/testkit/v1`` 安排设备录像场景，``/provider/v1`` device-record-queries
是结果 seam，真实 SIP MESSAGE（MANSCDP+xml，RecordInfo）是协议 seam。

验收 6 项：空窗口空 items；recordKey 不透明稳定且左闭右开正确；多消息无重复、
乱序不改变身份；PARTIAL/timeout 保留有效项；UTC/device-time offset 可控可复现；
相同 Idempotency-Key 不创建第二个查询。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from conftest import data_of, wait_until_value

from video_testkit.sip.recordinfo import (
    RecordInfoItemData,
    build_recordinfo_query_xml,
    build_recordinfo_response_xml,
    parse_recordinfo_query,
    parse_recordinfo_response,
)

NVR = "34020000001320000001"
NVR_CH1 = "34020000001310000001"
NVR_CH2 = "34020000001310000002"

QUERY_START = "2026-08-01T00:00:00.000Z"
QUERY_END = "2026-08-01T02:30:00.000Z"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def _configure(client: httpx.Client, device_id: str, **kwargs: object) -> dict:
    resp = client.post(f"/testkit/v1/devices/{device_id}/recordinfo", json=kwargs)
    return data_of(resp, expected_status=200)


def _query(
    client: httpx.Client,
    device: str,
    channel: str,
    start: str = QUERY_START,
    end: str = QUERY_END,
    record_type: str = "ALL",
    idem_key: str | None = None,
) -> dict:
    resp = client.post(
        "/provider/v1/device-record-queries",
        headers={"Idempotency-Key": idem_key or uuid.uuid4().hex},
        json={
            "externalDeviceId": device,
            "externalChannelId": channel,
            "startTime": start,
            "endTime": end,
            "recordType": record_type,
        },
    )
    query_id = data_of(resp, expected_status=202)["queryId"]

    def get_query() -> dict:
        return data_of(client.get(f"/provider/v1/device-record-queries/{query_id}"))

    return wait_until_value(
        get_query,
        lambda d: d["status"] in ("SUCCEEDED", "PARTIAL", "FAILED"),
        timeout=8.0,
    )


def _default_keys(client: httpx.Client) -> set[str]:
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    return {item["recordKey"] for item in result["items"]}


# ---------------------------------------------------------------- XML 编解码


def test_recordinfo_xml_roundtrip() -> None:
    xml = build_recordinfo_query_xml(NVR_CH1, sn=7, start_time=QUERY_START, end_time=QUERY_END)
    query = parse_recordinfo_query(xml.encode("utf-8"))
    assert query.device_id == NVR_CH1
    assert query.sn == 7
    assert query.start_time.isoformat() == "2026-08-01T00:00:00+00:00"
    assert query.end_time.isoformat() == "2026-08-01T02:30:00+00:00"
    assert query.record_type == "ALL"

    items = [
        RecordInfoItemData(
            device_id=NVR_CH1,
            name="走廊东门",
            start_time=query.start_time,
            end_time=query.end_time,
            record_type="time",
            file_size=0,
        )
    ]
    xml = build_recordinfo_response_xml(7, NVR_CH1, items, sum_num=1)
    parsed = parse_recordinfo_response(xml.encode("utf-8"))
    assert parsed.sn == 7
    assert parsed.device_id == NVR_CH1
    assert parsed.sum_num == 1
    assert parsed.items[0].name == "走廊东门"
    assert parsed.items[0].start_time == query.start_time
    assert parsed.items[0].end_time == query.end_time
    assert parsed.items[0].record_type == "time"


def test_recordinfo_response_gb2312_chinese_name() -> None:
    items = [
        RecordInfoItemData(
            device_id=NVR_CH1,
            name="消防通道录像",
            start_time=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            record_type="time",
            file_size=0,
        )
    ]
    xml = build_recordinfo_response_xml(3, NVR_CH1, items, sum_num=1, charset="GB2312")
    parsed = parse_recordinfo_response(xml.encode("gb2312"))
    assert parsed.items[0].name == "消防通道录像"


def test_recordinfo_response_malformed_rejected() -> None:
    with pytest.raises(ValueError):
        parse_recordinfo_response(b"NOT-VALID-XML{{{")
    with pytest.raises(ValueError):
        parse_recordinfo_response(
            b'<?xml version="1.0"?><Response><CmdType>RecordInfo</CmdType><SN>1</SN></Response>'
        )


# ---------------------------------------------------------------- 空窗口


def test_empty_window_returns_empty_items(client: httpx.Client) -> None:
    """设备在查询窗口无录像：成功返回空 items（不是失败）。"""
    _configure(client, NVR, mode="empty")
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert result["items"] == []


# ---------------------------------------------------------------- 正常与确定性


def test_default_recordinfo_hourly_items(client: httpx.Client) -> None:
    """设备默认按小时生成录像：左闭右开区间正确、类型 TIME、设备/通道一致。"""
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert len(result["items"]) == 3
    for item in result["items"]:
        assert item["recordType"] == "TIME"
        assert item["externalDeviceId"] == NVR
        assert item["externalChannelId"] == NVR_CH1
        assert item["recordKey"].startswith("rec-")
    assert result["items"][0]["startTime"] == QUERY_START
    assert result["items"][0]["endTime"] == "2026-08-01T01:00:00.000Z"
    assert result["items"][1]["startTime"] == "2026-08-01T01:00:00.000Z"
    assert result["items"][2]["startTime"] == "2026-08-01T02:00:00.000Z"
    assert result["items"][2]["endTime"] == QUERY_END


def test_recordinfo_record_key_stable(client: httpx.Client) -> None:
    """同一录像在重复查询（不同 Idempotency-Key）中 recordKey 稳定不变。"""
    first = _default_keys(client)
    second = _default_keys(client)
    assert first == second
    assert len(first) == 3


def test_recordinfo_channel_isolated(client: httpx.Client) -> None:
    """不同通道的录像目录互不影响：recordKey 绑定通道且区间正确。"""
    ch1 = _default_keys(client)
    ch2 = _query(client, NVR, NVR_CH2)
    assert ch2["status"] == "SUCCEEDED"
    assert len(ch2["items"]) == 3
    assert ch1.isdisjoint({item["recordKey"] for item in ch2["items"]})
    assert all(item["externalChannelId"] == NVR_CH2 for item in ch2["items"])


# ---------------------------------------------------------------- 多消息/乱序/重复


def test_multi_message_aggregates_no_duplicates(client: httpx.Client) -> None:
    """设备分页返回录像：Provider 聚合出全部 items 且无重复 recordKey。"""
    _configure(client, NVR, mode="multi", pageSize=2)
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    keys = [item["recordKey"] for item in result["items"]]
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert set(keys) == _default_keys(client)  # 聚合身份与单消息一致


def test_out_of_order_aggregation_same_identity(client: httpx.Client) -> None:
    """乱序分页响应：结果身份与顺序响应一致。"""
    _configure(client, NVR, mode="out-of-order", pageSize=2)
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert {item["recordKey"] for item in result["items"]} == _default_keys(client)


def test_duplicate_recordinfo_responses_not_duplicated(client: httpx.Client) -> None:
    """设备重复发送录像响应：Provider 按区间去重，不产生重复 item。"""
    _configure(client, NVR, mode="duplicate")
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert len(result["items"]) == 3
    assert len({item["recordKey"] for item in result["items"]}) == 3


# ---------------------------------------------------------------- PARTIAL / timeout


def test_partial_preserves_valid_items(client: httpx.Client) -> None:
    """设备少发 1 条（SumNum 未收满）：PARTIAL 保留已收集有效项。"""
    _configure(client, NVR, mode="missing", missingCount=1)
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "PARTIAL"
    assert len(result["items"]) == 2
    assert result["items"][0]["startTime"] == QUERY_START
    assert result["items"][1]["startTime"] == "2026-08-01T01:00:00.000Z"
    assert result["items"][0]["endTime"] == "2026-08-01T01:00:00.000Z"


def test_timeout_no_response_fails(client: httpx.Client) -> None:
    """设备完全不响应：查询最终 FAILED 并带 error。"""
    _configure(client, NVR, mode="timeout")
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "FAILED"
    assert result["error"] is not None


def test_delayed_recordinfo_response_succeeds(client: httpx.Client) -> None:
    """设备延迟 0.5s 响应：仍在有界窗口内，成功返回全部录像。"""
    _configure(client, NVR, mode="delayed", delaySeconds=0.5)
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert len(result["items"]) == 3


# ---------------------------------------------------------------- device-time offset


def test_time_offset_controllable_and_reproducible(client: httpx.Client) -> None:
    """设备本地时间相对 UTC 偏移可控：结果时间整体平移且两次查询可复现。"""
    _configure(client, NVR, timeOffsetSeconds=7200)
    first = _query(client, NVR, NVR_CH1)
    assert first["status"] == "SUCCEEDED"
    assert len(first["items"]) == 3
    assert first["items"][0]["startTime"] == "2026-08-01T02:00:00.000Z"
    assert first["items"][0]["endTime"] == "2026-08-01T03:00:00.000Z"
    assert first["items"][2]["endTime"] == "2026-08-01T04:30:00.000Z"

    second = _query(client, NVR, NVR_CH1)
    assert second["status"] == "SUCCEEDED"
    assert [i["startTime"] for i in second["items"]] == [i["startTime"] for i in first["items"]]

    # 恢复 UTC：结果回到无偏移基线，确认偏移可控。
    _configure(client, NVR, timeOffsetSeconds=0)
    restored = _query(client, NVR, NVR_CH1)
    assert restored["items"][0]["startTime"] == QUERY_START


# ---------------------------------------------------------------- 幂等


def test_idempotency_key_reuses_same_query(client: httpx.Client) -> None:
    """相同 Idempotency-Key 复用同一查询，且设备侧只收到一次查询。"""
    key = uuid.uuid4().hex
    first = _query(client, NVR, NVR_CH1, idem_key=key)
    assert first["status"] == "SUCCEEDED"
    assert len(first["items"]) == 3

    resp = client.post(
        "/provider/v1/device-record-queries",
        headers={"Idempotency-Key": key},
        json={
            "externalDeviceId": NVR,
            "externalChannelId": NVR_CH1,
            "startTime": QUERY_START,
            "endTime": QUERY_END,
            "recordType": "ALL",
        },
    )
    reused = data_of(resp, expected_status=202)
    assert reused["queryId"] == first["queryId"]

    status = data_of(client.get(f"/testkit/v1/devices/{NVR}/recordinfo"))
    assert status["queriesReceived"] == 1  # 幂等复用不产生第二次设备查询


# ---------------------------------------------------------------- reset 与 SIP seam


def test_recordinfo_scenario_reset_restores_default(client: httpx.Client) -> None:
    """reset 恢复默认录像场景，查询恢复正常。"""
    _configure(client, NVR, mode="timeout")
    client.post("/testkit/v1/reset")
    result = _query(client, NVR, NVR_CH1)
    assert result["status"] == "SUCCEEDED"
    assert len(result["items"]) == 3


def test_recordinfo_uses_real_sip_messages(client: httpx.Client) -> None:
    """SIP seam 证据：Registrar 收到 RecordInfo 响应 MESSAGE，设备侧有查询/响应统计。"""
    _configure(client, NVR, mode="multi", pageSize=2)
    _query(client, NVR, NVR_CH1)

    messages = data_of(client.get("/testkit/v1/sip/registrar/requests"))["items"]
    recordinfo_responses = [
        m
        for m in messages
        if m.get("method") == "MESSAGE"
        and (m.get("contentType") or "").lower() == "application/manscdp+xml"
    ]
    assert len(recordinfo_responses) >= 2  # 分页至少 2 条响应

    status = data_of(client.get(f"/testkit/v1/devices/{NVR}/recordinfo"))
    assert status["queriesReceived"] >= 1
    assert status["responsesSent"] >= 2
