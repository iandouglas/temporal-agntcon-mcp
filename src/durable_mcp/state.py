"""Per-tool availability flags: the audience's lever on the MCP layer itself.

A tool is callable only when the master switch *and* its own switch are on.
Either being off is surfaced to the workflow as a retryable failure, so a
toggled-off tool behaves exactly like a dead downstream: retried every 250ms
until someone flips it back. Nothing is hidden and nothing fails fast, which is
also why a public caller cannot detect toggle state.

Reads happen inside the Temporal activity on every attempt. They must never
happen in workflow code: that would be a non-determinism violation, and the
value would be frozen into history at the first attempt so flipping the switch
back would never be noticed.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

GLOBAL_KEY = "__global__"
HASH_KEY = "durable_mcp:availability"


class AvailabilityStore(Protocol):
    """Reads and writes the availability flags."""

    async def is_enabled(self, tool_name: str) -> bool: ...
    async def get_state(self) -> dict[str, bool]: ...
    async def set_state(self, target: str, enabled: bool) -> None: ...
    async def reset(self) -> None: ...
    async def close(self) -> None: ...


def _effective(flags: dict[str, bool], tool_name: str) -> bool:
    """`global AND per_tool`, defaulting to enabled when a flag was never set."""
    return flags.get(GLOBAL_KEY, True) and flags.get(tool_name, True)


class MemoryStore:
    """In-process flags. Local development and tests only.

    Deliberately not used on Railway: killing the MCP container would silently
    reset every toggle to "available" and discard the demo state you set up.
    """

    def __init__(self) -> None:
        self._flags: dict[str, bool] = {}

    async def is_enabled(self, tool_name: str) -> bool:
        return _effective(self._flags, tool_name)

    async def get_state(self) -> dict[str, bool]:
        return dict(self._flags)

    async def set_state(self, target: str, enabled: bool) -> None:
        self._flags[target] = enabled

    async def reset(self) -> None:
        self._flags.clear()

    async def close(self) -> None:
        return None


class RedisStore:
    """Flags in a single Redis hash, so they survive an MCP server restart.

    Every read fails **open**. A Redis blip must never be able to wedge the
    demo: if we cannot tell whether a tool is disabled, we let the call proceed.
    The failure mode of guessing wrong is a taco that should not have been
    ordered; the failure mode of failing closed is a talk that stalls.
    """

    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self._redis = Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _decode(raw: dict[str, str]) -> dict[str, bool]:
        return {k: v == "1" for k, v in raw.items()}

    async def is_enabled(self, tool_name: str) -> bool:
        try:
            raw = await self._redis.hmget(HASH_KEY, [GLOBAL_KEY, tool_name])
        except Exception:
            logger.warning("redis unavailable reading availability; failing open", exc_info=True)
            return True
        global_raw, tool_raw = raw[0], raw[1]
        return (global_raw != "0") and (tool_raw != "0")

    async def get_state(self) -> dict[str, bool]:
        try:
            return self._decode(await self._redis.hgetall(HASH_KEY))
        except Exception:
            logger.warning("redis unavailable reading availability state", exc_info=True)
            return {}

    async def set_state(self, target: str, enabled: bool) -> None:
        await self._redis.hset(HASH_KEY, target, "1" if enabled else "0")

    async def reset(self) -> None:
        await self._redis.delete(HASH_KEY)

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(redis_url: str | None) -> AvailabilityStore:
    if redis_url:
        logger.info("availability flags backed by Redis")
        return RedisStore(redis_url)
    logger.warning(
        "no redis_url configured; availability flags are in-process only and "
        "will reset if this process restarts"
    )
    return MemoryStore()
