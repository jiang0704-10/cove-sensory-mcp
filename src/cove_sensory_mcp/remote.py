"""Remote Streamable HTTP entry point for Cove Sensory MCP.

This module intentionally leaves the existing local stdio entry point untouched.
It is intended for trusted remote deployments such as Render.
"""

from __future__ import annotations

import hmac
import logging
import os

from cove_sensory_mcp.cli import _build_services
from cove_sensory_mcp.server import create_server
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def main() -> None:
    """Serve the existing sensory toolset over MCP Streamable HTTP."""
    host = os.environ.get("HOST", "0.0.0.0")
    port = _env_int("PORT", 8000)
    path = os.environ.get("COVE_MCP_PATH", "/mcp")
    if not path.startswith("/"):
        raise ValueError("COVE_MCP_PATH must start with '/'")

    logging.basicConfig(
        level=os.environ.get("COVE_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger(__name__).info(
        "Starting Cove Sensory MCP remote server on %s:%s%s", host, port, path
    )

    server = create_server(_build_services())

    # Optional deployment guard. When set, requests to the MCP endpoint must
    # present the exact bearer token. Health remains public for platform probes.
    remote_token = os.environ.get("COVE_REMOTE_BEARER_TOKEN", "").strip()
    if remote_token:
        original_middleware = list(getattr(server.settings, "middleware", []))

        from starlette.middleware.base import BaseHTTPMiddleware

        class _BearerGuard(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
                if request.url.path == path:
                    supplied = request.headers.get("authorization", "")
                    expected = f"Bearer {remote_token}"
                    if not hmac.compare_digest(supplied, expected):
                        return PlainTextResponse("Unauthorized", status_code=401)
                return await call_next(request)

        # FastMCP accepts Starlette Middleware entries in settings.middleware.
        from starlette.middleware import Middleware
        server.settings.middleware = [Middleware(_BearerGuard), *original_middleware]

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "cove-sensory-mcp"})

    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
