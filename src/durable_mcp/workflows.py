"""The durable half of a tool call."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from durable_mcp.shared import CALL_SERVICE_ACTIVITY, ServiceCallInput, ServiceCallOutput


@workflow.defn(name="ServiceCallWorkflow")
class ServiceCallWorkflow:
    """POST a configured endpoint until it succeeds.

    One activity, retried aggressively and indefinitely. All the durability the
    demo needs lives in the retry policy plus the fact that this workflow's
    state outlives the worker executing it: kill the worker mid-outage and the
    attempt counter resumes where it left off when a worker comes back.
    """

    @workflow.run
    async def run(self, args: ServiceCallInput) -> ServiceCallOutput:
        return await workflow.execute_activity(
            CALL_SERVICE_ACTIVITY,
            args,
            # Short, and this is the load-bearing setting. Without it a
            # downstream that *hangs* produces exactly one activity attempt
            # that never fails, so no retry ever fires, the attempt counter
            # freezes at 1, and the audience sees nothing happen.
            start_to_close_timeout=timedelta(seconds=args.start_to_close_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=args.retry_initial_interval_ms),
                # Constant, not exponential: the point on stage is a visibly
                # sprinting attempt counter, not polite backoff.
                backoff_coefficient=1.0,
                maximum_interval=timedelta(milliseconds=args.retry_initial_interval_ms),
                # Never give up. A caller-side deadline ends the wait; the
                # workflow itself keeps going and stays inspectable.
                maximum_attempts=0,
            ),
            result_type=ServiceCallOutput,
        )
