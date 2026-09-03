"""A stand-in downstream service, for local development and tests only.

The real services live outside this repo and the audience toggles them by some
other mechanism. This stub exists so the MCP server is exercisable on a laptop:
it mirrors the stated contract exactly - `500` with an empty body while
"paused", otherwise `200` with `{"<key>": true}`.

Not deployed to Railway.
"""

from __future__ import annotations

import argparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_paused: set[str] = set()


def build_app(keys: list[str]) -> Starlette:
    async def handle(request: Request) -> Response:
        key = request.path_params["key"]
        if key in _paused:
            # Exactly the stated failure contract: 500, empty body.
            return Response(status_code=500)
        return JSONResponse({key: True})

    async def pause(request: Request) -> JSONResponse:
        key = request.path_params["key"]
        payload = await request.json()
        if payload.get("paused"):
            _paused.add(key)
        else:
            _paused.discard(key)
        return JSONResponse({"key": key, "paused": key in _paused})

    return Starlette(
        routes=[
            *[Route(f"/{key}", handle, methods=["POST"]) for key in keys],
            Route("/_chaos/{key}", pause, methods=["POST"]),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake downstream services")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--keys", default="taco,coffee,inventory,receipt")
    args = parser.parse_args()
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    print(f"stub services on :{args.port} -> {', '.join('/' + k for k in keys)}")
    print(f"pause one with: curl -XPOST localhost:{args.port}/_chaos/taco -d '{{\"paused\":true}}'")
    uvicorn.run(build_app(keys), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
