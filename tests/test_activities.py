"""Response classification: what retries, what fails fast, what succeeds."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from durable_mcp import dev_service
from durable_mcp.activities import ServiceActivities
from durable_mcp.shared import ServiceCallInput
from durable_mcp.state import GLOBAL_KEY, MemoryStore

BASE = "http://svc"


@pytest.fixture(autouse=True)
def _reset_chaos() -> Iterator[None]:
    dev_service._paused.clear()
    yield
    dev_service._paused.clear()


def _args(path: str = "/taco") -> ServiceCallInput:
    return ServiceCallInput(
        tool_name="order_taco",
        url=f"{BASE}{path}",
        caller="test",
        start_to_close_seconds=3.0,
        retry_initial_interval_ms=250,
    )


def _activities(store: MemoryStore | None = None) -> tuple[ServiceActivities, MemoryStore]:
    resolved = store or MemoryStore()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dev_service.build_app(["taco"])),
        base_url=BASE,
    )
    return ServiceActivities(resolved, 2.5, client=client), resolved


async def test_success_returns_body_and_attempt() -> None:
    activities, _ = _activities()
    result = await ActivityEnvironment().run(activities.call_service, _args())
    assert result.status_code == 200
    assert result.body == {"taco": True}
    assert result.attempts == 1


async def test_500_is_retryable() -> None:
    """The stated contract for a paused service: 500 with an empty body."""
    activities, _ = _activities()
    dev_service._paused.add("taco")

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.non_retryable is False
    assert excinfo.value.type == "DownstreamError"


async def test_toggled_off_tool_is_retryable_not_an_error_the_caller_sees() -> None:
    activities, store = _activities()
    await store.set_state("order_taco", enabled=False)

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.type == "ToolUnavailable"
    assert excinfo.value.non_retryable is False


async def test_master_switch_off_disables_every_tool() -> None:
    activities, store = _activities()
    await store.set_state(GLOBAL_KEY, enabled=False)

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.type == "ToolUnavailable"


async def test_toggling_back_on_lets_the_call_through() -> None:
    activities, store = _activities()
    await store.set_state("order_taco", enabled=False)
    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(activities.call_service, _args())

    await store.set_state("order_taco", enabled=True)
    result = await ActivityEnvironment().run(activities.call_service, _args())
    assert result.body == {"taco": True}


async def test_404_fails_fast_so_a_url_typo_is_obvious() -> None:
    """A typo'd URL must not look identical to an outage for fourteen minutes."""
    activities, _ = _activities()

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args("/tacoo"))
    assert excinfo.value.non_retryable is True
    assert excinfo.value.type == "DownstreamConfigError"
    assert "/tacoo" in str(excinfo.value)


async def test_connection_failure_is_retryable() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url=BASE)
    activities = ServiceActivities(MemoryStore(), 2.5, client=client)

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.non_retryable is False
    assert excinfo.value.type == "DownstreamUnreachable"


async def test_timeout_is_retryable() -> None:
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow), base_url=BASE)
    activities = ServiceActivities(MemoryStore(), 2.5, client=client)

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.non_retryable is False
    assert excinfo.value.type == "DownstreamTimeout"


async def test_429_retries_even_though_it_is_4xx() -> None:
    def throttled(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = httpx.AsyncClient(transport=httpx.MockTransport(throttled), base_url=BASE)
    activities = ServiceActivities(MemoryStore(), 2.5, client=client)

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(activities.call_service, _args())
    assert excinfo.value.non_retryable is False
