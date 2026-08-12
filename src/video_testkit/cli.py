"""命令行入口：``python -m video_testkit`` 或控制台脚本 ``video-testkit``。"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from video_testkit.app import create_app
from video_testkit.config import get_settings
from video_testkit.logging_conf import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-testkit",
        description="SmartFire Video TestKit：Fake Video Provider + GB28181 Device Simulator",
    )

    subparsers = parser.add_subparsers(dest="command")

    # 子命令：server（服务启动）
    server_parser = subparsers.add_parser("server", help="启动 Provider & Simulator 服务")
    server_parser.add_argument(
        "--host", default=None, help="HTTP 监听地址（默认读 VIDEO_TESTKIT_HOST）"
    )
    server_parser.add_argument(
        "--port", type=int, default=None, help="HTTP 监听端口（默认读 VIDEO_TESTKIT_PORT）"
    )
    server_parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )

    # 子命令：conformance（契约 Conformance 测试）
    conf_parser = subparsers.add_parser("conformance", help="运行 Provider Conformance 测试")
    conf_parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000/provider/v1",
        help="被测 Provider Base URL (如 http://127.0.0.1:8000/provider/v1)",
    )
    conf_parser.add_argument("--token", default=None, help="Bearer Auth Token")
    conf_parser.add_argument("--contract-version", default="1.0.0-draft.1", help="期望的契约版本号")
    conf_parser.add_argument(
        "--bundle", default=None, help="Contract Bundle tar.gz 文件路径（默认使用内置 Bundle）"
    )
    conf_parser.add_argument("--report-dir", default="./conformance-reports", help="报告输出目录")
    conf_parser.add_argument(
        "--provider-type", default=None, help="覆盖 Provider 类型（如 WVP, SIPGO_GATEWAY, MOCK）"
    )
    conf_parser.add_argument("--timeout", type=float, default=10.0, help="HTTP 请求超时秒数")
    conf_parser.add_argument(
        "--scenario", default="", help="测试场景描述（进入报告标识，如 four-channel NVR + IPC）"
    )
    conf_parser.add_argument("--seed", default="", help="确定性 seed（进入报告标识）")
    conf_parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="连续运行次数（>=3 为发布 Gate：要求全部运行零失败）",
    )
    conf_parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )

    # 全局兼容顶层参数（当未输入子命令时，退回到启动 server）
    parser.add_argument("--host", default=None, help="HTTP 监听地址（默认读 VIDEO_TESTKIT_HOST）")
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP 监听端口（默认读 VIDEO_TESTKIT_PORT）"
    )
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = getattr(args, "log_level", "info") or "info"
    configure_logging(logging.getLevelName(log_level.upper()))

    if args.command == "conformance":
        from video_testkit.conformance.runner import ConformanceRunner

        provider_type = getattr(args, "provider_type", None)
        runs = max(1, getattr(args, "runs", 1))
        scenario = getattr(args, "scenario", "") or ""
        seed = getattr(args, "seed", "") or ""
        all_summaries = []
        failed_total = 0
        for run_index in range(1, runs + 1):
            try:
                runner = ConformanceRunner(
                    base_url=args.base_url,
                    token=args.token,
                    contract_version=args.contract_version,
                    bundle_path=args.bundle,
                    provider_type=provider_type,
                    timeout=args.timeout,
                    scenario=scenario,
                    seed=seed,
                )
                report = runner.run()
            except RuntimeError as exc:
                print(f"\nConformance 运行失败（{run_index}/{runs}）: {exc}")
                sys.exit(1)
            report_dir = args.report_dir
            if runs > 1:
                report_dir = f"{args.report_dir}/run-{run_index}"
            json_file, xml_file, md_file = report.save(report_dir)

            summary = report.to_json_dict()["summary"]
            all_summaries.append(summary)
            failed_total += summary["failed"]

            print(f"\n=== Provider Contract Conformance Report ({run_index}/{runs}) ===")
            print(f"Test Run ID : {report.test_run_id}")
            print(f"Target URL  : {args.base_url}")
            print(f"Contract Ver: {report.contract_version}")
            print(f"Provider    : {report.provider_type} ({report.provider_instance_code})")
            print(f"Scenario    : {report.scenario or 'default'} (seed {report.seed or 'default'})")
            print(f"Total Tests : {summary['total']}")
            print(f"Passed      : {summary['passed']}")
            print(f"Failed      : {summary['failed']}")
            print(f"Skipped     : {summary['skipped']}")
            print(f"JSON Report : {json_file}")
            print(f"JUnit XML   : {xml_file}")
            print(f"Markdown    : {md_file}")

        # 发布 Gate：连续运行（>=3 次）全部零失败。
        if runs >= 3:
            gate_passed = failed_total == 0 and len(all_summaries) == runs
            print("\n=== Release Gate ===")
            print(f"Runs        : {runs}")
            print(f"Total failed: {failed_total}")
            print(f"Gate result : {'PASSED' if gate_passed else 'FAILED'}")
            print("Conclusion  : Simulator Conformance only; does not imply Vendor Compatibility")
            if not gate_passed:
                sys.exit(1)
            sys.exit(0)

        if failed_total > 0:
            sys.exit(1)
        sys.exit(0)

    # 默认命令 / server 子命令
    settings = get_settings()
    if args.host is not None:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=log_level)


if __name__ == "__main__":
    main()
