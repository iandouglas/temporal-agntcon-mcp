"""HTTP entrypoint: the public MCP listener and the internal admin listener.

Deployed as the Railway `mcp` service. Two ports in one process:

  * `port` (default 8080) gets the public Railway domain and serves `/mcp`.
  * `admin_port` (default 8081) gets **no domain** and serves `/mcp` plus
    `/admin/*`. Reachable only at `mcp.railway.internal:<admin_port>`.

Railway maps one domain to one target port, so the ports you do not map simply
have no route from the internet. That is what makes the admin surface
unreachable from the conference floor rather than merely password-protected -
and it is why the `admin` role additionally requires arrival on this listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import uvicorn
from temporalio.client import Client

from durable_mcp.admin import build_admin_routes, build_health_route
from durable_mcp.config import Config, load_config
from durable_mcp.mcp_app import build_server
from durable_mcp.roles import HTTPAuthMiddleware
from durable_mcp.state import build_store
from durable_mcp.temporal import connect

logger = logging.getLogger(__name__)


class _ManagedServer(uvicorn.Server):
    """Signals are handled once, centrally, for both listeners."""

    def install_signal_handlers(self) -> None:
        return None


def _log_boot(config: Config) -> None:
    logger.info("public listener  :%d  -> /mcp", config.public_port)
    logger.info("internal listener :%d -> /mcp + /admin/*", config.admin_port)
    for service in config.services:
        logger.info("  tool %-24s -> %s", service.tool_name, service.url)
    if config.allow_insecure:
        logger.warning(
            "allow_insecure is set: authentication is relaxed. Never do this on Railway."
        )


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = load_config()
    _log_boot(config)

    store = build_store(config.redis_url)
    client: Client | None = None

    def get_client() -> Client:
        if client is None:  # pragma: no cover - ordering is guaranteed below
            raise RuntimeError("Temporal client is not connected yet")
        return client

    client = await connect(config)
    server = build_server(config, get_client, store)

    # Each call builds its own session manager and lifespan, so the two
    # listeners are fully independent. `json_response` stays off: SSE mode is
    # what gives a blocking call the transport's `: ping` keepalive, without
    # which Railway closes an idle request after five minutes.
    public_app = server.streamable_http_app(streamable_http_path="/mcp", host=config.bind_host)
    internal_app = server.streamable_http_app(streamable_http_path="/mcp", host=config.bind_host)

    # Admin routes exist only on the internal app, so there is no route to them
    # from the public domain even before any token is checked.
    for route in reversed(build_admin_routes(config, store, get_client)):
        internal_app.router.routes.insert(0, route)
    for app in (public_app, internal_app):
        app.router.routes.insert(0, build_health_route(config))

    servers = [
        _ManagedServer(
            uvicorn.Config(
                HTTPAuthMiddleware(app, config),
                host=config.bind_host,
                port=port,
                log_level="info",
                # Blocking tool calls hold a request open for minutes; uvicorn
                # must not time them out before Temporal finishes.
                timeout_keep_alive=int(config.tool_call_max_wait_seconds) + 60,
            )
        )
        for app, port in ((public_app, config.public_port), (internal_app, config.admin_port))
    ]

    loop = asyncio.get_running_loop()

    def shutdown() -> None:
        logger.info("shutdown signal received; draining both listeners")
        for managed in servers:
            managed.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown)

    try:
        await asyncio.gather(*(managed.serve() for managed in servers))
    finally:
        await store.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
