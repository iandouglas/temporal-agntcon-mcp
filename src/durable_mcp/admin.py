"""The audience control panel's API, plus the stage dashboard.

Mounted only on the internal listener, so none of it has a route from the
internet. The panel drives it over Railway's private network at
`mcp.railway.internal:<admin_port>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from temporalio.client import Client

from durable_mcp.config import Config
from durable_mcp.roles import bearer_token, caller_for, listener_port
from durable_mcp.state import GLOBAL_KEY, AvailabilityStore

logger = logging.getLogger(__name__)

GLOBAL_TARGET = "global"
MAX_TRACKED_WORKFLOWS = 25
RUNNING_QUERY = "WorkflowType = 'ServiceCallWorkflow' AND ExecutionStatus = 'Running'"


class ControlError(ValueError):
    """An unusable control request (unknown target)."""


async def read_state(config: Config, store: AvailabilityStore) -> dict[str, Any]:
    """Current switch positions plus the resolved tool table.

    The tool table is included so the panel renders its buttons from the
    server's actual configuration instead of hardcoding `taco`/`coffee`. Rename
    `service_2_name` an hour before the talk and the panel follows.
    """
    flags = await store.get_state()
    global_enabled = flags.get(GLOBAL_KEY, True)
    return {
        "global": global_enabled,
        "services": [
            {
                "tool_name": service.tool_name,
                "display_name": service.display_name,
                "description": service.description,
                "url": service.url,
                "switch": flags.get(service.tool_name, True),
                "effective": global_enabled and flags.get(service.tool_name, True),
            }
            for service in config.services
        ],
    }


async def apply_control(
    config: Config, store: AvailabilityStore, target: str, enabled: bool
) -> dict[str, Any]:
    """Flip the master switch or one tool's switch. Idempotent, safe to mash."""
    key = GLOBAL_KEY if target in (GLOBAL_TARGET, GLOBAL_KEY) else target
    if key != GLOBAL_KEY and config.service_for(key) is None:
        known = ", ".join(s.tool_name for s in config.services)
        raise ControlError(f"unknown target {target!r}; expected 'global' or one of: {known}")
    await store.set_state(key, enabled)
    logger.info("availability: %s -> %s", key, "on" if enabled else "off")
    return await read_state(config, store)


async def in_flight(config: Config, client: Client) -> list[dict[str, Any]]:
    """Running tool calls with their live Temporal retry counts.

    This is the stage visual. `pending_activities[].attempt` is the number the
    audience watches climb while a service is dead, and `last_failure.message`
    is why it is still climbing.
    """
    rows: list[dict[str, Any]] = []
    try:
        async for execution in client.list_workflows(RUNNING_QUERY, limit=MAX_TRACKED_WORKFLOWS):
            handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
            try:
                description = await handle.describe()
            except Exception:  # the dashboard must never crash the demo
                logger.debug("describe failed for %s; skipping row", execution.id, exc_info=True)
                continue
            pending = list(description.raw_description.pending_activities)
            attempt = pending[0].attempt if pending else 0
            last_error = ""
            if pending and pending[0].HasField("last_failure"):
                last_error = pending[0].last_failure.message
            rows.append(
                {
                    "workflow_id": execution.id,
                    "run_id": execution.run_id,
                    "started_at": execution.start_time.isoformat()
                    if execution.start_time
                    else None,
                    "attempt": attempt,
                    "last_error": last_error,
                }
            )
    except Exception:  # ditto: degrade to an empty table, never a 500
        logger.warning("could not list running workflows for the dashboard", exc_info=True)
    return rows


def _require_admin(request: Request, config: Config) -> JSONResponse | None:
    internal = listener_port(request) == config.admin_port
    caller = caller_for(config, request.headers, internal)
    if caller is None or not caller.is_admin:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


def build_admin_routes(
    config: Config,
    store: AvailabilityStore,
    get_client: Any,
) -> list[Route]:
    """Admin REST + SSE routes. `get_client` returns the connected Temporal client."""

    async def control(request: Request) -> JSONResponse:
        if (denied := _require_admin(request, config)) is not None:
            return denied
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        target = str(payload.get("target", GLOBAL_TARGET))
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "'enabled' must be true or false"}, status_code=400)

        try:
            return JSONResponse(await apply_control(config, store, target, enabled))
        except ControlError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def state(request: Request) -> JSONResponse:
        if (denied := _require_admin(request, config)) is not None:
            return denied
        return JSONResponse(await read_state(config, store))

    async def reset(request: Request) -> JSONResponse:
        if (denied := _require_admin(request, config)) is not None:
            return denied
        await store.reset()
        logger.info("availability: all switches reset to on")
        return JSONResponse(await read_state(config, store))

    async def live(request: Request) -> Any:
        if (denied := _require_admin(request, config)) is not None:
            return denied

        async def events() -> AsyncIterator[dict[str, str]]:
            previous: str | None = None
            while True:
                if await request.is_disconnected():
                    return
                snapshot = json.dumps(
                    {
                        "state": await read_state(config, store),
                        "in_flight": await in_flight(config, get_client()),
                    },
                    default=str,
                )
                if snapshot != previous:
                    previous = snapshot
                    yield {"event": "snapshot", "data": snapshot}
                await asyncio.sleep(config.dashboard_poll_seconds)

        return EventSourceResponse(events(), ping=15)

    return [
        Route("/admin/control", control, methods=["POST"]),
        Route("/admin/state", state, methods=["GET"]),
        Route("/admin/reset", reset, methods=["POST"]),
        Route("/admin/live", live, methods=["GET"]),
    ]


def build_health_route(config: Config) -> Route:
    """Unauthenticated health check, mounted on both listeners for Railway.

    Reports only tool names - never availability state, which would leak the
    toggle positions to any public caller.
    """

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "tools": [s.tool_name for s in config.services]})

    return Route("/healthz", healthz, methods=["GET"])


__all__ = [
    "GLOBAL_TARGET",
    "ControlError",
    "apply_control",
    "bearer_token",
    "build_admin_routes",
    "build_health_route",
    "in_flight",
    "read_state",
]
