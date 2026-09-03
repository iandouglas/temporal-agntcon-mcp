# Single image, two Railway services. The only difference is the start command:
#   mcp     -> durable-mcp          (public domain + internal admin listener)
#   worker  -> durable-mcp-worker   (no domain, outbound only)
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall the world.
# The lockfile is committed and installs are --frozen: a conference demo should
# never resolve a different dependency tree than the one it was rehearsed with.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
RUN uv sync --no-dev

CMD ["uv", "run", "durable-mcp"]
