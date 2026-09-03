"""Temporal worker entrypoint.

Deployed as its own Railway service with no public domain: outbound-only to
Temporal Cloud and to the downstream services over the private network.

Running it separately is what makes the stage beat possible. Kill this service
mid-outage and the agent's in-flight tool call stays open with its attempt
counter frozen; restart it and the counter resumes and the order completes,
with nothing lost and no connection dropped.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.worker import Worker

from durable_mcp.activities import ServiceActivities
from durable_mcp.config import load_config
from durable_mcp.state import build_store
from durable_mcp.temporal import connect
from durable_mcp.workflows import ServiceCallWorkflow

logger = logging.getLogger(__name__)


async def run() -> None:
    config = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    logger.info("worker serving %d tool(s):", len(config.services))
    for service in config.services:
        logger.info("  %-24s -> %s", service.tool_name, service.url)

    store = build_store(config.redis_url)
    activities = ServiceActivities(store, config.http_timeout_seconds)
    client = await connect(config)

    worker = Worker(
        client,
        task_queue=config.temporal.task_queue,
        workflows=[ServiceCallWorkflow],
        activities=[activities.call_service],
    )
    logger.info("worker polling task queue %r", config.temporal.task_queue)
    try:
        await worker.run()
    finally:
        await activities.aclose()
        await store.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
