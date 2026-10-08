"""End-to-end: a real Temporal dev server, a real worker, a real HTTP service.

These are the regression tests for the demo itself. They prove the two things
that have to be true on stage: a dead service does not fail the tool call, and
a typo'd URL does not masquerade as a dead service.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_mcp import dev_service
from durable_mcp.activities import ServiceActivities
from durable_mcp.config import Config, ServiceConfig, TemporalConfig
from durable_mcp.mcp_app import _make_service_tool
from durable_mcp.shared import ServiceCallInput
from durable_mcp.state import MemoryStore
from durable_mcp.workflows import ServiceCallWorkflow

TASK_QUEUE = "durable-mcp-test"
BASE = "http://svc"
RETRY_MS = 100

_TEMPORAL_CLI = shutil.which("temporal")
pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="temporal CLI not available for a local dev server"
)


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    # Real time, not time-skipping: the point is to observe retries actually
    # accumulating the way the audience will see them.
    environment = await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI)
    try:
        yield environment
    finally:
        await environment.shutdown()


@pytest_asyncio.fixture(autouse=True)
async def _reset_chaos() -> AsyncIterator[None]:
    dev_service._paused.clear()
    yield
    dev_service._paused.clear()


@pytest_asyncio.fixture
async def store() -> MemoryStore:
    return MemoryStore()


@pytest_asyncio.fixture
async def worker(env: WorkflowEnvironment, store: MemoryStore) -> AsyncIterator[Worker]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dev_service.build_app(["taco"])),
        base_url=BASE,
    )
    activities = ServiceActivities(store, 2.5, client=client)
    running = Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ServiceCallWorkflow],
        activities=[activities.call_service],
    )
    async with running:
        yield running
    await activities.aclose()


def _args(path: str = "/taco") -> ServiceCallInput:
    return ServiceCallInput(
        tool_name="order_taco",
        url=f"{BASE}{path}",
        caller="test",
        start_to_close_seconds=3.0,
        retry_initial_interval_ms=RETRY_MS,
    )


async def _start(client: Client, workflow_id: str, path: str = "/taco"):
    return await client.start_workflow(
        ServiceCallWorkflow.run,
        _args(path),
        id=workflow_id,
        task_queue=TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def _attempt_count(handle) -> int:
    """Read the live retry count exactly the way the stage dashboard does."""
    description = await handle.describe()
    pending = list(description.raw_description.pending_activities)
    return pending[0].attempt if pending else 0


async def test_happy_path(env: WorkflowEnvironment, worker: Worker) -> None:
    handle = await _start(env.client, "e2e-happy")
    result = await handle.result()
    assert result.status_code == 200
    assert result.body == {"taco": True}


async def test_dead_service_does_not_fail_the_call(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    """The demo, in one test.

    The service is dead when the order is placed. The workflow does not fail; it
    retries. Un-pausing the service completes the original call, and the attempt
    counter proves Temporal did the work rather than the caller getting lucky.
    """
    dev_service._paused.add("taco")
    handle = await _start(env.client, "e2e-outage")

    # Let it accumulate retries while the service is down.
    await asyncio.sleep(1.0)
    attempts_during_outage = await _attempt_count(handle)
    assert attempts_during_outage > 1, "expected visible retries while the service was down"

    # Nothing has failed - it is still working.
    description = await handle.describe()
    assert description.status is not None
    assert description.status.name == "RUNNING"

    dev_service._paused.discard("taco")
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.body == {"taco": True}
    assert result.attempts > 1


async def test_unavailable_tool_behaves_exactly_like_a_dead_service(
    env: WorkflowEnvironment, worker: Worker, store: MemoryStore
) -> None:
    """Flipping the MCP switch off is absorbed, not surfaced as an error."""
    await store.set_state("order_taco", enabled=False)
    handle = await _start(env.client, "e2e-toggle")

    await asyncio.sleep(1.0)
    assert await _attempt_count(handle) > 1
    description = await handle.describe()
    assert description.status.name == "RUNNING"

    await store.set_state("order_taco", enabled=True)
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.body == {"taco": True}


def _root_cause(error: BaseException) -> BaseException:
    """Unwrap WorkflowFailureError -> ActivityError -> ApplicationError."""
    current = error
    while (cause := getattr(current, "cause", None)) is not None:
        current = cause
    return current


async def test_url_typo_fails_fast_instead_of_retrying_forever(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    """A 404 is a config error, and must not hide inside an infinite retry loop."""
    handle = await _start(env.client, "e2e-typo", path="/tacoo")
    with pytest.raises(WorkflowFailureError) as excinfo:
        await asyncio.wait_for(handle.result(), timeout=30)

    root = _root_cause(excinfo.value)
    assert isinstance(root, ApplicationError)
    assert root.type == "DownstreamConfigError"
    assert root.non_retryable is True
    assert "/tacoo" in str(root)


async def test_same_idempotency_key_joins_the_existing_order(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    """A re-issued tool call must not place a second order."""
    dev_service._paused.add("taco")
    first = await _start(env.client, "e2e-idem")
    await asyncio.sleep(0.3)
    second = await _start(env.client, "e2e-idem")
    assert second.result_run_id == first.result_run_id

    dev_service._paused.discard("taco")
    assert (await asyncio.wait_for(first.result(), timeout=30)).body == {"taco": True}


# --------------------------------------------------------------------------
# Idempotency across a COMPLETED workflow.
#
# `id_conflict_policy=USE_EXISTING` only covers a workflow that is still
# running. Melissa's agent derives its keys from workflow state alone, so they
# are byte-identical across a replay: a worker killed after we already finished
# re-issues the exact same key. With the default reuse policy that started a
# second run -- a second taco -- while her scoreboard counted it as a duplicate
# she had prevented.
# --------------------------------------------------------------------------


class _FakeMeta:
    def __init__(self, key: str) -> None:
        self.idempotencyKey = key  # noqa: N815 - mirrors the wire field name


class _FakeRequestContext:
    def __init__(self, key: str) -> None:
        self.meta = _FakeMeta(key)


class _FakeContext:
    """Just enough of an MCP Context for the tool body."""

    def __init__(self, key: str) -> None:
        self.headers: dict[str, str] = {}
        self.request_context = _FakeRequestContext(key)


_SERVICE = ServiceConfig(
    index=1,
    tool_name="order_taco",
    display_name="Order Taco",
    description="orders a taco",
    url=f"{BASE}/taco",
)


def _tool_config() -> Config:
    return Config(
        services=(_SERVICE,),
        temporal=TemporalConfig(
            address="unused", namespace="default", task_queue=TASK_QUEUE,
            api_key=None, tls=False,
        ),
        redis_url=None,
        agent_tokens={},
        admin_token=None,
        public_port=0,
        admin_port=0,
        bind_host="127.0.0.1",
        activity_start_to_close_seconds=3.0,
        http_timeout_seconds=2.5,
        retry_initial_interval_ms=RETRY_MS,
        tool_call_max_wait_seconds=30.0,
        keepalive_interval_seconds=5.0,
        dashboard_poll_seconds=1.0,
        tools_list_ttl_ms=0,
        allow_insecure=True,
    )


async def test_the_same_key_never_orders_twice(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    config = _tool_config()
    tool = _make_service_tool(config, _SERVICE, lambda: env.client)

    first = await tool(_FakeContext("stable-key-1"))
    assert first["completed"] is True

    # Same key again, long after the first workflow closed: the re-issued call
    # must attach to that execution and return what it returned, not start a
    # second one.
    second = await tool(_FakeContext("stable-key-1"))

    assert second["workflow_id"] == first["workflow_id"]
    assert second["response"] == first["response"]

    # The real proof: one execution, one run. A second run under the same id
    # would be a second taco.
    runs = [
        execution
        async for execution in env.client.list_workflows(
            f"WorkflowId = '{first['workflow_id']}'"
        )
    ]
    assert len(runs) == 1, f"expected one run, found {len(runs)}"


async def test_distinct_keys_still_order_separately(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    """The dedup must not be so eager that two real orders collapse into one."""
    config = _tool_config()
    tool = _make_service_tool(config, _SERVICE, lambda: env.client)

    first = await tool(_FakeContext("distinct-a"))
    second = await tool(_FakeContext("distinct-b"))

    assert first["workflow_id"] != second["workflow_id"]
