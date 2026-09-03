"""The side-effecting half of a tool call: check the flag, then POST."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from durable_mcp.shared import CALL_SERVICE_ACTIVITY, ServiceCallInput, ServiceCallOutput
from durable_mcp.state import AvailabilityStore

logger = logging.getLogger(__name__)

# 4xx means we are asking wrong, not that the far end is unwell. Retrying a
# typo'd URL four times a second for fourteen minutes looks exactly like a
# genuine outage on stage, which is a terrible way to discover a spelling
# mistake. 408 and 429 are the two that really are transient.
_NON_RETRYABLE_STATUSES = frozenset(range(400, 500)) - {408, 429}


class ServiceActivities:
    """Activity implementations, bound to a shared HTTP client and flag store."""

    def __init__(self, store: AvailabilityStore, http_timeout_seconds: float) -> None:
        self._store = store
        self._client = httpx.AsyncClient(
            # Must stay under the activity's start_to_close_timeout, so the
            # HTTP layer gives up first and Temporal records a clean failure
            # rather than having the attempt killed out from under it.
            timeout=httpx.Timeout(http_timeout_seconds),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @activity.defn(name=CALL_SERVICE_ACTIVITY)
    async def call_service(self, args: ServiceCallInput) -> ServiceCallOutput:
        attempt = activity.info().attempt

        # Read on *every* attempt, never once in the workflow: flipping the
        # switch back mid-outage has to be noticed.
        if not await self._store.is_enabled(args.tool_name):
            raise ApplicationError(
                f"{args.tool_name} is currently marked unavailable",
                type="ToolUnavailable",
            )

        try:
            response = await self._client.post(args.url)
        except httpx.TimeoutException as exc:
            raise ApplicationError(
                f"{args.tool_name}: timed out calling {args.url}",
                type="DownstreamTimeout",
            ) from exc
        except httpx.RequestError as exc:
            raise ApplicationError(
                f"{args.tool_name}: cannot reach {args.url} ({exc.__class__.__name__})",
                type="DownstreamUnreachable",
            ) from exc

        if response.status_code in _NON_RETRYABLE_STATUSES:
            raise ApplicationError(
                f"{args.tool_name}: {args.url} returned {response.status_code}. "
                "This looks like a configuration error, not an outage - check the URL.",
                type="DownstreamConfigError",
                non_retryable=True,
            )

        if not response.is_success:
            raise ApplicationError(
                f"{args.tool_name}: {args.url} returned {response.status_code}",
                type="DownstreamError",
            )

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None

        logger.info(
            "%s completed on attempt %d (caller=%s, status=%d)",
            args.tool_name,
            attempt,
            args.caller,
            response.status_code,
        )
        return ServiceCallOutput(
            status_code=response.status_code,
            body=body,
            raw_text=response.text[:4096],
            attempts=attempt,
        )
