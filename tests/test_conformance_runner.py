"""Provider 共同契约 Conformance Runner 测试套件。"""

from __future__ import annotations

import tempfile

import pytest
from conftest import ServerHandle

from video_testkit.conformance.bundle import ContractBundle, ContractValidationError
from video_testkit.conformance.runner import ConformanceRunner


def test_conformance_runner_against_fake_provider(server: ServerHandle) -> None:
    """测试 ConformanceRunner 能够成功校验进程内 Fake Provider。"""
    base_url = f"{server.base_url}/provider/v1"
    runner = ConformanceRunner(base_url=base_url)
    report = runner.run()

    data = report.to_json_dict()
    summary = data["summary"]

    assert summary["total"] >= 15
    assert summary["failed"] == 0
    assert summary["passed"] > 0
    assert data["contractVersion"] == "1.0.0-draft.1"
    assert data["providerType"] == "MOCK"

    with tempfile.TemporaryDirectory() as tmp_dir:
        json_file, xml_file = report.save(tmp_dir)
        assert json_file.exists()
        assert xml_file.exists()
        assert "<testsuites" in xml_file.read_text(encoding="utf-8")


def test_conformance_runner_against_auth_provider(auth_server: ServerHandle) -> None:
    """测试在 Bearer Token 启用下 ConformanceRunner 的正确性。"""
    base_url = f"{auth_server.base_url}/provider/v1"

    # 不带 Token，应当有失败用例
    unauth_runner = ConformanceRunner(base_url=base_url)
    unauth_report = unauth_runner.run()
    assert unauth_report.to_json_dict()["summary"]["failed"] > 0

    # 携带正确 Token，应当全部通过
    auth_runner = ConformanceRunner(base_url=base_url, token="test-token-123")
    auth_report = auth_runner.run()
    assert auth_report.to_json_dict()["summary"]["failed"] == 0


def test_schema_mismatch_detection() -> None:
    """当 required 字段缺失或类型损坏时，断言抛出包含关键元数据的 ContractValidationError。"""
    bundle = ContractBundle.load()

    # 构造缺失 required 字段 'requestId' 的 HealthResponse
    invalid_health_body = {"data": {"status": "UP"}}
    with pytest.raises(ContractValidationError) as exc_info:
        bundle.validate_response(
            operation_id="getProviderLiveness",
            status_code=200,
            body=invalid_health_body,
            request_id="req-test-123",
        )

    err = exc_info.value
    assert err.operation_id == "getProviderLiveness"
    assert err.request_id == "req-test-123"
    assert "requestId" in err.expected or "requestId" in err.message

    # 构造类型损坏的 HealthResponse (status 应该为 'UP')
    invalid_status_body = {"requestId": "req-456", "data": {"status": "INVALID_STATUS"}}
    with pytest.raises(ContractValidationError) as exc_info2:
        bundle.validate_response(
            operation_id="getProviderLiveness",
            status_code=200,
            body=invalid_status_body,
            request_id="req-456",
        )

    err2 = exc_info2.value
    assert err2.operation_id == "getProviderLiveness"
    assert err2.request_id == "req-456"
