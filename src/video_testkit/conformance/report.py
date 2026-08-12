"""Conformance 报告生成器：JSON Summary、Standard JUnit XML 与简洁 Markdown。

发布语义（VT-10）：

- 报告标识 contract/checksum、Provider、implementation、scenario、seed 与 test run；
- mandatory（无 required_capability）与 capability-gated skip 分开统计；
- 结论只写 Simulator Conformance，不推断 Vendor Compatibility；
- 失败含脱敏 HTTP/SIP 证据引用（token/密码/完整 SIP 报文不落盘）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

# 发布结论：只声明 Simulator Conformance，不推断真实厂商兼容性。
CONFORMANCE_CONCLUSION = (
    "Simulator Conformance: the Provider satisfies the machine-readable contract "
    "as observed through the public HTTP seams. This does not imply Vendor "
    "Compatibility with any real camera, GB28181 gateway or platform."
)


@dataclass
class TestResult:
    test_id: str
    name: str
    category: str
    status: Literal["PASSED", "FAILED", "SKIPPED"]
    duration_ms: float
    required_capability: str | None = None
    skip_reason: str | None = None
    error_details: dict[str, Any] | None = None
    # VT-10：脱敏失败证据引用（HTTP 方法/路径/状态码/关键头，不含 token 与报文体）。
    evidence: list[dict[str, Any]] | None = None


@dataclass
class ConformanceReport:
    test_run_id: str
    contract_version: str
    contract_checksum: str
    provider_type: str
    provider_instance_code: str
    implementation_version: str
    build_commit: str
    start_time: str
    end_time: str
    duration_seconds: float
    capabilities: list[dict[str, Any]]
    results: list[TestResult] = field(default_factory=list)
    # VT-10：测试场景描述与确定性 seed（报告标识的一部分）。
    scenario: str = ""
    seed: str = ""

    def _summary(self) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.status == "PASSED")
        failed = sum(1 for r in self.results if r.status == "FAILED")
        skipped = sum(1 for r in self.results if r.status == "SKIPPED")
        # mandatory = 无 required_capability 的用例；capability-gated = 声明能力门槛。
        mandatory = [r for r in self.results if r.required_capability is None]
        gated = [r for r in self.results if r.required_capability is not None]
        return {
            "total": len(self.results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "mandatoryTotal": len(mandatory),
            "mandatoryPassed": sum(1 for r in mandatory if r.status == "PASSED"),
            "mandatoryFailed": sum(1 for r in mandatory if r.status == "FAILED"),
            "mandatorySkipped": sum(1 for r in mandatory if r.status == "SKIPPED"),
            "capabilityGatedTotal": len(gated),
            "capabilityGatedSkipped": sum(1 for r in gated if r.status == "SKIPPED"),
            "capabilityGatedPassed": sum(1 for r in gated if r.status == "PASSED"),
        }

    def to_json_dict(self) -> dict[str, Any]:
        skipped_optional = [
            {
                "testId": r.test_id,
                "name": r.name,
                "capability": r.required_capability,
                "reason": r.skip_reason or "Capability not supported by Provider",
            }
            for r in self.results
            if r.status == "SKIPPED"
        ]

        return {
            "testRunId": self.test_run_id,
            "contractVersion": self.contract_version,
            "contractChecksum": self.contract_checksum,
            "providerType": self.provider_type,
            "providerInstanceCode": self.provider_instance_code,
            "implementationVersion": self.implementation_version,
            "buildCommit": self.build_commit,
            "scenario": self.scenario,
            "seed": self.seed,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "durationSeconds": round(self.duration_seconds, 3),
            "summary": self._summary(),
            "capabilities": self.capabilities,
            "conclusion": CONFORMANCE_CONCLUSION,
            "skippedOptionalTests": skipped_optional,
            "results": [
                {
                    "testId": r.test_id,
                    "name": r.name,
                    "category": r.category,
                    "status": r.status,
                    "durationMs": round(r.duration_ms, 2),
                    "requiredCapability": r.required_capability,
                    "errorDetails": r.error_details,
                    "evidence": r.evidence,
                }
                for r in self.results
            ],
        }

    def to_junit_xml(self) -> str:
        failed = sum(1 for r in self.results if r.status == "FAILED")
        skipped = sum(1 for r in self.results if r.status == "SKIPPED")

        testsuites = ET.Element("testsuites", attrib={"name": "Provider Conformance Suite"})
        suite = ET.SubElement(
            testsuites,
            "testsuite",
            attrib={
                "name": "provider-contract-conformance",
                "tests": str(len(self.results)),
                "failures": str(failed),
                "skipped": str(skipped),
                "time": f"{self.duration_seconds:.3f}",
                "timestamp": self.start_time,
            },
        )

        for r in self.results:
            case = ET.SubElement(
                suite,
                "testcase",
                attrib={
                    "classname": f"provider_conformance.{r.category.lower()}",
                    "name": f"{r.test_id}: {r.name}",
                    "time": f"{r.duration_ms / 1000.0:.3f}",
                },
            )
            if r.status == "SKIPPED":
                skipped_elem = ET.SubElement(
                    case, "skipped", attrib={"message": r.skip_reason or "Skipped"}
                )
                skipped_elem.text = f"Required capability: {r.required_capability}"
            elif r.status == "FAILED":
                err_msg = "Test failed"
                err_text = ""
                if r.error_details:
                    err_msg = r.error_details.get("message", "Contract assertion failed")
                    err_text = json.dumps(r.error_details, ensure_ascii=False, indent=2)
                if r.evidence:
                    err_text = (err_text + "\n" if err_text else "") + json.dumps(
                        r.evidence, ensure_ascii=False, indent=2
                    )
                fail_elem = ET.SubElement(case, "failure", attrib={"message": err_msg})
                fail_elem.text = err_text

        res = ET.tostring(testsuites, encoding="utf-8").decode("utf-8")
        return str(res)

    def to_markdown(self) -> str:
        """简洁 Markdown 报告：标识、摘要（mandatory/gated 分开）、结论。"""
        s = self._summary()
        lines = [
            "# Provider Contract Conformance Report",
            "",
            f"- **Test Run** : `{self.test_run_id}`",
            f"- **Contract** : `{self.contract_version}` "
            f"(checksum `{self.contract_checksum[:12]}…`)",
            f"- **Provider** : `{self.provider_type}` (`{self.provider_instance_code}`)",
            f"- **Implementation** : `{self.implementation_version}` "
            f"(commit `{self.build_commit}`)",
            f"- **Scenario** : {self.scenario or 'default'} (seed `{self.seed or 'default'}`)",
            f"- **Duration** : {self.duration_seconds:.1f}s",
            "",
            "## Summary",
            "",
            f"- Total: **{s['total']}** | Passed: **{s['passed']}** | "
            f"Failed: **{s['failed']}** | Skipped: **{s['skipped']}**",
            f"- Mandatory: {s['mandatoryPassed']}/{s['mandatoryTotal']} passed "
            f"({s['mandatoryFailed']} failed, {s['mandatorySkipped']} skipped)",
            f"- Capability-gated: {s['capabilityGatedPassed']}/{s['capabilityGatedTotal']} passed "
            f"({s['capabilityGatedSkipped']} skipped by capability)",
            "",
            "## Failures",
            "",
        ]
        failures = [r for r in self.results if r.status == "FAILED"]
        if not failures:
            lines.append("None.")
        for r in failures:
            lines.append(f"- `{r.test_id}` {r.name}: {self._failure_line(r)}")
        lines += [
            "",
            "## Conclusion",
            "",
            CONFORMANCE_CONCLUSION,
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _failure_line(r: TestResult) -> str:
        if not r.error_details:
            return "unknown failure"
        op = r.error_details.get("operationId", "unknown")
        req_id = r.error_details.get("requestId", "unknown")
        message = r.error_details.get("message", "")
        return f"`{op}` (requestId `{req_id}`): {message}"

    def save(self, output_dir: str | Path) -> tuple[Path, Path, Path]:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        json_file = out_path / "conformance-report.json"
        json_file.write_text(
            json.dumps(self.to_json_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        xml_file = out_path / "junit-conformance.xml"
        xml_file.write_text(self.to_junit_xml(), encoding="utf-8")

        md_file = out_path / "conformance-report.md"
        md_file.write_text(self.to_markdown(), encoding="utf-8")

        return json_file, xml_file, md_file
