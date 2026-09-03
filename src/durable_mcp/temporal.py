"""Temporal client construction."""

from __future__ import annotations

import logging

from temporalio.client import Client

from durable_mcp.config import Config

logger = logging.getLogger(__name__)


async def connect(config: Config) -> Client:
    temporal = config.temporal
    logger.info(
        "connecting to Temporal at %s (namespace=%s, tls=%s, api_key=%s)",
        temporal.address,
        temporal.namespace,
        temporal.tls,
        "yes" if temporal.api_key else "no",
    )
    return await Client.connect(
        temporal.address,
        namespace=temporal.namespace,
        api_key=temporal.api_key,
        tls=temporal.tls,
    )
