#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import Any

from websockets.asyncio.server import ServerConnection, serve


LOGGER = logging.getLogger("ws_frame_server")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_json(data: Any, compact: bool) -> str:
    if compact:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=False, indent=2)


def summarize(payload: dict[str, Any]) -> str:
    msg_type = payload.get("type", "?")
    seq = payload.get("seq", "?")
    frame_id = None
    frame_meta = payload.get("payload", {}).get("frame_meta")
    if isinstance(frame_meta, dict):
        frame_id = frame_meta.get("frame_id")
    return f"type={msg_type} seq={seq} frame_id={frame_id or '?'}"


async def on_connection(
    ws: ServerConnection, compact: bool, show_raw_on_error: bool
) -> None:
    peer = ws.remote_address
    LOGGER.info("client connected: %s", peer)
    try:
        async for message in ws:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError as exc:
                LOGGER.warning("invalid json from %s: %s", peer, exc)
                if show_raw_on_error:
                    LOGGER.warning("raw payload: %s", message)
                LOGGER.info("%s", "-" * 72)
                continue

            if isinstance(payload, dict):
                LOGGER.info("recv %s", summarize(payload))
            else:
                LOGGER.info("recv non-dict json payload")
            LOGGER.info("%s", format_json(payload, compact=compact))
            LOGGER.info("%s", "-" * 72)
    finally:
        LOGGER.info("client disconnected: %s", peer)


def configure_logger(debug: bool) -> None:
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    if not debug:
        return

    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ws_frame_server.log"

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=8 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    LOGGER.info("debug log enabled: %s", log_file)
    LOGGER.info("log retention: 8 files x 8MB = 64MB max")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Debug websocket server for Android UI frames"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host, default 0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=6910, help="Bind port, default 6910"
    )
    parser.add_argument(
        "--compact", action="store_true", help="Print compact one-line JSON"
    )
    parser.add_argument(
        "--show-raw-on-error",
        action="store_true",
        help="Print raw payload when JSON parsing fails",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable file logging with 64MB rotation",
    )
    return parser


async def main_async(args: argparse.Namespace) -> None:
    async def handler(ws: ServerConnection) -> None:
        await on_connection(
            ws, compact=args.compact, show_raw_on_error=args.show_raw_on_error
        )

    async with serve(handler, args.host, args.port, max_size=4 * 1024 * 1024):
        LOGGER.info("websocket server listening at ws://%s:%s", args.host, args.port)
        await asyncio.Future()


def main() -> None:
    args = build_parser().parse_args()
    configure_logger(args.debug)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOGGER.info("server stopped")


if __name__ == "__main__":
    main()
