"""The whole thing at once: HTTP tools/call -> Temporal -> HTTP service.

Real dev server, real worker, real ASGI app, real downstream service. These are
the tests that prove the demo works, including the SSE keepalive that Railway's
proxy depends on.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from helpers import asgi_lifespan, make_config
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_mcp import dev_service
from durable_mcp.activities import ServiceActivities
from durable_mcp.config import ServiceConfig, TemporalConfig
from durable_mcp.main import build_apps
from durable_mcp.protocol import MODERN_PROTOCOL_VERSION
from durable_mcp.state import MemoryStore
from durable_mcp.workflows import ServiceCallWorkflow

AGENT = "tok-agent"
SERVICE_BASE = "http://svc"
TASK_QUEUE = "q"

_TEMPORAL_CLI = shutil.which("temporal")
pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="temporal CLI not available for a local dev server"
)


class _Stack:
    def __init__(self, client: httpx.AsyncClient, store: MemoryStore) -> None:
        self.client = client
        self.store = store

    async def call(self, tool: str, timeout: float = 60.0) -> httpx.Response:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                },
            },
        }
        return await self.client.post(
            "/mcp",
            json=body,
            timeout=timeout,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {AGENT}",
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": tool,
            },
        )


def _tool_payload(response: httpx.Response) -> dict[str, Any]:
    """Unwrap a result that may have arrived as JSON or as an SSE frame."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no data frame in SSE response: {text!r}")
    return response.json()


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[_Stack]:
    environment = await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI)
    config = make_config(
        services=(
            ServiceConfig(1, "order_taco", "Order Taco", "Order a taco", f"{SERVICE_BASE}/taco"),
        ),
        temporal=TemporalConfig("unused", "default", TASK_QUEUE, None, False),
        tool_call_max_wait_seconds=45.0,
        retry_initial_interval_ms=100,
    )
    store = MemoryStore()

    service_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dev_service.build_app(["taco"])),
        base_url=SERVICE_BASE,
    )
    activities = ServiceActivities(store, 2.5, client=service_client)
    worker = Worker(
        environment.client,
        task_queue=TASK_QUEUE,
        workflows=[ServiceCallWorkflow],
        activities=[activities.call_service],
    )

    public_app, _ = build_apps(config, store, lambda: environment.client)
    try:
        async with (
            worker,
            asgi_lifespan(public_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=public_app), base_url="http://mcp:8080"
            ) as client,
        ):
            yield _Stack(client, store)
    finally:
        dev_service._paused.clear()
        await activities.aclose()
        await environment.shutdown()


async def test_tool_call_succeeds_end_to_end(stack: _Stack) -> None:
    response = await stack.call("order_taco")
    assert response.status_code == 200, response.text

    payload = _tool_payload(response)
    assert "error" not in payload, payload
    structured = payload["result"]["structuredContent"]
    assert structured["completed"] is True
    assert structured["response"] == {"taco": True}
    assert structured["attempts"] == 1


async def test_dead_service_still_returns_a_successful_tool_call(stack: _Stack) -> None:
    """The demo: the audience kills /taco and the tool call still succeeds."""
    dev_service._paused.add("taco")
    call = asyncio.create_task(stack.call("order_taco"))

    await asyncio.sleep(1.5)
    assert not call.done(), "the tool call must stay open while Temporal retries"

    dev_service._paused.discard("taco")
    response = await asyncio.wait_for(call, timeout=45)

    structured = _tool_payload(response)["result"]["structuredContent"]
    assert structured["completed"] is True
    assert structured["response"] == {"taco": True}
    assert structured["attempts"] > 1, "expected Temporal to have retried"


async def test_switched_off_tool_is_absorbed_not_failed(stack: _Stack) -> None:
    await stack.store.set_state("order_taco", enabled=False)
    call = asyncio.create_task(stack.call("order_taco"))

    await asyncio.sleep(1.0)
    assert not call.done(), "a switched-off tool must retry, not fail"

    await stack.store.set_state("order_taco", enabled=True)
    response = await asyncio.wait_for(call, timeout=45)
    structured = _tool_payload(response)["result"]["structuredContent"]
    assert structured["completed"] is True


async def test_long_call_upgrades_to_sse_and_keeps_the_connection_alive(
    stack: _Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Railway closes an idle request at 5 minutes; the ping is what prevents it.

    The real interval is 15s. Shortened here so the upgrade is observable in a
    fast test - what matters is that a silent handler commits
    `text/event-stream` rather than buffering a JSON response.
    """
    from mcp.server import _streamable_http_modern as modern

    monkeypatch.setattr(modern, "_SSE_PING_INTERVAL", 0.4)

    dev_service._paused.add("taco")
    call = asyncio.create_task(stack.call("order_taco"))
    await asyncio.sleep(1.5)
    dev_service._paused.discard("taco")

    response = await asyncio.wait_for(call, timeout=45)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert ": ping" in response.text, "expected transport keepalive comments"

    structured = _tool_payload(response)["result"]["structuredContent"]
    assert structured["completed"] is True
    assert structured["attempts"] > 1
