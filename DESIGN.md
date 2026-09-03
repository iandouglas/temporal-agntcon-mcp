# Durable MCP Server — Design Decisions

Demo for AGNTcon/MCPcon, late October 2026 (Ian + Melissa, Temporal).

A remote MCP server that exposes 4 configurable HTTP endpoints as MCP tools and
executes every call through a Temporal workflow, so audience-triggered failures
in the downstream services are absorbed by Temporal's retries instead of
surfacing as tool-call failures.

Narrative: fake restaurant ordering/inventory system. Tools are things like
order a taco, order a coffee, restock inventory, send a receipt. Each downstream
service returns a hardcoded payload such as `{"taco": true}` meaning the work
completed.

## Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Purpose | Conference demo; we control the agent (client) side too |
| 2 | MCP spec revision | `2026-07-28` (latest) |
| 3 | `tools/call` shape | **Blocking**. No `notifications/progress`; SSE comment heartbeats keep Railway's proxy fed |
| 4 | Tool input schema | **Zero-argument** — POST the configured URL as-is, empty body |
| 5 | Chaos on the services | Out of scope for this server; a separate audience-facing mechanism toggles them |
| 6 | Per-tool fake availability | Togglable flag, absorbed as a **retryable failure** (Temporal retries until flipped back) |
| 7 | Toggle store | **Redis** on Railway, fail-open if unreachable |
| 8 | Toggle transport | Plain HTTP admin endpoints, **not** an MCP tool (keeps it out of the LLM's reach) |
| 9 | Deployment | **Two Railway services from this one repo**: `mcp` (public domain) + `worker` (no domain) |
| 10 | Temporal backend | Temporal Cloud |
| 11 | Downstream reachability | Railway **private network** (`*.railway.internal`, `http://`), nothing external |
| 12 | MCP endpoint reachability | **Public** — the agent calls it from outside Railway |
| 13 | Language | **Python** — MCP Python SDK v2 + Temporal Python SDK |
| 14 | Auth | Two static bearer tokens: `MCP_AUTH_TOKEN` (MCP), `ADMIN_TOKEN` (`/admin/*`) |
| 15 | Downstream contract | `500` + empty body → retry. `200` + `{"<name>": true}` → done. Nothing else occurs |
| 16 | Retry visibility | **Server-side SSE dashboard** at `GET /admin/live`, projected on a second screen |

## The stage visual

The audience watches `GET /admin/live` — an SSE stream of every in-flight workflow with
its live attempt count and last error, read from `DescribeWorkflowExecution`
(`pendingActivities[].attempt`). Projected on a second screen.

This is deliberately **independent of the agent**. `notifications/progress` would only be
visible if the agent chose to render them, and the agent is a separate workstream; the
failure mode where Temporal retries 400 times, the order succeeds, and the audience sees
nothing but a spinner is the worst outcome available to this talk. The dashboard removes
that risk entirely.

## Keeping the connection alive

`tools/call` blocks, so the response must keep transferring bytes or Railway's edge proxy
severs it at 5:00 — and `2026-07-28` has no resumability to recover from that.

- Response is `text/event-stream`.
- A bare SSE **comment** line (`: keepalive`) every 30s. Comments are not MCP protocol
  messages, so no client parses them and nothing depends on agent behaviour, but they are
  bytes on the wire and Railway's 5-minute idle timer resets.
- Fallback if the SDK makes raw comment injection awkward: emit `notifications/progress`
  purely as a heartbeat. Any agent ignores it harmlessly.
- Ceiling stays the 15-minute hard cap. Workflow wait is capped at ~14 minutes; on
  expiry return an error naming the workflow ID and **leave the workflow running** — it
  is still durable, and pointing at the Temporal UI is on-message.

## Temporal configuration

- Activity `start_to_close_timeout`: **3s**
- HTTP client timeout inside the activity: **2.5s** (must be under start_to_close)
- Retry policy: `initialInterval 250ms`, `backoffCoefficient 1.0`, `maximumAttempts` unlimited
- Rationale: a short start_to_close is what makes a *hanging* service retry at all.
  Without it, a hang produces exactly one activity attempt that never fails, the
  retry counter freezes at 1, and nothing appears on stage.
- Activity retry attempts do **not** each write history events — they are an attempt
  counter in mutable state under one `ActivityTaskScheduled`. A two-minute outage at
  4 attempts/sec is ~480 attempts and a still-tiny history.
- The availability flag is read **inside the activity on every attempt**, never in
  workflow code: reading Redis from a workflow is non-deterministic, and the value
  would be frozen into history at the first attempt so flipping it back would never
  be noticed.

## Railway constraints that shape the implementation

Verified against Railway docs:

- Edge proxy closes an HTTP request after **5 minutes with no data transferred**;
  with heartbeats a request may stay open to a **15-minute hard cap**.
  → `tools/call` MUST respond with `text/event-stream` and emit progress
    notifications (~every 2s). Those notifications *are* the keep-alive. A buffered
    JSON response transfers zero bytes during an outage and gets severed at 5:00.
  → Cap the workflow wait around 14 minutes and return a clean "still working"
    result rather than letting Railway sever the stream.
- `2026-07-28` removed SSE resumability (`Last-Event-ID`, event IDs). A severed
  stream loses the in-flight request; there is no resume. Hence the caps above.
- **Serverless / App Sleeping must be disabled** on both services. Containers are not
  functions, so workers and long-lived connections work by default, but a sleeping
  MCP server means a cold start mid-talk.
- The worker needs **no public domain**; it is outbound-only to Temporal Cloud and to
  the services.
- Private networking is **runtime-only** — no build step may reach the services.
- Environments created after 2025-10-16 resolve `.railway.internal` to IPv4 *and*
  IPv6; only legacy environments are IPv6-only.

## Configuration

Per service N in 1..4:

    service_N_url          e.g. http://taco-service.railway.internal:8080/taco
    service_N_name         MCP tool name
    service_N_description  MCP tool description

## Classification rules

| Downstream outcome | Treated as |
|---|---|
| `200` + truthy JSON body | Success — body passed through as the tool result |
| `500`, other `5xx`, empty body | Retryable |
| Timeout / connection refused / DNS failure | Retryable |
| `4xx` (`400/401/403/404/405`) | **Non-retryable**, fails fast naming the URL |

The `4xx` row cannot happen under the stated service contract. It exists purely so a
typo'd `service_N_url` surfaces in one second instead of looking identical to a genuine
outage for fourteen minutes.

## Notes / smaller decisions

- Idempotency: honor an optional key from request `_meta` if present, otherwise mint a
  UUID workflow ID per call. Duplicate orders are invisible when every service returns
  a hardcoded `true`, so this is deliberately not over-engineered.
- Progress data comes from `DescribeWorkflowExecution` — `pendingActivities[].attempt`
  gives the live retry count to put on screen.

### Asserted without asking

- **Service discovery:** scan `service_1_*` upward until a gap rather than hardcoding 4.
  Validate at boot, log the resolved tool table, refuse to boot on a partial service
  definition or a tool-name collision.
- **Tool names:** sanitized from `service_N_name` (lowercased, spaces to underscores,
  invalid characters stripped) to satisfy the MCP tool-name constraints.
- **Workflow shape:** one `ServiceCallWorkflow` parameterized by service key. Workflow ID
  `{tool_name}-{uuid}`. Single task queue.
- **Tool result:** text content carrying the raw body, plus `structuredContent`.
- **Admin surface:** `POST /admin/services/{name}/state`, `GET /admin/state`,
  `POST /admin/reset` (all available, for between rehearsals), `GET /admin/live` (SSE).
  Same service as MCP, gated by `ADMIN_TOKEN`.
- **Boot safety:** refuse to start if either token is unset.
- **Redis:** fail-open. A Redis blip must never wedge the demo.
- **No Railway-specific code.** Pure 12-factor, so the identical image runs against
  `temporal server start-dev` and local services as a conference-wifi fallback.

## Deliverables

- `railway.json` / service definitions for `mcp` + `worker`
- Dockerfile (or nixpacks config), `.env.example`
- README with exact Railway setup steps, including the **disable Serverless** checklist
- A dev stub service and tests, so this repo is exercisable without `../service`
