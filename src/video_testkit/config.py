"""环境配置与启动校验。

所有配置通过 ``VIDEO_TESTKIT_`` 前缀环境变量注入，启动时由
pydantic-settings 完成类型与取值校验，避免运行时才发现拼写/类型错误。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderType = Literal["WVP", "SIPGO_GATEWAY", "MOCK"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIDEO_TESTKIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 身份与版本 ----
    provider_instance_code: str = Field(default="testkit-main", min_length=1, max_length=64)
    provider_type: ProviderType = "MOCK"
    contract_version: str = "1.0.0-draft.1"
    implementation_version: str = "0.1.0"

    # ---- HTTP 监听 ----
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # ---- 服务认证（生产必须设置；留空表示开发模式显式关闭认证）----
    auth_token: str | None = None

    # ---- 内置 Fake SIP Registrar ----
    registrar_enabled: bool = True
    registrar_host: str = "127.0.0.1"
    registrar_port: int = Field(default=15060, ge=1, le=65535)

    # ---- GB28181 Device Simulator ----
    # 设备向哪个 Registrar 注册；留空时使用内置 Registrar 地址。
    gb_registrar_addr: str = ""
    gb_password: str = "12345678"
    gb_realm: str = "3402000000"
    # 注册有效期秒数；下限 1 秒以支持确定性短界限测试，不等待真实 3600 秒。
    gb_expires: int = Field(default=3600, ge=1, le=86400)
    # 到期前提前多少秒自动发起 REGISTER 刷新（后台维护循环）。
    gb_refresh_margin: float = Field(default=2.0, gt=0, le=300)
    gb_register_timeout: float = Field(default=3.0, gt=0, le=30)

    # ---- Provider 事件投递 ----
    events_callback_url: str | None = None
    events_retry_base_delay: float = Field(default=0.1, gt=0, le=5)
    events_max_attempts: int = Field(default=3, ge=1, le=10)

    # ---- Fake 媒体引用基础地址（URL 不携带任何 secret）----
    media_base_url: str = "http://127.0.0.1:8080"

    @field_validator("events_callback_url")
    @classmethod
    def _validate_callback_scheme(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("events_callback_url 必须以 http:// 或 https:// 开头")
        return value

    @property
    def registrar_addr(self) -> str:
        """内置 Registrar 的 host:port。"""
        return f"{self.registrar_host}:{self.registrar_port}"

    @property
    def effective_gb_registrar_addr(self) -> str:
        """Device Simulator 实际注册目标。"""
        return self.gb_registrar_addr or self.registrar_addr

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    def validate_startup(self) -> None:
        """启动校验：所有绑定目标必须可解析、取值必须自洽。"""
        self._validate_addr(self.registrar_addr, "registrar_addr")
        self._validate_addr(self.effective_gb_registrar_addr, "effective_gb_registrar_addr")
        if not self.registrar_enabled and not self.gb_registrar_addr:
            raise ValueError(
                "registrar_enabled=false 且未配置 gb_registrar_addr：设备模拟器没有注册目标"
            )
        if self.auth_enabled and len(self.auth_token or "") < 8:
            raise ValueError("auth_token 长度必须至少 8 个字符")

    @staticmethod
    def _validate_addr(addr: str, field_name: str) -> None:
        host, port = Settings._split_addr(addr)
        if not host:
            raise ValueError(f"{field_name} 缺少 host")
        if not (1 <= port <= 65535):
            raise ValueError(f"{field_name} 端口非法: {port}")

    @staticmethod
    def _split_addr(addr: str) -> tuple[str, int]:
        host_part, _, port_part = addr.rpartition(":")
        if not port_part.isdigit():
            raise ValueError(f"地址必须为 host:port 形式: {addr!r}")
        return host_part, int(port_part)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_startup()
    return settings
