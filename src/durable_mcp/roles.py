"""Who is calling, and what they are allowed to see.

Two roles. `agent` sees the service tools. `admin` additionally sees the
availability tools, and is only obtainable by presenting `admin_token` **on the
internal listener**. An admin token arriving at the public port is downgraded to
`agent`, so the "internal network only" constraint is enforced by the network
rather than by a header a caller controls: a leaked admin token is not
sufficient to reach the chaos levers from the conference floor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from durable_mcp.config import Config

logger = logging.getLogger(__name__)

ADMIN_TOOL_NAMES = frozenset({"admin_get_availability", "admin_set_availability"})
EXEMPT_PATHS = frozenset({"/healthz", "/health"})


@dataclass(frozen=True, slots=True)
class Caller:
    label: str
    is_admin: bool


def bearer_token(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("authorization")
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def listener_port(scope_or_request: Any) -> int | None:
    """The port this request actually arrived on.

    Taken from the ASGI scope's `server` pair, which is the socket the process
    is listening on - not anything the client can set. That is the whole point:
    a header would be spoofable, a listening port is not.
    """
    scope = scope_or_request if isinstance(scope_or_request, dict) else None
    if scope is None:
        scope = getattr(scope_or_request, "scope", None)
    if not isinstance(scope, dict):
        return None
    server = scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) >= 2 and isinstance(server[1], int):
        return server[1]
    return None


def resolve_caller(config: Config, token: str | None, internal: bool) -> Caller | None:
    """`None` means unauthenticated - the caller gets a 401 and no tool list."""
    if config.admin_token and token == config.admin_token:
        if internal:
            return Caller("admin", True)
        logger.warning("admin_token presented on the public listener; downgrading to agent role")
        return Caller("admin-token-downgraded", False)

    if token:
        label = config.agent_tokens.get(token)
        if label:
            return Caller(label, False)

    # Local development only, and only when nothing at all is configured.
    if config.allow_insecure and not config.agent_tokens and not config.admin_token:
        return Caller("insecure-local", internal)

    return None


def caller_for(config: Config, headers: Mapping[str, str], internal: bool) -> Caller | None:
    return resolve_caller(config, bearer_token(headers), internal)


class HTTPAuthMiddleware:
    """Rejects unauthenticated requests before MCP ever sees them.

    A missing or unknown bearer token gets a flat 401, not a reduced tool list.
    """

    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        internal = listener_port(scope) == self.config.admin_port
        caller = caller_for(self.config, Headers(scope=scope), internal)
        if caller is None:
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="durable-mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


class RoleFilterMiddleware:
    """Hides the admin tools from anyone who is not an admin.

    `2026-07-28` removed per-connection variation of list endpoints so that
    list results become cacheable, which is exactly what filtering by role
    reintroduces. The server therefore pins `cacheScope` to `"private"` (see
    `mcp_app.build_server`) so no shared intermediary can ever hand the admin
    tool list to an agent.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def _caller(self, ctx: ServerRequestContext[Any, Any]) -> Caller | None:
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        internal = listener_port(request) == self.config.admin_port
        return caller_for(self.config, headers, internal)

    @staticmethod
    def _strip_admin_tools(result: HandlerResult) -> HandlerResult:
        tools = getattr(result, "tools", None)
        if tools is not None:
            kept = [t for t in tools if getattr(t, "name", None) not in ADMIN_TOOL_NAMES]
            return result.model_copy(update={"tools": kept})  # type: ignore[union-attr]
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            kept_raw = [t for t in result["tools"] if t.get("name") not in ADMIN_TOOL_NAMES]
            return {**result, "tools": kept_raw}
        return result

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        caller = self._caller(ctx)
        is_admin = bool(caller and caller.is_admin)

        if not is_admin and ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name in ADMIN_TOOL_NAMES:
                # "Unknown", not "forbidden": a forbidden response would confirm
                # the tool exists, which is the one thing a public caller must
                # not be able to learn.
                raise ToolError(f"Unknown tool: {name}")

        result = await call_next(ctx)

        if not is_admin and ctx.method == "tools/list":
            return self._strip_admin_tools(result)
        return result
