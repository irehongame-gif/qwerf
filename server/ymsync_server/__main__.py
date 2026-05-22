"""``ymsync-server`` entry point — runs the FastAPI app under uvicorn."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from ymsync.config import ConfigError, load_config

from ymsync_server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ymsync-server",
        description="Local listener for the qwerf Chrome extension.",
    )
    parser.add_argument("--host", default=None, help="bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: from config)")
    parser.add_argument("--reload", action="store_true", help="dev autoreload")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Run `ymsync init` first.", file=sys.stderr)
        sys.exit(2)

    host = args.host or cfg.server_host
    port = args.port or cfg.server_port

    app = create_app(cfg)
    print(f"ymsync-server listening on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
