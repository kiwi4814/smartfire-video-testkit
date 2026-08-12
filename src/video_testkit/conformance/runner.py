"""Conformance Runner：执行共同契约测试套件并出具报告。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from video_testkit.conformance.bundle import ContractBundle, ContractValidationError
from video_testkit.conformance.cases import ALL_CASES
from video_testkit.conformance.report import ConformanceReport, TestResult

logger = logging.getLogger(__name__)

# 脱敏：报告中禁止出现的头字段（token/凭据/授权信息不落盘）。
_REDACTED_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def redact_trace(request: httpx.Request, response: httpx.Response | None) -> dict[str, Any]:
    """生成脱敏 HTTP 证据：方法/路径/状态码/非敏感头，不含 token 与报文体。"""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _REDACTED_HEADERS and k.lower() not in ("x-request-id",)
    }
    entry: dict[str, Any] = {
        "method": request.method,
        "path": request.url.path,
        "headers": headers,
    }
    if response is not None:
        entry["statusCode"] = response.status_code
    return entry


class ConformanceRunner:
    """Provider 黑盒 Conformance Runner。"""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        contract_version: str = "1.0.0-draft.1",
        bundle_path: str | Path | None = None,
        provider_type: str | None = None,
        timeout: float = 10.0,
        scenario: str = "",
        seed: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.contract_version = contract_version
        self.bundle_path = bundle_path
        self.override_provider_type = provider_type
        self.timeout = timeout
        self.scenario = scenario
        self.seed = seed

    def run(self) -> ConformanceReport:
        test_run_id = f"run-{uuid.uuid4().hex[:12]}"
        start_dt = datetime.now(UTC)
        start_time_iso = start_dt.isoformat().replace("+00:00", "Z")
        start_mono = time.monotonic()

        logger.info(f"Starting Conformance Run {test_run_id} against {self.base_url}")

        # 1. 加载并校验 Contract Bundle
        bundle = ContractBundle.load(
            bundle_path=self.bundle_path, expected_version=self.contract_version
        )

        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # 2. 基础 client（event hooks 记录脱敏痕迹供失败证据引用）
        traces: list[dict[str, Any]] = []

        def on_request(request: httpx.Request) -> None:
            traces.append(redact_trace(request, None))

        def on_response(response: httpx.Response) -> None:
            if traces:
                traces[-1]["statusCode"] = response.status_code

        with httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            event_hooks={"request": [on_request], "response": [on_response]},
        ) as client:
            # 3. 探查 /info 和 /capabilities（连接不可达时稳定失败而非崩栈）
            try:
                info_resp = client.get("/info")
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"Provider 不可达: {self.base_url} ({type(exc).__name__})"
                ) from exc
            info_data = info_resp.json().get("data", {}) if info_resp.status_code == 200 else {}

            provider_type = (
                self.override_provider_type or info_data.get("providerType") or "UNKNOWN"
            )
            provider_instance_code = info_data.get("providerInstanceCode", "unknown")
            implementation_version = info_data.get("implementationVersion", "unknown")
            build_commit = info_data.get("buildCommit", "unknown")

            cap_resp = client.get("/capabilities")
            cap_data = (
                cap_resp.json().get("data", {}).get("capabilities", [])
                if cap_resp.status_code == 200
                else []
            )

            supported_caps = {c.get("code"): c.get("supported", False) for c in cap_data}

            # 4. 执行用例矩阵
            context: dict[str, Any] = {}
            results: list[TestResult] = []

            for case in ALL_CASES:
                case_start = time.monotonic()
                req_cap = case.required_capability

                if req_cap and not supported_caps.get(req_cap, False):
                    duration_ms = (time.monotonic() - case_start) * 1000.0
                    results.append(
                        TestResult(
                            test_id=case.test_id,
                            name=case.name,
                            category=case.category,
                            status="SKIPPED",
                            duration_ms=duration_ms,
                            required_capability=req_cap,
                            skip_reason=f"Capability '{req_cap}' is not supported by Provider",
                        )
                    )
                    continue

                try:
                    case.run(client, bundle, context)
                    duration_ms = (time.monotonic() - case_start) * 1000.0
                    results.append(
                        TestResult(
                            test_id=case.test_id,
                            name=case.name,
                            category=case.category,
                            status="PASSED",
                            duration_ms=duration_ms,
                            required_capability=req_cap,
                        )
                    )
                except ContractValidationError as e:
                    duration_ms = (time.monotonic() - case_start) * 1000.0
                    results.append(
                        TestResult(
                            test_id=case.test_id,
                            name=case.name,
                            category=case.category,
                            status="FAILED",
                            duration_ms=duration_ms,
                            required_capability=req_cap,
                            error_details={
                                "operationId": e.operation_id,
                                "requestId": e.request_id,
                                "expected": e.expected,
                                "actual": str(e.actual),
                                "message": e.message,
                            },
                            evidence=list(traces),
                        )
                    )
                except Exception as e:
                    duration_ms = (time.monotonic() - case_start) * 1000.0
                    results.append(
                        TestResult(
                            test_id=case.test_id,
                            name=case.name,
                            category=case.category,
                            status="FAILED",
                            duration_ms=duration_ms,
                            required_capability=req_cap,
                            error_details={
                                "operationId": "unknown",
                                "requestId": "unknown",
                                "expected": "Successful assertion",
                                "actual": type(e).__name__,
                                "message": str(e),
                            },
                            evidence=list(traces),
                        )
                    )

        end_dt = datetime.now(UTC)
        end_time_iso = end_dt.isoformat().replace("+00:00", "Z")
        duration_sec = time.monotonic() - start_mono

        return ConformanceReport(
            test_run_id=test_run_id,
            contract_version=self.contract_version,
            contract_checksum=bundle.checksum,
            provider_type=provider_type,
            provider_instance_code=provider_instance_code,
            implementation_version=implementation_version,
            build_commit=build_commit,
            start_time=start_time_iso,
            end_time=end_time_iso,
            duration_seconds=duration_sec,
            capabilities=cap_data,
            results=results,
            scenario=self.scenario,
            seed=self.seed,
        )
