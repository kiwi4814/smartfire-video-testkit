"""Provider 共同契约 Conformance Runner 测试套件。"""

from __future__ import annotations

import json
import tempfile

import pytest
from conftest import ServerHandle

from video_testkit.conformance.bundle import ContractBundle, ContractValidationError
from video_testkit.conformance.runner import ConformanceRunner


def test_conformance_runner_against_fake_provider(server: ServerHandle) -> None:
    """测试 ConformanceRunner 能够成功校验进程内 Fake Provider。"""
    base_url = f"{server.base_url}/provider/v1"
    runner = ConformanceRunner(base_url=base_url, scenario="four-channel NVR + IPC", seed="test-42")
    report = runner.run()

    data = report.to_json_dict()
    summary = data["summary"]

    assert summary["total"] >= 15
    assert summary["failed"] == 0
    assert summary["passed"] > 0
    assert data["contractVersion"] == "1.0.0-draft.1"
    assert data["providerType"] == "MOCK"
    # VT-10：报告标识包含 scenario/seed；mandatory/gated 分开统计；结论明确。
    assert data["scenario"] == "four-channel NVR + IPC"
    assert data["seed"] == "test-42"
    assert summary["mandatoryTotal"] >= 1
    assert summary["mandatoryFailed"] == 0
    assert summary["capabilityGatedTotal"] >= 1
    assert data["conclusion"].startswith("Simulator Conformance")
    assert "Vendor Compatibility" in data["conclusion"]

    with tempfile.TemporaryDirectory() as tmp_dir:
        json_file, xml_file, md_file = report.save(tmp_dir)
        assert json_file.exists()
        assert xml_file.exists()
        assert md_file.exists()
        assert "<testsuites" in xml_file.read_text(encoding="utf-8")
        md_text = md_file.read_text(encoding="utf-8")
        assert "Provider Contract Conformance Report" in md_text
        assert "Mandatory:" in md_text
        assert "Conclusion" in md_text
        assert "test-42" in md_text


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


def test_failed_report_evidence_redacted_and_attached(auth_server: ServerHandle) -> None:
    """失败用例带脱敏证据：包含方法/路径/状态码，且绝不含 Authorization token。"""
    base_url = f"{auth_server.base_url}/provider/v1"
    runner = ConformanceRunner(base_url=base_url)  # 无 token → 401 失败
    report = runner.run()
    data = report.to_json_dict()
    failed = [r for r in data["results"] if r["status"] == "FAILED"]
    assert failed, "无 token 场景必须存在失败用例"
    failed_with_evidence = [r for r in failed if r.get("evidence")]
    assert failed_with_evidence, "失败用例必须携带脱敏证据"
    serialized = json.dumps(data, ensure_ascii=False)
    assert "test-token-123" not in serialized  # 凭据绝不落盘
    assert "authorization" not in serialized.lower()  # 敏感头字段不出现
    evidence = failed_with_evidence[0]["evidence"][0]
    assert "method" in evidence and "path" in evidence


def test_cli_conformance_runs_and_gate_exit_codes(server: ServerHandle) -> None:
    """CLI conformance 子命令：单次运行退出码 0；重复运行产出 run-N 子目录。"""
    import subprocess
    import sys
    import tempfile

    base_url = f"{server.base_url}/provider/v1"
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 单次运行：成功 → exit 0
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "video_testkit",
                "conformance",
                "--base-url",
                base_url,
                "--report-dir",
                tmp_dir,
                "--runs",
                "2",
                "--scenario",
                "cli-gate",
                "--seed",
                "seed-1",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        # 多次运行产出 run-N 子目录与三份报告
        run1 = f"{tmp_dir}/run-1"
        assert (f"{run1}/conformance-report.json").endswith("run-1/conformance-report.json")
        md = open(f"{run1}/conformance-report.md", encoding="utf-8").read()
        assert "cli-gate" in md and "seed-1" in md


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
