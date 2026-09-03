# Durable MCP

A remote MCP server whose every tool call runs as a Temporal workflow. Point it
at four HTTP endpoints via environment variables and it exposes four MCP tools.
When a downstream service dies, the tool call does not fail — Temporal retries
it four times a second until it succeeds.

Built for a live demo at AGNTcon/MCPcon, October 2026. The audience breaks
things; Temporal makes the breakage invisible to the agent.

- Protocol: MCP **2026-07-28** (latest), Streamable HTTP
- Durability: Temporal Cloud
- Host: Railway (two services from this one repo)

## How a tool call works

```
agent ──POST /mcp (tools/call)──▶ mcp service ──start_workflow──▶ Temporal Cloud
                                       │                              │
                            blocks, awaiting result          worker polls queue
                                       │                              │
                                       │              ┌───────────────┘
                                       │              ▼
                                       │      check availability flag (Redis)
                                       │              ▼
                                       │      POST http://taco.railway.internal/taco
                                       │              │
                                       │        500? ──▶ retry in 250ms ──┐
                                       │              │                   │
                                       │        200 {"taco": true}  ◀─────┘
                                       ▼              │
              {"completed": true, "attempts": 143} ◀──┘
```

The call blocks until the work is genuinely done. That is safe because the
`2026-07-28` transport commits `text/event-stream` for any handler that runs
silent past 15 seconds and then emits `: ping` comment lines, which keeps
Railway's proxy from closing the request. See **Timing constraints** below.

## What the agent must send

This is the most likely reason a first rehearsal fails, so it is first.

| Header | Value | Why |
|---|---|---|
| `MCP-Protocol-Version` | `2026-07-28` | **Selects the transport era.** Omit it and the SDK routes to the legacy handshake path: sessions are required and *there is no SSE keepalive*, so a blocking call gets severed after ~5 minutes behind Railway's proxy with no resumability to recover it. |
| `Mcp-Method` | e.g. `tools/call` | Required on Streamable HTTP POSTs by `2026-07-28`; a mismatch returns `HeaderMismatchError` (`-32020`). |
| `Mcp-Name` | tool name, or `""` | Same requirement. |
| `Authorization` | `Bearer <agent token>` | Missing or unknown token gets a flat `401`. |
| `Accept` | `application/json, text/event-stream` | Both, so the server may upgrade to SSE mid-request. |

Requests also carry their protocol version and capabilities in `params._meta`
(`io.modelcontextprotocol/protocolVersion`, `.../clientCapabilities`) — there is
no `initialize` handshake in `2026-07-28`. The v2 client SDK does all of this
for you; hand-rolled clients must do it themselves. The server logs a loud,
specific warning whenever a request arrives on the legacy path, so this is a
ten-second diagnosis rather than a forty-minute one.

`tests/test_http.py` contains a known-good request, headers and `_meta`
included.

## Configuration

See `.env.example` for the annotated full set. The essentials:

```
service_N_url           http://taco-service.railway.internal:8080/taco
service_N_name          Order Taco          # becomes the tool name: order_taco
service_N_description   Place an order …    # becomes the tool description
```

Scanning starts at `service_1_*` and **stops at the first gap**, so keep indexes
contiguous — `service_4_*` with no `service_3_*` is ignored, not renumbered. A
partially defined service, a non-`http(s)` URL, or two names that sanitize to the
same tool name all refuse to boot with a named error. So does a missing
`mcp_agent_tokens` or `admin_token`, unless `allow_insecure=1` is set for local
development.

Tools are **zero-argument**: the URL is POSTed as-is with an empty body.

## Two listeners, one process

| Port | Domain | Serves |
|---|---|---|
| `port` (8080) | public Railway domain | `/mcp`, `/healthz` |
| `admin_port` (8081) | **none** | `/mcp`, `/healthz`, `/admin/*` |

Railway maps one domain to one target port, so ports you do not map have no
route from the internet. The admin surface is therefore unreachable from the
conference floor rather than merely password-protected.

The `admin` role requires the admin token **and** arrival on the internal
listener. An admin token presented to the public port is downgraded to `agent`,
so a leaked token is not sufficient to reach the chaos levers.

### Roles

| Role | Condition | Sees in `tools/list` |
|---|---|---|
| `admin` | `admin_token` on the internal listener | service tools + admin tools |
| `agent` | a valid agent token | service tools only |
| — | no or unknown token | `401` |

`tools/list` results are pinned to `cacheScope: "private"`. `2026-07-28` made
list results cacheable and role filtering makes ours vary per caller — a
`"public"` scope would let a shared intermediary hand the admin tool list to an
agent.

## Control panel API

Internal listener only, `Authorization: Bearer <admin_token>`.

| Endpoint | Body | Purpose |
|---|---|---|
| `GET /admin/state` | — | Switch positions + the resolved tool table |
| `POST /admin/control` | `{"target": "global"\|"<tool>", "enabled": false}` | Flip a switch. Idempotent |
| `POST /admin/reset` | — | Everything back on, between rehearsals |
| `GET /admin/live` | — | SSE: live retry counts and switch changes |

`GET /admin/state` returns the tool table so the panel renders its buttons from
the server's actual config. Rename `service_2_name` an hour before the talk and
the panel follows.

A tool is callable when `global AND per_tool` are both on. Switching either off
is **absorbed**: the activity raises a retryable error, so the tool behaves
exactly like a dead downstream and retries until you flip it back. Tools are
never removed from `tools/list` — a vanishing tool reads as a bug, not a
failure — and a public caller therefore cannot detect switch state at all.

### The stage visual

`GET /admin/live` streams every in-flight workflow with its live `attempt` count
and last error, read from `DescribeWorkflowExecution`. Project it on a second
screen.

This is deliberately independent of the agent. `notifications/progress` would
only be visible if the agent chose to render them, and the failure mode where
Temporal retries 400 times, the order succeeds, and the audience sees nothing
but a spinner is the worst outcome available to this talk.

## Timing constraints

Verified against Railway's documented limits.

| Constraint | Value | Consequence |
|---|---|---|
| Railway closes an idle request | 5 min | Handled: the modern transport pings every 15s |
| Railway hard request cap | 15 min | `tool_call_max_wait_seconds` is 840 (14 min) |
| Activity `start_to_close` | 3s | **Load-bearing.** Without it a *hanging* service yields one attempt that never fails, so no retry fires and the counter freezes at 1 |
| HTTP client timeout | 2.5s | Under `start_to_close`, so HTTP gives up first |
| Retry interval | 250ms, constant, unlimited | ~4 attempts/sec. Retries are an attempt counter in mutable state, not history events, so a 2-minute outage stays cheap |

At 14 minutes the tool returns a deliberately generic error naming the workflow
id, and **leaves the workflow running**. Saying "disabled" there would leak
switch state to a public caller.

### Response classification

| Downstream outcome | Treated as |
|---|---|
| `200` + truthy JSON body | Success; body passed through |
| `500`/other `5xx`, timeout, connection refused | Retryable |
| `4xx` except `408`/`429` | **Non-retryable**, fails fast naming the URL |

The `4xx` row cannot happen under the stated service contract (`500` empty, or
`200 {"taco": true}`). It exists so a typo'd `service_N_url` surfaces in one
second instead of looking identical to a real outage for fourteen minutes.

## Deploying to Railway

1. **Redis** — add the Redis template. Reference it as `redis_url=${{Redis.REDIS_URL}}`.
2. **`worker` service** — this repo, config-as-code path `railway.worker.json`.
   Give it **no domain**. It is outbound-only.
3. **`mcp` service** — this repo, config-as-code path `railway.mcp.json`.
   Generate a domain and set its **target port to `8080`**. Do **not** map a
   domain to `8081`.
4. **Turn Serverless off** on both services (Settings → Serverless). Railway
   runs containers, not functions, so workers and long-lived connections work by
   default — but a sleeping MCP server means a cold start mid-talk. (The worker
   would not sleep anyway; a Temporal long-poll never looks idle.)
5. Set the environment variables from `.env.example` on both services.
6. Confirm `GET https://<your-domain>/healthz` returns `{"ok": true, ...}` and
   that `https://<your-domain>/admin/state` returns **404**.

Step 6's second half is the one worth actually checking: Railway's target-port
picker auto-detects both listening ports and asks you to choose, so a single
mis-click exposes the chaos levers to the internet.

## Local development

```bash
uv sync --extra dev

# terminal 1 - fake downstream services
uv run durable-mcp-dev-service            # :9100 with /taco /coffee /inventory /receipt

# terminal 2 - Temporal
temporal server start-dev

# terminal 3 - worker, then 4 - server
export allow_insecure=1
export service_1_url=http://127.0.0.1:9100/taco
export service_1_name='Order Taco'
export service_1_description='Order a taco'
uv run durable-mcp-worker
uv run durable-mcp
```

Break a service the way the audience will:

```bash
curl -XPOST localhost:9100/_chaos/taco -d '{"paused":true}'   # now returns 500
curl localhost:8081/admin/state
curl -N localhost:8081/admin/live                              # watch attempts climb
curl -XPOST localhost:9100/_chaos/taco -d '{"paused":false}'   # order completes
```

Nothing here is Railway-specific — the same image runs against a local dev
server, which is also the fallback if conference wifi fails.

## Tests

```bash
uv run pytest        # 54 tests
uv run ruff check .
```

- `test_config.py` — env scanning, gap handling, sanitization, boot refusals
- `test_roles.py` — token roles, the public-listener downgrade, list filtering
- `test_activities.py` — response classification, switch behaviour
- `test_http.py` — auth, listener isolation, tool visibility, `cacheScope`, control flow
- `test_e2e.py` — real Temporal dev server + worker: a dead service does not
  fail the call, a switched-off tool behaves identically, a typo'd URL fails
  fast, and a re-issued call joins the existing order instead of double-ordering

`test_e2e.py` skips automatically when the `temporal` CLI is absent.

## Known limits

- **The image is not built in CI here.** `Dockerfile` is written but was not
  built locally (no Docker available); build it once before relying on it.
- **Idempotency is best-effort.** A caller-supplied key in `_meta` makes a
  re-issued call join the existing workflow via
  `WorkflowIDConflictPolicy.USE_EXISTING`; without one, a UUID is minted per
  call and a severed-and-retried request could place a second order. Invisible
  while every service returns a hardcoded `true`.
- **Legacy-protocol clients get no keepalive.** They work, but a blocking call
  will be severed at ~5 minutes. Send `MCP-Protocol-Version: 2026-07-28`.
- **`notifications/progress` is not emitted.** By design; the dashboard is the
  visibility surface.

`DESIGN.md` records every decision and the reasoning, including the options
rejected.
