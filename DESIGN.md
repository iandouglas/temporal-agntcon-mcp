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
| 3 | `tools/call` shape | **Blocking**, with `notifications/progress` streamed while Temporal retries |
| 4 | Tool input schema | **Zero-argument** — POST the configured URL as-is, empty body |
| 5 | Chaos on the services | Out of scope for this server; a separate audience-facing mechanism toggles them |
| 6 | Per-tool fake availability | Togglable flag, absorbed as a **retryable failure** (Temporal retries until flipped back) |
| 7 | Toggle store | **Redis** on Railway, fail-open if unreachable |
| 8 | Toggle transport | Plain HTTP admin endpoints, **not** an MCP tool (keeps it out of the LLM's reach) |
| 9 | Deployment | **Two Railway services from this one repo**: `mcp` (public domain) + `worker` (no domain) |
| 10 | Temporal backend | Temporal Cloud |
| 11 | Downstream reachability | Railway **private network** (`*.railway.internal`, `http://`), nothing external |
| 12 | MCP endpoint reachability | **Public** — the agent calls it from outside Railway |

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

## Notes / smaller decisions

- Idempotency: honor an optional key from request `_meta` if present, otherwise mint a
  UUID workflow ID per call. Duplicate orders are invisible when every service returns
  a hardcoded `true`, so this is deliberately not over-engineered.
- Progress data comes from `DescribeWorkflowExecution` — `pendingActivities[].attempt`
  gives the live retry count to put on screen.

## Open questions

- Implementation language / MCP SDK
- Auth on the public MCP endpoint and on the admin toggle endpoints
- Response-body validation: is HTTP 200 enough, or must the payload be truthy?
- Tool-name sanitization from `service_N_name`
- Fixed 4 services vs. scanning `service_N_*` until a gap
