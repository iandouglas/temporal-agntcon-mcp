from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from starlette.types import ASGIApp

from durable_mcp.config import Config, ServiceConfig, TemporalConfig


@asynccontextmanager
async def asgi_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol against the real middleware chain.

    Deliberately not Starlette's `lifespan_context` on an unwrapped app: running
    it through the outermost app is what proves our middlewares pass lifespan
    events down, without which uvicorn would never start the MCP session manager.
    """
    to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, to_app.get, from_app.put)
    )
    await to_app.put({"type": "lifespan.startup"})
    message = await asyncio.wait_for(from_app.get(), timeout=10)
    assert message["type"] == "lifespan.startup.complete", message
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(from_app.get(), timeout=10)
        except TimeoutError:
            pass
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def make_config(**overrides: Any) -> Config:
    """A two-service config matching the demo's shape."""
    taco = ServiceConfig(1, "order_taco", "Order Taco", "Order a taco", "http://svc/taco")
    coffee = ServiceConfig(2, "order_coffee", "Order Coffee", "Order a coffee", "http://svc/coffee")
    base: dict[str, Any] = {
        "services": (taco, coffee),
        "temporal": TemporalConfig("localhost:7233", "default", "q", None, False),
        "redis_url": None,
        "agent_tokens": {"tok-agent": "melissa"},
        "admin_token": "tok-admin",
        "public_port": 8080,
        "admin_port": 8081,
        "bind_host": "0.0.0.0",
        "activity_start_to_close_seconds": 3.0,
        "http_timeout_seconds": 2.5,
        "retry_initial_interval_ms": 250,
        "tool_call_max_wait_seconds": 840.0,
        "keepalive_interval_seconds": 30.0,
        "dashboard_poll_seconds": 0.05,
        "tools_list_ttl_ms": 30000,
        "allow_insecure": False,
        "services_by_tool": {"order_taco": taco, "order_coffee": coffee},
    }
    base.update(overrides)
    return Config(**base)
