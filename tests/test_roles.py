from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from durable_mcp.config import Config, ServiceConfig, TemporalConfig
from durable_mcp.roles import RoleFilterMiddleware, resolve_caller

PUBLIC_PORT = 8080
ADMIN_PORT = 8081


def _config(**overrides: Any) -> Config:
    service = ServiceConfig(
        index=1,
        tool_name="order_taco",
        display_name="Order Taco",
        description="d",
        url="http://svc/taco",
    )
    base: dict[str, Any] = {
        "services": (service,),
        "temporal": TemporalConfig("localhost:7233", "default", "q", None, False),
        "redis_url": None,
        "agent_tokens": {"tok-agent": "melissa"},
        "admin_token": "tok-admin",
        "public_port": PUBLIC_PORT,
        "admin_port": ADMIN_PORT,
        "bind_host": "127.0.0.1",
        "activity_start_to_close_seconds": 3.0,
        "http_timeout_seconds": 2.5,
        "retry_initial_interval_ms": 250,
        "tool_call_max_wait_seconds": 840.0,
        "keepalive_interval_seconds": 30.0,
        "dashboard_poll_seconds": 0.5,
        "tools_list_ttl_ms": 30000,
        "allow_insecure": False,
        "services_by_tool": {"order_taco": service},
    }
    base.update(overrides)
    return Config(**base)


def test_admin_token_on_internal_listener_is_admin() -> None:
    caller = resolve_caller(_config(), "tok-admin", internal=True)
    assert caller is not None
    assert caller.is_admin


def test_admin_token_on_public_listener_is_downgraded() -> None:
    """The core guarantee: a leaked admin token cannot reach the levers."""
    caller = resolve_caller(_config(), "tok-admin", internal=False)
    assert caller is not None
    assert not caller.is_admin


def test_agent_token_is_labelled_and_never_admin() -> None:
    for internal in (True, False):
        caller = resolve_caller(_config(), "tok-agent", internal=internal)
        assert caller is not None
        assert caller.label == "melissa"
        assert not caller.is_admin


@pytest.mark.parametrize("token", [None, "", "nope"])
def test_unknown_token_is_unauthenticated(token: str | None) -> None:
    assert resolve_caller(_config(), token, internal=True) is None


def test_insecure_mode_only_applies_when_nothing_is_configured() -> None:
    open_config = _config(agent_tokens={}, admin_token=None, allow_insecure=True)
    caller = resolve_caller(open_config, None, internal=True)
    assert caller is not None
    assert caller.is_admin

    # With tokens configured, allow_insecure must not become a bypass.
    locked = _config(allow_insecure=True)
    assert resolve_caller(locked, "nope", internal=True) is None


# --- tools/list filtering ------------------------------------------------


class _Tool(BaseModel):
    name: str


class _ListResult(BaseModel):
    tools: list[_Tool]


@dataclass
class _FakeRequest:
    headers: dict[str, str]
    scope: dict[str, Any]


@dataclass
class _FakeCtx:
    method: str
    params: dict[str, Any] | None = None
    request: Any = None
    meta: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


def _ctx(method: str, token: str | None, port: int, params: dict[str, Any] | None = None) -> Any:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return _FakeCtx(
        method=method,
        params=params,
        request=_FakeRequest(headers=headers, scope={"server": ("0.0.0.0", port)}),
    )


ALL_TOOLS = _ListResult(
    tools=[
        _Tool(name="order_taco"),
        _Tool(name="admin_get_availability"),
        _Tool(name="admin_set_availability"),
    ]
)


async def test_agent_never_sees_admin_tools() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return ALL_TOOLS

    result = await middleware(_ctx("tools/list", "tok-agent", PUBLIC_PORT), call_next)
    assert [t.name for t in result.tools] == ["order_taco"]


async def test_admin_token_on_public_port_still_sees_no_admin_tools() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return ALL_TOOLS

    result = await middleware(_ctx("tools/list", "tok-admin", PUBLIC_PORT), call_next)
    assert [t.name for t in result.tools] == ["order_taco"]


async def test_admin_on_internal_port_sees_everything() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return ALL_TOOLS

    result = await middleware(_ctx("tools/list", "tok-admin", ADMIN_PORT), call_next)
    assert [t.name for t in result.tools] == [
        "order_taco",
        "admin_get_availability",
        "admin_set_availability",
    ]


async def test_dict_shaped_list_result_is_also_filtered() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return {"tools": [{"name": "order_taco"}, {"name": "admin_set_availability"}]}

    result = await middleware(_ctx("tools/list", "tok-agent", PUBLIC_PORT), call_next)
    assert result == {"tools": [{"name": "order_taco"}]}


async def test_agent_calling_admin_tool_gets_unknown_tool_not_forbidden() -> None:
    """ "Forbidden" would confirm the tool exists. It must read as nonexistent."""
    middleware = RoleFilterMiddleware(_config())
    called = False

    async def call_next(_: Any) -> Any:
        nonlocal called
        called = True
        return {}

    ctx = _ctx("tools/call", "tok-agent", PUBLIC_PORT, params={"name": "admin_set_availability"})
    with pytest.raises(ToolError, match="Unknown tool"):
        await middleware(ctx, call_next)
    assert not called


async def test_admin_may_call_admin_tool() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return {"ok": True}

    ctx = _ctx("tools/call", "tok-admin", ADMIN_PORT, params={"name": "admin_set_availability"})
    assert await middleware(ctx, call_next) == {"ok": True}


async def test_service_tool_call_passes_through_for_agents() -> None:
    middleware = RoleFilterMiddleware(_config())

    async def call_next(_: Any) -> Any:
        return {"ok": True}

    ctx = _ctx("tools/call", "tok-agent", PUBLIC_PORT, params={"name": "order_taco"})
    assert await middleware(ctx, call_next) == {"ok": True}
