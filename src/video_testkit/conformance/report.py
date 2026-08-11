"""Conformance 报告生成器：JSON Summary 与 Standard JUnit XML。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET


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

    def to_json_dict(self) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.status == "PASSED")
        failed = sum(1 for r in self.results if r.status == "FAILED")
        skipped = sum(1 for r in self.results if r.status == "SKIPPED")

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
            "startTime": self.start_time,
            "endTime": self.end_time,
            "durationSeconds": round(self.duration_seconds, 3),
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            },
            "capabilities": self.capabilities,
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
                fail_elem = ET.SubElement(case, "failure", attrib={"message": err_msg})
                fail_elem.text = err_text

        res = ET.tostring(testsuites, encoding="utf-8").decode("utf-8")
        return str(res)

    def save(self, output_dir: str | Path) -> tuple[Path, Path]:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        json_file = out_path / "conformance-report.json"
        json_file.write_text(
            json.dumps(self.to_json_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        xml_file = out_path / "junit-conformance.xml"
        xml_file.write_text(self.to_junit_xml(), encoding="utf-8")

        return json_file, xml_file
