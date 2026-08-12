"""inventory snapshotToken 与 reconciliation（VT-11）：真实 HTTP 验证。

公共 seam：``/provider/v1/devices`` 与 ``/devices/{id}/channels`` 的
``snapshotToken`` 查询参数与响应字段、409 过期错误；测试不调用私有实现。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of

NVR = "34020000001320000001"


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield
    client.post("/testkit/v1/reset")


def _devices(client: httpx.Client, **params) -> dict:
    resp = client.get("/provider/v1/devices", params=params)
    assert resp.status_code == 200, resp.text
    return data_of(resp)


# ---------------------------------------------------------------- 快照轮次


def test_first_page_returns_snapshot_token(client: httpx.Client) -> None:
    """首次请求开启快照轮次并返回 token；后续页回传同一 token 继续。"""
    first = _devices(client)
    token = first["snapshotToken"]
    assert token  # 契约 x-required-from-contract
    second = _devices(client, snapshotToken=token, page=1)
    assert second["snapshotToken"] == token
    assert second["total"] == first["total"] >= 1


def test_unknown_snapshot_token_409_retryable(client: httpx.Client) -> None:
    """未知/过期 token：409 VIDEO_CATALOG_SNAPSHOT_EXPIRED 且 retryable=true。"""
    resp = client.get("/provider/v1/devices", params={"snapshotToken": uuid.uuid4().hex})
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "VIDEO_CATALOG_SNAPSHOT_EXPIRED"
    assert error["retryable"] is True


def test_stale_token_after_catalog_change_409(client: httpx.Client) -> None:
    """目录内容变化（设备状态 revision 递增）后旧 token 过期。"""
    first = _devices(client)
    token = first["snapshotToken"]

    # 触发目录指纹变化：设备状态 revision 递增
    client.post(f"/testkit/v1/devices/{NVR}/status", json={"onlineStatus": "OFFLINE"})

    resp = client.get("/provider/v1/devices", params={"snapshotToken": token})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VIDEO_CATALOG_SNAPSHOT_EXPIRED"

    # 开启新轮次可继续（reconciliation 整轮重启语义）
    fresh = _devices(client)
    assert fresh["snapshotToken"] != token


def test_channel_page_snapshot_round(client: httpx.Client) -> None:
    """通道分页同样返回/接受 snapshotToken；设备级轮次独立。"""
    first = _devices(client)
    device_token = first["snapshotToken"]

    resp = client.get(f"/provider/v1/devices/{NVR}/channels")
    assert resp.status_code == 200
    channels = data_of(resp)
    channel_token = channels["snapshotToken"]
    assert channel_token
    assert channel_token != device_token  # 设备级与通道级轮次独立

    again = data_of(
        client.get(
            f"/provider/v1/devices/{NVR}/channels",
            params={"snapshotToken": channel_token},
        )
    )
    assert again["snapshotToken"] == channel_token


def test_reset_clears_snapshot_rounds(client: httpx.Client) -> None:
    """reset 后旧 token 过期（409），新轮次可开启。"""
    token = _devices(client)["snapshotToken"]
    client.post("/testkit/v1/reset")

    resp = client.get("/provider/v1/devices", params={"snapshotToken": token})
    assert resp.status_code == 409
    assert _devices(client)["snapshotToken"]
