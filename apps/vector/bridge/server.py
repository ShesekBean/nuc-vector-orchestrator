#!/usr/bin/env python3
"""Vector HTTP-to-gRPC bridge server.

Translates REST API calls to Vector SDK/gRPC commands.  Runs on NUC at
``localhost:<port>`` (default 8081).  Used by OpenClaw robot-control skill
and the voice command router.

Run::

    python3 -m apps.vector.bridge.server [--port 8081] [--host 127.0.0.1]

Environment variables::

    VECTOR_BRIDGE_PORT  — port (default 8081)
    VECTOR_BRIDGE_HOST  — bind address (default 127.0.0.1)
    VECTOR_SERIAL       — Vector serial number (default 0dd1cdcf)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from aiohttp import web

from apps.vector.bridge.connection import ConnectionManager
from apps.vector.bridge.routes import setup_routes

logger = logging.getLogger(__name__)

DEFAULT_PORT = int(os.environ.get("VECTOR_BRIDGE_PORT", "8081"))
DEFAULT_HOST = os.environ.get("VECTOR_BRIDGE_HOST", "127.0.0.1")


@web.middleware
async def request_logger(request: web.Request, handler) -> web.StreamResponse:
    """Log every request with method, path, and response status."""
    logger.info("%s %s", request.method, request.path)
    try:
        response = await handler(request)
        logger.info("%s %s → %d", request.method, request.path, response.status)
        return response
    except web.HTTPException as exc:
        logger.warning("%s %s → %d", request.method, request.path, exc.status)
        raise
    except Exception:
        logger.exception("%s %s → 500", request.method, request.path)
        raise


def create_app(conn: ConnectionManager | None = None) -> web.Application:
    """Create and configure the aiohttp application.

    Parameters
    ----------
    conn:
        Connection manager to use.  If None, a new one is created.
    """
    app = web.Application(middlewares=[request_logger])
    app["conn"] = conn or ConnectionManager()
    setup_routes(app)
    return app


async def on_startup(app: web.Application) -> None:
    """Connect to Vector on server startup (runs in executor to avoid blocking)."""
    import asyncio

    conn: ConnectionManager = app["conn"]
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, conn.connect)
        logger.info("Vector bridge connected and ready")
    except Exception:
        logger.exception(
            "Failed to connect to Vector on startup — "
            "endpoints will return 503 until connection is established"
        )


async def on_shutdown(app: web.Application) -> None:
    """Disconnect from Vector on server shutdown."""
    import asyncio

    conn: ConnectionManager = app["conn"]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, conn.disconnect)
    logger.info("Vector bridge disconnected")


def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args and run the server."""
    import signal

    parser = argparse.ArgumentParser(description="Vector HTTP-to-gRPC bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument("--serial", type=str, default=None, help="Vector serial number")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    conn = ConnectionManager(serial=args.serial) if args.serial else ConnectionManager()
    app = create_app(conn)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Ensure clean SDK disconnect on SIGTERM/SIGINT so Vector's gRPC streams
    # are properly closed (prevents AudioFeed stall on next connection).
    def _graceful_shutdown(signum, frame):
        signame = signal.Signals(signum).name
        logger.info("Received %s — shutting down gracefully", signame)
        conn.disconnect()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Bind to localhost + Docker bridge IP so OpenClaw container can reach us
    import asyncio

    async def _run():
        runner = web.AppRunner(app)
        await runner.setup()
        sites = []
        for host in (args.host, "172.17.0.1"):
            try:
                site = web.TCPSite(runner, host, args.port)
                await site.start()
                sites.append(host)
                logger.info("Listening on %s:%d", host, args.port)
            except OSError as exc:
                logger.warning("Could not bind to %s:%d — %s", host, args.port, exc)
        if not sites:
            logger.error("No addresses bound — exiting")
            return
        try:
            await asyncio.Event().wait()  # run forever
        finally:
            await runner.cleanup()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
