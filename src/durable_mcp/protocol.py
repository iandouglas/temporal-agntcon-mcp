"""Protocol-revision selection, and a loud warning when it goes wrong.

The SDK routes by header: a request carrying `MCP-Protocol-Version` set to
anything outside the known handshake revisions goes to the modern
(`2026-07-28`) transport; otherwise it falls back to the legacy
initialize-handshake path with sessions. There is no server-side switch - **the
client picks the era**.

That matters more here than it looks. Only the modern transport commits
`text/event-stream` and emits `: ping` comment lines for a handler that runs
silent, and those pings are the only thing keeping a blocking tool call alive
through Railway's five-minute idle timeout. A client that omits the header gets
the legacy path, no keepalive, and a request that dies mid-outage with no
resumability to recover it.

So an agent connecting to this server MUST send:

    MCP-Protocol-Version: 2026-07-28

Getting that wrong is silent at low latency and fatal exactly when the audience
starts breaking things, which is the worst possible time to discover it. Hence
the warning below.
"""

from __future__ import annotations

import logging

from mcp_types import LATEST_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
MODERN_PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION

MCP_PATHS = ("/mcp",)


class ProtocolVersionWarningMiddleware:
    """Warns once per observed legacy revision, naming the consequence."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._warned: set[str] = set()

    def _warn(self, observed: str | None) -> None:
        key = observed or "<absent>"
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(
            "client sent MCP-Protocol-Version=%s, so this connection is using the "
            "legacy handshake transport. Blocking tool calls on that path get no "
            "SSE keepalive and will be severed after ~5 minutes behind a proxy, "
            "with no resumability. Send MCP-Protocol-Version: %s instead.",
            key,
            MODERN_PROTOCOL_VERSION,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "") in MCP_PATHS:
            observed = Headers(scope=scope).get(PROTOCOL_VERSION_HEADER)
            if observed is None or observed in HANDSHAKE_PROTOCOL_VERSIONS:
                self._warn(observed)
        await self.app(scope, receive, send)
