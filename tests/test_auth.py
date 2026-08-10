"""认证：AUTH_TOKEN 启用时 Provider 与控制面均需 Bearer；根路由不受保护。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import data_of

TOKEN = "test-token-123"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset(auth_client: httpx.Client) -> Iterator[None]:
    auth_client.post("/testkit/v1/reset", headers=AUTH_HEADERS)
    yield


def test_unauthenticated_401(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/provider/v1/health/live")
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["code"] == "VIDEO_PROVIDER_AUTH_FAILED"
    assert error["retryable"] is False


def test_wrong_token_401(auth_client: httpx.Client) -> None:
    resp = auth_client.get(
        "/provider/v1/health/live", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


def test_authenticated_ok(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/provider/v1/health/live", headers=AUTH_HEADERS)
    assert data_of(resp) == {"status": "UP"}


def test_testkit_control_plane_requires_auth(auth_client: httpx.Client) -> None:
    assert auth_client.get("/testkit/v1/scenario").status_code == 401
    resp = auth_client.get("/testkit/v1/scenario", headers=AUTH_HEADERS)
    assert resp.status_code == 200


def test_root_index_unprotected(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "/provider/v1"
    assert resp.json()["testkit"] == "/testkit/v1"
