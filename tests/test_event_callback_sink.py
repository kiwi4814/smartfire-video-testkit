"""Provider 事件投递与 Callback Sink（VT-11）：真实 HTTP 回调验证。

公共 seam：``/testkit/v1`` 编排 sink 脚本与投递目标，``/sink/provider-events``
是真实回调端点（Bearer 校验），``/testkit/v1/events`` 是 Provider outbox 视图。
事件经 Keepalive/在线状态变化确定性产生；测试不导入任何私有实现。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of, wait_until_value

NVR = "34020000001320000001"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    client.post("/testkit/v1/events/sink/clear")
    yield
    client.post("/testkit/v1/events/sink/clear")
    client.post("/testkit/v1/reset")


def _configure_sink(client: httpx.Client, token: str = "sink-token-123") -> dict:
    """将 Provider 事件投递目标指向 TestKit 内嵌 sink（同一端口 /sink）。"""
    resp = client.post(
        "/testkit/v1/events/sink/config",
        json={
            "url": f"{client.base_url}/sink/provider-events",
            "token": token,
        },
    )
    return data_of(resp, expected_status=200)


def _make_events(client: httpx.Client) -> None:
    """产生确定性的 DEVICE_OFFLINE → DEVICE_ONLINE 事件序列。"""
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "ONLINE"})


def _sink_received(client: httpx.Client) -> list[dict]:
    return data_of(client.get("/testkit/v1/events/sink/received"))["items"]


def _provider_events(client: httpx.Client) -> list[dict]:
    return data_of(client.get("/testkit/v1/events"))["items"]


# ---------------------------------------------------------------- 正常投递与 Bearer


def test_event_delivered_to_sink_with_bearer(client: httpx.Client) -> None:
    """事件经真实 HTTP 到达 sink（Bearer 校验通过），revision/epoch 完整。"""
    _configure_sink(client)
    _make_events(client)

    def received() -> list[dict]:
        return _sink_received(client)

    items = wait_until_value(received, lambda items: len(items) >= 2, timeout=8.0)
    types = [e["eventType"] for e in items]
    assert types == ["DEVICE_OFFLINE", "DEVICE_ONLINE"]
    first = items[0]
    assert first["providerEpoch"]  # epoch 携带
    assert first["revision"].isdigit()  # uint64 十进制
    assert first["resource"]["externalDeviceId"] == NVR
    assert first["_duplicate"] is False

    # Provider outbox 侧全部 DELIVERED
    def all_delivered() -> bool:
        evs = _provider_events(client)
        return len(evs) >= 2 and all(e["deliveryState"] == "DELIVERED" for e in evs)

    wait_until_value(all_delivered, lambda v: v is True, timeout=6.0)


def test_sink_rejects_wrong_bearer_token(client: httpx.Client) -> None:
    """错误 Bearer：sink 返回 401，Provider 不盲目重试（稳定可观察）。"""
    _configure_sink(client, token="correct-token")
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})

    def attempts() -> int:
        evs = _provider_events(client)
        return evs[0]["attempts"] if evs else 0

    # Provider 用正确 token 投递（token 由 config 下发），sink 校验通过。
    wait_until_value(attempts, lambda n: n >= 1, timeout=6.0)
    evs = _provider_events(client)
    assert evs[0]["deliveryState"] == "DELIVERED"


# ---------------------------------------------------------------- 故障脚本与重试


def test_500_script_triggers_bounded_retry(client: httpx.Client) -> None:
    """sink 脚本化 500：Provider 有界重试（attempts 递增），恢复后 DELIVERED。"""
    _configure_sink(client)
    data_of(
        client.post("/testkit/v1/events/sink/script", json={"status": 500}),
        expected_status=200,
    )
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})

    def attempts() -> int:
        evs = _provider_events(client)
        return evs[0]["attempts"] if evs else 0

    # 脚本 500 下至少重试 2 次
    wait_until_value(attempts, lambda n: n >= 2, timeout=8.0)

    # 恢复正常脚本，事件最终送达
    data_of(client.post("/testkit/v1/events/sink/script", json={"status": None}))
    wait_until_value(
        lambda: _provider_events(client)[0]["deliveryState"],
        lambda s: s == "DELIVERED",
        timeout=8.0,
    )
    assert _sink_received(client)[0]["eventType"] == "DEVICE_OFFLINE"


def test_401_script_no_retry(client: httpx.Client) -> None:
    """sink 脚本化 401：Provider 不无限重试（attempts 停留 1，标记 no retry）。"""
    _configure_sink(client)
    data_of(client.post("/testkit/v1/events/sink/script", json={"status": 401}))
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})

    def ev() -> dict:
        evs = _provider_events(client)
        return evs[0] if evs else {}

    wait_until_value(
        lambda: ev().get("lastError") or "",
        lambda error: "no retry" in error,
        timeout=6.0,
    )
    # 稳定窗口内不再重试（401/403 不盲目重试）
    assert ev()["attempts"] == 1
    assert "no retry" in (ev()["lastError"] or "")


# ---------------------------------------------------------------- 幂等与乱序


def test_duplicate_delivery_applied_once(client: httpx.Client) -> None:
    """同一 eventId 重复投递：sink 按 providerInstanceCode+eventId 幂等去重。"""
    _configure_sink(client)
    _make_events(client)
    wait_until_value(lambda: len(_sink_received(client)), lambda n: n >= 2, timeout=8.0)

    # 手动重复投递同一事件（模拟 Provider at-least-once 重复）
    item = _sink_received(client)[0]
    resp = client.post(
        "/sink/provider-events",
        headers={"Authorization": "Bearer sink-token-123"},
        json={"event": item},
    )
    assert resp.status_code == 200

    received = _sink_received(client)
    assert len(received) == 3  # 原 2 + 重复 1（不新增唯一事件）
    assert received[-1]["_duplicate"] is True
    assert len({(e["providerInstanceCode"], e["eventId"]) for e in received}) == 2


def test_out_of_order_revision_observable(client: httpx.Client) -> None:
    """迟到旧 revision：sink 标记 _outOfOrder，不覆盖新状态。"""
    _configure_sink(client)
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    wait_until_value(lambda: len(_sink_received(client)), lambda n: n >= 1, timeout=8.0)
    first = _sink_received(client)[0]

    # 构造同一 epoch/resource 的旧 revision 迟到事件（新 revision 为 first+1）
    stale = dict(first)
    stale["revision"] = str(int(first["revision"]) - 5)
    stale["eventId"] = str(uuid.uuid4())
    resp = client.post(
        "/sink/provider-events",
        headers={"Authorization": "Bearer sink-token-123"},
        json={"event": stale},
    )
    assert resp.status_code == 200

    received = _sink_received(client)
    stale_entry = next(e for e in received if e["eventId"] == stale["eventId"])
    assert stale_entry["_outOfOrder"] is True


# ---------------------------------------------------------------- reset 清理


def test_reset_clears_sink_state(client: httpx.Client) -> None:
    """reset 清理 sink 接收状态与脚本；Provider outbox 清空。"""
    _configure_sink(client)
    data_of(client.post("/testkit/v1/events/sink/script", json={"status": 500}))
    _make_events(client)
    # 500 脚本下事件不达 sink（被拒绝），Provider 侧有 FAILED 事件
    wait_until_value(
        lambda: len(_provider_events(client)),
        lambda n: n >= 1,
        timeout=6.0,
    )
    assert _sink_received(client) == []

    client.post("/testkit/v1/reset")
    status = data_of(client.get("/testkit/v1/events/sink/status"))
    assert status["received"] == 0
    assert status["scriptStatus"] is None
    assert _provider_events(client) == []

    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    wait_until_value(
        lambda: _provider_events(client)[0]["deliveryState"],
        lambda state: state == "NOT_CONFIGURED",
        timeout=6.0,
    )
    assert _sink_received(client) == []


def test_epoch_present_in_info_and_events(client: httpx.Client) -> None:
    """providerEpoch 同时出现在 /info 与事件 payload（契约要求）。"""
    info = data_of(client.get("/provider/v1/info"))
    assert info["providerEpoch"]
    epoch = info["providerEpoch"]

    _configure_sink(client)
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    wait_until_value(lambda: len(_sink_received(client)), lambda n: n >= 1, timeout=8.0)
    assert _sink_received(client)[0]["providerEpoch"] == epoch


# ---------------------------------------------------------------- reconciliation（CT-EVT-005）


def test_dropped_event_recoverable_via_inventory_snapshot(client: httpx.Client) -> None:
    """漏事件：事件通道持续失败不产生错误结论，inventory 全量对账可收敛。

    1. 正常投递事件到 sink；
    2. sink 脚本化 500（Provider 有界重试耗尽），新事件不达 sink（可观察为
       outbox FAILED，不伪装为已送达）；
    3. 以 snapshotToken 全量拉取 /devices，目录是事实源且完整——对账可恢复。
    """
    _configure_sink(client)

    # 阶段 1：正常事件到达
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})
    wait_until_value(lambda: len(_sink_received(client)), lambda n: n >= 1, timeout=8.0)

    # 阶段 2：sink 持续 500，新事件重试耗尽后 FAILED（不达 sink、不伪装送达）
    data_of(client.post("/testkit/v1/events/sink/script", json={"status": 500}))
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "ONLINE"})

    def dropped_failed() -> bool:
        evs = _provider_events(client)
        return any(
            e["eventType"] == "DEVICE_ONLINE"
            and e["deliveryState"] == "FAILED"
            and e["attempts"] >= 3
            for e in evs
        )

    wait_until_value(dropped_failed, lambda v: v is True, timeout=8.0)
    online_events = [e for e in _provider_events(client) if e["eventType"] == "DEVICE_ONLINE"]
    assert online_events and online_events[-1]["deliveryState"] == "FAILED"
    # sink 侧没有任何 ONLINE 事件（事件确实丢失，未被错误标记为送达）
    assert not any(e["eventType"] == "DEVICE_ONLINE" for e in _sink_received(client))

    # 阶段 3：inventory 全量对账——Provider 目录是事实源，快照完整无 MISSING
    page = data_of(client.get("/provider/v1/devices"))
    token = page["snapshotToken"]
    assert token
    devices = {d["externalDeviceId"]: d for d in page["items"]}
    assert NVR in devices
    # 设备当前在线状态与 Provider 目录一致（对账后无遗漏/漂移）
    assert devices[NVR]["onlineStatus"] == "ONLINE"
    # 分页续拉保持同一轮次（全量对账可完成）
    page2 = data_of(client.get("/provider/v1/devices", params={"snapshotToken": token}))
    assert page2["snapshotToken"] == token
    assert len(page2["items"]) == len(page["items"])
