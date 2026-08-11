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
        runner = ConformanceRunner(
            base_url=args.base_url,
            token=args.token,
            contract_version=args.contract_version,
            bundle_path=args.bundle,
            provider_type=provider_type,
            timeout=args.timeout,
        )
        report = runner.run()
        json_file, xml_file = report.save(args.report_dir)

        summary = report.to_json_dict()["summary"]
        print("\n=== Provider Contract Conformance Report ===")
        print(f"Test Run ID : {report.test_run_id}")
        print(f"Target URL  : {args.base_url}")
        print(f"Contract Ver: {report.contract_version}")
        print(f"Provider    : {report.provider_type} ({report.provider_instance_code})")
        print(f"Total Tests : {summary['total']}")
        print(f"Passed      : {summary['passed']}")
        print(f"Failed      : {summary['failed']}")
        print(f"Skipped     : {summary['skipped']}")
        print(f"JSON Report : {json_file}")
        print(f"JUnit XML   : {xml_file}")

        if summary["failed"] > 0:
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
