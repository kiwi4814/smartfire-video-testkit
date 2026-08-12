"""Provider 公共契约：health / info / capabilities（真实 HTTP）。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of


@pytest.fixture(autouse=True)
def _reset(client: httpx.Client) -> Iterator[None]:
    client.post("/testkit/v1/reset")
    yield


def test_health_live(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/health/live")
    assert data_of(resp) == {"status": "UP"}


def test_health_ready_default(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/health/ready")
    assert resp.status_code == 200
    assert data_of(resp) == {"status": "READY"}


def test_request_id_echoed(client: httpx.Client) -> None:
    resp = client.get("/provider/v1/health/live", headers={"X-Request-Id": "req-echo-01"})
    body = resp.json()
    assert body["requestId"] == "req-echo-01"
    assert resp.headers["X-Request-Id"] == "req-echo-01"


def test_info(client: httpx.Client) -> None:
    data = data_of(client.get("/provider/v1/info"))
    assert data["providerType"] == "MOCK"
    assert data["providerInstanceCode"] == "testkit-main"
    assert data["contractVersion"] == "1.0.0-draft.1"
    assert data["implementationVersion"]
    assert data["buildCommit"]
    assert data["buildTime"].endswith("Z")
    assert data["protocolStack"] == "MOCK+SIP"
    assert data["recordTypesSupported"] == ["ALL", "TIME"]
    assert data["authEnabled"] is False


def test_capabilities(client: httpx.Client) -> None:
    data = data_of(client.get("/provider/v1/capabilities"))
    caps = {c["code"]: c["supported"] for c in data["capabilities"]}
    assert caps["DEVICE_DISCOVERY"] is True
    assert caps["CATALOG_SYNC"] is True
    assert caps["LIVE_STREAM"] is True
    assert caps["DEVICE_RECORD_QUERY"] is True
    assert caps["DEVICE_RECORD_PLAYBACK"] is True
    assert caps["PROVIDER_EVENTS"] is True
    # VT-09 可选能力：以契约允许的 constraints 声明（不新增枚举 code）。
    live = next(c for c in data["capabilities"] if c["code"] == "LIVE_STREAM")
    constraints = live["constraints"]
    assert constraints["codecs"] == ["H264", "H265"]
    assert constraints["audioCodecs"] == ["G711A"]
    assert constraints["signalingTransports"] == ["UDP", "TCP"]
    assert constraints["mediaTransports"] == ["UDP", "TCP"]
    assert caps["SNAPSHOT"] is False
    assert caps["PTZ"] is False
    assert caps["PLAYBACK_SEEK"] is False
