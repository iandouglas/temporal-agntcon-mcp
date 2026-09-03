"""The MCP server: one zero-argument tool per configured service.

A tool call blocks until its Temporal workflow finishes. That is safe on
Railway because the `2026-07-28` streamable-HTTP transport commits
`text/event-stream` for any handler that runs silent past 15 seconds and then
emits `: ping` comment lines, which keeps Railway's edge proxy from closing an
idle request at five minutes. Two consequences:

  * `json_response` must stay off. JSON mode buffers, transfers no bytes during
    an outage, and gets severed with no way to resume - `2026-07-28` removed SSE
    resumability entirely.
  * Railway's 15-minute hard cap still applies, so the wait is bounded below it
    and the workflow is left running rather than abandoned.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy

from durable_mcp.admin import GLOBAL_TARGET, ControlError, apply_control, read_state
from durable_mcp.config import Config, ServiceConfig
from durable_mcp.roles import ADMIN_TOOL_NAMES, RoleFilterMiddleware, bearer_token
from durable_mcp.shared import ServiceCallInput, ServiceCallOutput
from durable_mcp.state import AvailabilityStore
from durable_mcp.workflows import ServiceCallWorkflow

logger = logging.getLogger(__name__)

ClientProvider = Callable[[], Client]


def _caller_label(config: Config, ctx: Context) -> str:
    """Best-effort label for logs: which agent placed this order."""
    headers = getattr(ctx, "headers", None) or {}
    token = bearer_token(headers)
    if token and token == config.admin_token:
        return "admin"
    if token:
        return config.agent_tokens.get(token, "unknown")
    return "anonymous"


def _idempotency_key(ctx: Context) -> str | None:
    """An optional caller-supplied key from the request's `_meta`.

    Blocking tool calls mean a severed stream leads the agent to re-issue the
    request as a brand new one. With a key, the retry attaches to the original
    workflow instead of starting a second order; without one, we mint a UUID and
    accept that risk, which is invisible while every service returns a hardcoded
    `true`.
    """
    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    if meta is None:
        return None
    for key in ("idempotencyKey", "idempotency_key"):
        value = getattr(meta, key, None)
        if value is None and isinstance(meta, dict):
            value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _make_service_tool(
    config: Config, service: ServiceConfig, get_client: ClientProvider
) -> Callable[..., Any]:
    async def call_service_tool(ctx: Context) -> dict[str, Any]:
        caller = _caller_label(config, ctx)
        key = _idempotency_key(ctx) or uuid.uuid4().hex[:12]
        workflow_id = f"{service.tool_name}-{key}"

        handle = await get_client().start_workflow(
            ServiceCallWorkflow.run,
            ServiceCallInput(
                tool_name=service.tool_name,
                url=service.url,
                caller=caller,
                start_to_close_seconds=config.activity_start_to_close_seconds,
                retry_initial_interval_ms=config.retry_initial_interval_ms,
            ),
            id=workflow_id,
            task_queue=config.temporal.task_queue,
            # A re-issued request with the same key joins the running workflow
            # rather than ordering a second taco.
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        logger.info("%s started workflow %s (caller=%s)", service.tool_name, workflow_id, caller)

        try:
            async with asyncio.timeout(config.tool_call_max_wait_seconds):
                result: ServiceCallOutput = await handle.result()
        except TimeoutError:
            # Deliberately generic. Saying "disabled" here would leak the toggle
            # state to a public caller, which is the one thing this must not do.
            raise ToolError(
                f"{service.display_name} is still in progress "
                f"(workflow {workflow_id}). The work is durable and continues."
            ) from None
        except WorkflowFailureError as exc:
            cause = exc.cause.message if exc.cause else str(exc)
            raise ToolError(f"{service.display_name} failed: {cause}") from exc

        return {
            "service": service.display_name,
            "completed": True,
            "response": result.body,
            "attempts": result.attempts,
            "workflow_id": workflow_id,
        }

    call_service_tool.__name__ = service.tool_name
    call_service_tool.__doc__ = service.description
    return call_service_tool


def build_server(config: Config, get_client: ClientProvider, store: AvailabilityStore) -> MCPServer:
    server: MCPServer = MCPServer(
        name="durable-mcp",
        title="Durable MCP",
        version="0.1.0",
        instructions=(
            "Each tool performs one restaurant operation. Calls are durable: a tool "
            "returns only once the underlying work has actually completed."
        ),
        # `2026-07-28` made list results cacheable, and role filtering makes ours
        # vary per caller. "private" is mandatory here: "public" would let a shared
        # intermediary serve the admin tool list to an agent.
        cache_hints={"tools/list": CacheHint(ttl_ms=config.tools_list_ttl_ms, scope="private")},
        middleware=[RoleFilterMiddleware(config)],
    )

    for service in config.services:
        server.add_tool(
            _make_service_tool(config, service, get_client),
            name=service.tool_name,
            title=service.display_name,
            description=service.description,
        )

    async def admin_get_availability() -> dict[str, Any]:
        """Report the master switch and every per-tool switch."""
        return await read_state(config, store)

    async def admin_set_availability(target: str, enabled: bool) -> dict[str, Any]:
        """Turn the MCP layer, or one tool, on or off.

        `target` is "global" for the master switch, or a tool name.
        """
        try:
            return await apply_control(config, store, target, enabled)
        except ControlError as exc:
            raise ToolError(str(exc)) from exc

    server.add_tool(
        admin_get_availability,
        name="admin_get_availability",
        title="Get availability",
        description="Report the master switch and every per-tool availability switch.",
    )
    server.add_tool(
        admin_set_availability,
        name="admin_set_availability",
        title="Set availability",
        description=(
            f"Turn the MCP layer or a single tool on/off. Use target={GLOBAL_TARGET!r} "
            "for the master switch, or a tool name."
        ),
    )

    registered = {tool.name for tool in server.list_tools()}
    missing = ADMIN_TOOL_NAMES - registered
    if missing:
        # The filter hides tools by name. If a name drifts, the filter silently
        # stops hiding it - so assert the two lists agree at boot.
        raise RuntimeError(f"admin tool names out of sync with the role filter: {missing}")

    logger.info(
        "MCP server exposing %d service tool(s) + %d admin tool(s)",
        len(config.services),
        len(ADMIN_TOOL_NAMES),
    )
    return server
