"""命令行入口：``python -m video_testkit`` 或控制台脚本 ``video-testkit``。"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from video_testkit.app import create_app
from video_testkit.config import get_settings
from video_testkit.logging_conf import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-testkit",
        description="SmartFire Video TestKit：Fake Video Provider + GB28181 Device Simulator",
    )
    parser.add_argument(
        "--host", default=None, help="HTTP 监听地址（默认读环境变量 VIDEO_TESTKIT_HOST）"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP 监听端口（默认读环境变量 VIDEO_TESTKIT_PORT）"
    )
    parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_logging(logging.getLevelName(args.log_level.upper()))
    settings = get_settings()
    if args.host is not None:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
