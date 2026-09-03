"""Types crossing the workflow/activity boundary.

Kept import-light on purpose: the workflow sandbox imports this module, and
pulling httpx or redis in through the back door is how sandbox restrictions
turn into confusing import errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CALL_SERVICE_ACTIVITY = "call_service"


@dataclass
class ServiceCallInput:
    """One tool invocation.

    Timings travel with the input rather than being read from the environment
    inside the workflow. Workflow code must be deterministic across replay, and
    an env var that changed between the original run and a replay would produce
    a different activity command.
    """

    tool_name: str
    url: str
    caller: str
    start_to_close_seconds: float
    retry_initial_interval_ms: int


@dataclass
class ServiceCallOutput:
    """A completed downstream call."""

    status_code: int
    body: Any
    raw_text: str
    attempts: int
