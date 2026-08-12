"""共享测试基础设施：真实 uvicorn 线程（lifespan 生效）、动态端口、干净停止。

- 测试只经真实 HTTP 与 UDP：不调用任何私有方法。
- 端口动态分配：HTTP 与 UDP Registrar 端口均通过 bind(0) 探测空闲端口。
- 停止方式：置 ``uvicorn.Server.should_exit``（公开属性）后 join 线程，触发
  lifespan shutdown（Registrar 停止、事件 worker 取消），随后 fixture teardown 兜底。
- 轮询辅助：deadline + 动态退避（0.05s 起、每轮加倍、封顶 0.2s），有界且无固定长 sleep。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import httpx
import pytest
import uvicorn

from video_testkit.app import create_app
from video_testkit.config import Settings

T = TypeVar("T")


# ---------------------------------------------------------------- 有界轮询辅助


def data_of(resp: httpx.Response, expected_status: int = 200) -> dict[str, Any]:
    """断言 HTTP 状态码并返回统一 envelope 的 ``data``。"""
    assert resp.status_code == expected_status, f"status={resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("requestId"), "响应缺少 requestId"
    return body["data"]


def wait_until(predicate: Callable[[], bool], timeout: float, base_interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    interval = base_interval
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
        interval = min(interval * 2, 0.2)
    raise AssertionError(f"等待超时（{timeout:.1f}s）：条件未满足")


def wait_until_value(
    get_value: Callable[[], T],
    predicate: Callable[[T], bool],
    timeout: float,
    base_interval: float = 0.05,
) -> T:
    deadline = time.monotonic() + timeout
    interval = base_interval
    last: Any = None
    while time.monotonic() < deadline:
        value = get_value()
        if predicate(value):
            return value
        last = value
        time.sleep(interval)
        interval = min(interval * 2, 0.2)
    raise AssertionError(f"等待超时（{timeout:.1f}s）：最后值 {last!r}")


def free_port(sock_type: int = socket.SOCK_STREAM) -> int:
    """动态分配空闲端口：bind(0) 读取系统分配值后释放。"""
    with socket.socket(socket.AF_INET, sock_type) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------- 真实 uvicorn 线程


class ServerHandle:
    """在线程中运行真实 uvicorn（lifespan 完整生效），并提供干净的启停。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = f"http://127.0.0.1:{settings.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 15.0) -> None:
        app = create_app(self.settings)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.settings.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(target=server.run, name="testkit-uvicorn", daemon=True)
        self._thread.start()
        wait_until(self._tcp_ready, timeout=timeout)
        with httpx.Client(base_url=self.base_url, timeout=2.0) as c:
            assert c.get("/").status_code == 200, "HTTP 冒烟探测失败"

    def _tcp_ready(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", self.settings.port))
                return True
            except OSError:
                return False

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError("uvicorn 线程未能在期限内停止")
        self._server = None
        self._thread = None


def _base_settings(port: int) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=port,
        registrar_host="127.0.0.1",
        registrar_port=free_port(socket.SOCK_DGRAM),
        gb_registrar_addr="",
        gb_password="12345678",
        gb_realm="3402000000",
        auth_token=None,
        events_callback_url=None,
        # 普通 fixture 显式关闭 ZLM 集成，避免继承 VIDEO_TESTKIT_ZLM_API_URL 环境变量
        # 导致 mock 行为被意外切换（仅 zlm_settings() 构造的专用 fixture 启用）。
        zlm_api_url="",
    )


@pytest.fixture(scope="session")
def server() -> Iterator[ServerHandle]:
    handle = ServerHandle(_base_settings(free_port()))
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def client(server: ServerHandle) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server.base_url, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="session")
def auth_server() -> Iterator[ServerHandle]:
    settings = _base_settings(free_port())
    settings.auth_token = "test-token-123"
    handle = ServerHandle(settings)
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def auth_client(auth_server: ServerHandle) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=auth_server.base_url, timeout=10.0) as c:
        yield c


# ---------------------------------------------------------------- ZLM 集成冒烟


def zlm_settings(port: int) -> Settings:
    """构造启用 ZLM 集成的 Settings（环境变量指向真实 ZLM）。"""
    settings = _base_settings(port)
    settings.zlm_api_url = os.environ.get("VIDEO_TESTKIT_ZLM_API_URL", "")
    settings.zlm_api_secret = os.environ.get("VIDEO_TESTKIT_ZLM_API_SECRET", "")
    settings.zlm_rtp_host = "127.0.0.1"
    settings.zlm_rtp_port_range = (21001, 21036)
    settings.zlm_stream_online_timeout = 5.0
    return settings


@pytest.fixture(scope="session")
def zlm_server() -> Iterator[ServerHandle]:
    """真实 ZLM 集成环境（未配置 VIDEO_TESTKIT_ZLM_API_URL 时整体 skip）。"""
    api_url = os.environ.get("VIDEO_TESTKIT_ZLM_API_URL", "")
    if not api_url:
        pytest.skip("VIDEO_TESTKIT_ZLM_API_URL 未配置，跳过 ZLM 集成冒烟")
    handle = ServerHandle(zlm_settings(free_port()))
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def zlm_client(zlm_server: ServerHandle) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=zlm_server.base_url, timeout=10.0) as c:
        yield c
