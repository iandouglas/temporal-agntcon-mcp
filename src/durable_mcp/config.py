"""Environment-driven configuration.

Everything this server knows about the outside world arrives as environment
variables, so the identical image runs on Railway and against a local
`temporal server start-dev` with nothing but a different `.env`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAX_SERVICES = 64
_TOOL_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class ConfigError(Exception):
    """Raised at boot for a configuration problem we refuse to start with."""


def _env(*names: str, default: str | None = None) -> str | None:
    """First non-empty match, trying each name lowercase then uppercase.

    The service variables were specified lowercase (`service_1_url`). Env vars
    are case-sensitive on Linux, and dashboards love to uppercase things, so
    accept either rather than failing mysteriously at 10am.
    """
    for name in names:
        for candidate in (name, name.upper()):
            value = os.environ.get(candidate)
            if value is not None and value.strip():
                return value.strip()
    return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def sanitize_tool_name(raw: str) -> str:
    """Turn a human `service_N_name` into a legal MCP tool name.

    "Order Taco" -> "order_taco". Raises rather than silently producing an
    empty name, because a nameless tool is a boot-time bug, not a runtime one.
    """
    name = re.sub(r"[^a-z0-9_-]+", "_", raw.strip().lower())
    name = re.sub(r"_{2,}", "_", name).strip("_-")[:128]
    if not name or not _TOOL_NAME_OK.match(name):
        raise ConfigError(f"service name {raw!r} does not sanitize to a usable tool name")
    return name


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """One downstream endpoint, exposed as one zero-argument MCP tool."""

    index: int
    tool_name: str
    display_name: str
    description: str
    url: str


@dataclass(frozen=True, slots=True)
class TemporalConfig:
    address: str
    namespace: str
    task_queue: str
    api_key: str | None
    tls: bool

    @property
    def is_cloud(self) -> bool:
        return self.api_key is not None


@dataclass(frozen=True, slots=True)
class Config:
    services: tuple[ServiceConfig, ...]
    temporal: TemporalConfig
    redis_url: str | None
    agent_tokens: dict[str, str]
    """token -> caller label, so logs can say *which* agent placed an order."""
    admin_token: str | None
    public_port: int
    admin_port: int
    bind_host: str
    activity_start_to_close_seconds: float
    http_timeout_seconds: float
    retry_initial_interval_ms: int
    tool_call_max_wait_seconds: float
    keepalive_interval_seconds: float
    dashboard_poll_seconds: float
    tools_list_ttl_ms: int
    allow_insecure: bool
    services_by_tool: dict[str, ServiceConfig] = field(default_factory=dict)

    def service_for(self, tool_name: str) -> ServiceConfig | None:
        return self.services_by_tool.get(tool_name)


def _load_services() -> tuple[ServiceConfig, ...]:
    """Scan `service_1_*` upward, stopping at the first gap.

    Scanning beats hardcoding four: the demo can grow a fifth service by adding
    env vars, and a typo'd index shows up as a missing service at boot instead
    of a tool that silently never appears.
    """
    services: list[ServiceConfig] = []
    seen: dict[str, int] = {}

    for index in range(1, MAX_SERVICES + 1):
        prefix = f"service_{index}"
        url = _env(f"{prefix}_url")
        display_name = _env(f"{prefix}_name")
        description = _env(f"{prefix}_description")

        if url is None and display_name is None and description is None:
            break

        # A partial definition is always a mistake. Failing here turns a typo
        # into an instant, named boot error instead of a missing tool nobody
        # notices until the agent asks for it on stage.
        missing = [
            part
            for part, value in (("url", url), ("name", display_name), ("description", description))
            if value is None
        ]
        if missing:
            raise ConfigError(
                f"{prefix} is partially defined: missing {', '.join(f'{prefix}_{m}' for m in missing)}"
            )
        assert url is not None and display_name is not None and description is not None

        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{prefix}_url must start with http:// or https://, got {url!r}")

        tool_name = sanitize_tool_name(display_name)
        if tool_name in seen:
            raise ConfigError(
                f"{prefix}_name {display_name!r} collides with service_{seen[tool_name]} "
                f"(both sanitize to tool name {tool_name!r})"
            )
        seen[tool_name] = index

        services.append(
            ServiceConfig(
                index=index,
                tool_name=tool_name,
                display_name=display_name,
                description=description,
                url=url,
            )
        )

    return tuple(services)


def _load_agent_tokens() -> dict[str, str]:
    """Parse `MCP_AGENT_TOKENS` as comma-separated `label:token` (or bare token)."""
    raw = _env("mcp_agent_tokens", "mcp_auth_token")
    tokens: dict[str, str] = {}
    if not raw:
        return tokens
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        label, _, token = part.rpartition(":")
        token = token.strip()
        label = label.strip() or "agent"
        if not token:
            raise ConfigError(f"malformed entry in mcp_agent_tokens: {part!r}")
        tokens[token] = label
    return tokens


def load_config() -> Config:
    services = _load_services()
    if not services:
        raise ConfigError(
            "no services configured; set service_1_url, service_1_name, service_1_description"
        )

    agent_tokens = _load_agent_tokens()
    admin_token = _env("admin_token")
    allow_insecure = _env_bool("allow_insecure")

    # Refusing to boot beats deploying an open server by accident. The escape
    # hatch is explicit and named, so it can't happen without someone meaning it.
    if not allow_insecure:
        if not agent_tokens:
            raise ConfigError(
                "mcp_agent_tokens is required (or set allow_insecure=1 for local dev)"
            )
        if not admin_token:
            raise ConfigError("admin_token is required (or set allow_insecure=1 for local dev)")

    api_key = _env("temporal_api_key")
    temporal = TemporalConfig(
        address=_env("temporal_address", default="localhost:7233") or "localhost:7233",
        namespace=_env("temporal_namespace", default="default") or "default",
        task_queue=_env("temporal_task_queue", default="durable-mcp") or "durable-mcp",
        api_key=api_key,
        # Temporal Cloud is TLS; a local dev server is not. Keyed off the API
        # key so the common cases need no extra variable.
        tls=_env_bool("temporal_tls", default=api_key is not None),
    )

    services_by_tool = {s.tool_name: s for s in services}

    return Config(
        services=services,
        temporal=temporal,
        redis_url=_env("redis_url"),
        agent_tokens=agent_tokens,
        admin_token=admin_token,
        public_port=_env_int("port", 8080),
        admin_port=_env_int("admin_port", 8081),
        bind_host=_env("bind_host", default="0.0.0.0") or "0.0.0.0",
        activity_start_to_close_seconds=_env_float("activity_start_to_close_seconds", 3.0),
        http_timeout_seconds=_env_float("http_timeout_seconds", 2.5),
        retry_initial_interval_ms=_env_int("retry_initial_interval_ms", 250),
        # Railway severs any request at a 15-minute hard cap. Finish first, with
        # a margin, so the client gets a real answer instead of a dead socket.
        tool_call_max_wait_seconds=_env_float("tool_call_max_wait_seconds", 840.0),
        # Railway also closes a request idle for 5 minutes. Keepalive bytes are
        # what keep a blocking tool call alive through an outage.
        keepalive_interval_seconds=_env_float("keepalive_interval_seconds", 30.0),
        dashboard_poll_seconds=_env_float("dashboard_poll_seconds", 0.5),
        tools_list_ttl_ms=_env_int("tools_list_ttl_ms", 30_000),
        allow_insecure=allow_insecure,
        services_by_tool=services_by_tool,
    )
