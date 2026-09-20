"""Remote Streamable HTTP entry point for Cove Sensory MCP.

This module intentionally leaves the existing local stdio entry point untouched.
It is intended for trusted remote deployments such as Render.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cove_sensory_mcp.config.secrets import KeyringSecretStore
from cove_sensory_mcp.config.store import ConfigStore
from cove_sensory_mcp.server import create_server
from cove_sensory_mcp.services import AppServices
from starlette.requests import Request
from starlette.responses import JSONResponse


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _build_remote_services() -> AppServices:
    """Build services without relying on the local-only OS path resolver."""
    config_file = Path(os.environ.get("COVE_CONFIG_FILE", "/app/data/config.yaml"))
    jobs_dir = Path(os.environ.get("COVE_JOBS_DIR", "/tmp/cove-sensory-mcp/jobs"))
    config_file.parent.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    return AppServices(
        config_store=ConfigStore(config_file, jobs_dir=jobs_dir),
        secret_store=KeyringSecretStore(),
    )


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

    server = create_server(_build_remote_services())

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
