"""HTTP-level behaviour: authentication, listener isolation, tool visibility.

These tests exercise the apps exactly as `main.run()` assembles them, over the
modern `2026-07-28` transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from helpers import asgi_lifespan, make_config

from durable_mcp.main import build_apps
from durable_mcp.protocol import MODERN_PROTOCOL_VERSION
from durable_mcp.state import MemoryStore

AGENT = "tok-agent"
ADMIN = "tok-admin"
PUBLIC_URL = "http://mcp.example.com:8080"
INTERNAL_URL = "http://mcp.railway.internal:8081"


def _mcp_headers(token: str | None, method: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Mcp-Name": "",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(params or {})
    body["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": body}


class _Harness:
    def __init__(self, public: httpx.AsyncClient, internal: httpx.AsyncClient) -> None:
        self.public = public
        self.internal = internal

    async def tools(self, client: httpx.AsyncClient, token: str | None) -> list[str]:
        response = await client.post(
            "/mcp", json=_rpc("tools/list"), headers=_mcp_headers(token, "tools/list")
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "error" not in payload, payload
        return sorted(tool["name"] for tool in payload["result"]["tools"])


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[_Harness]:
    config = make_config()
    store = MemoryStore()

    def get_client() -> Any:  # not exercised: no test here calls a service tool
        raise AssertionError("Temporal client should not be needed by these tests")

    public_app, internal_app = build_apps(config, store, get_client)
    async with (
        asgi_lifespan(public_app),
        asgi_lifespan(internal_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public_app), base_url=PUBLIC_URL
        ) as public,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url=INTERNAL_URL
        ) as internal,
    ):
        yield _Harness(public, internal)


# --- authentication ------------------------------------------------------


async def test_healthz_needs_no_token_and_leaks_no_state(harness: _Harness) -> None:
    for client in (harness.public, harness.internal):
        response = await client.get("/healthz")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert sorted(payload["tools"]) == ["order_coffee", "order_taco"]
        # Availability must not appear here: healthz is unauthenticated.
        assert "global" not in payload
        assert "switch" not in response.text


@pytest.mark.parametrize("token", [None, "bogus"])
async def test_mcp_rejects_missing_or_unknown_token(harness: _Harness, token: str | None) -> None:
    response = await harness.public.post(
        "/mcp", json=_rpc("tools/list"), headers=_mcp_headers(token, "tools/list")
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


# --- listener isolation --------------------------------------------------


async def test_admin_routes_have_no_route_on_the_public_listener(harness: _Harness) -> None:
    """The core security claim: not merely forbidden, absent."""
    for path in ("/admin/state", "/admin/control", "/admin/reset", "/admin/live"):
        response = await harness.public.get(path, headers={"Authorization": f"Bearer {ADMIN}"})
        assert response.status_code == 404, f"{path} should not exist on the public listener"


async def test_admin_state_requires_admin_role_on_internal_listener(harness: _Harness) -> None:
    response = await harness.internal.get(
        "/admin/state", headers={"Authorization": f"Bearer {AGENT}"}
    )
    assert response.status_code == 403

    response = await harness.internal.get(
        "/admin/state", headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert response.status_code == 200
    assert response.json()["global"] is True


# --- tool visibility -----------------------------------------------------


async def test_agent_on_public_listener_sees_only_service_tools(harness: _Harness) -> None:
    assert await harness.tools(harness.public, AGENT) == ["order_coffee", "order_taco"]


async def test_admin_token_on_public_listener_still_sees_only_service_tools(
    harness: _Harness,
) -> None:
    """A leaked admin token must not expose the admin surface from the internet."""
    assert await harness.tools(harness.public, ADMIN) == ["order_coffee", "order_taco"]


async def test_admin_on_internal_listener_sees_admin_tools(harness: _Harness) -> None:
    assert await harness.tools(harness.internal, ADMIN) == [
        "admin_get_availability",
        "admin_set_availability",
        "order_coffee",
        "order_taco",
    ]


async def test_agent_on_internal_listener_still_sees_no_admin_tools(harness: _Harness) -> None:
    assert await harness.tools(harness.internal, AGENT) == ["order_coffee", "order_taco"]


async def test_tools_list_is_marked_private_so_no_proxy_can_share_it(harness: _Harness) -> None:
    """Role-filtered lists must never be cacheable across authorization contexts."""
    response = await harness.internal.post(
        "/mcp", json=_rpc("tools/list"), headers=_mcp_headers(ADMIN, "tools/list")
    )
    result = response.json()["result"]
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == 30000


# --- admin control flow --------------------------------------------------


async def test_control_toggles_and_state_reflects_it(harness: _Harness) -> None:
    auth = {"Authorization": f"Bearer {ADMIN}"}

    body = (
        await harness.internal.post(
            "/admin/control", json={"target": "order_taco", "enabled": False}, headers=auth
        )
    ).json()
    taco = next(s for s in body["services"] if s["tool_name"] == "order_taco")
    assert taco["switch"] is False
    assert taco["effective"] is False
    coffee = next(s for s in body["services"] if s["tool_name"] == "order_coffee")
    assert coffee["effective"] is True

    # Master switch off takes everything down without clearing per-tool switches.
    body = (
        await harness.internal.post(
            "/admin/control", json={"target": "global", "enabled": False}, headers=auth
        )
    ).json()
    assert body["global"] is False
    assert all(s["effective"] is False for s in body["services"])

    body = (await harness.internal.post("/admin/reset", headers=auth)).json()
    assert body["global"] is True
    assert all(s["effective"] is True for s in body["services"])


async def test_control_rejects_unknown_target(harness: _Harness) -> None:
    response = await harness.internal.post(
        "/admin/control",
        json={"target": "order_burrito", "enabled": False},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert response.status_code == 400
    assert "unknown target" in response.json()["error"]


async def test_control_requires_a_boolean(harness: _Harness) -> None:
    response = await harness.internal.post(
        "/admin/control",
        json={"target": "global", "enabled": "off"},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert response.status_code == 400


async def test_control_is_idempotent(harness: _Harness) -> None:
    auth = {"Authorization": f"Bearer {ADMIN}"}
    for _ in range(3):
        body = (
            await harness.internal.post(
                "/admin/control", json={"target": "global", "enabled": False}, headers=auth
            )
        ).json()
    assert body["global"] is False
