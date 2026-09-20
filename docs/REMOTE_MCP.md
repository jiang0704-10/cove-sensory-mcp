# Remote MCP deployment (experimental)

This branch adds a remote Streamable HTTP transport without changing the existing
local stdio server.

## What it does

`python -m cove_sensory_mcp.remote` exposes the same seven Cove sensory tools
over MCP Streamable HTTP. The default endpoint is `/mcp`.

Environment variables:

- `HOST` — bind host, default `0.0.0.0`.
- `PORT` — bind port, default `8000` (hosting platforms normally inject this).
- `COVE_MCP_PATH` — MCP path, default `/mcp`.
- `COVE_LOG_LEVEL` — Python log level, default `INFO`.

The server is stateless at the MCP transport layer and returns JSON responses.
The original `cove-sensory-mcp serve` stdio behavior is unchanged.

## Provider configuration

Cove's existing provider/configuration system is intentionally unchanged in this
first remote transport patch. A remote instance still needs a valid Cove config
and provider credentials. Prefer environment-backed credentials
(`env:VARIABLE_NAME`) rather than putting API keys in files or source control.

For an OpenAI-compatible relay such as GemAI, use Cove's existing custom provider
support. Capability verification must succeed before enabling a modality. Do not
assume that a relay's text compatibility implies image/audio/video compatibility.

## Render

`Dockerfile.remote` installs FFmpeg and runs the remote entry point.
`render.remote.yaml` is a starter Blueprint.

The MCP endpoint after deployment is:

`https://<service-host>/mcp`

## Security status

This first patch is a transport/deployment scaffold. It does **not** add a new
authentication system. Do not expose a quota-bearing provider behind a public
production URL until authentication (or an equivalent trusted gateway) is in
place.

A later hardening patch should add standards-compatible MCP authorization and a
non-secret health endpoint before production use.
