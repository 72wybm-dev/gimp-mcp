# GIMP MCP

This directory contains a local Model Context Protocol server for GIMP and a
GIMP Python plug-in that exposes GIMP's Procedure Database (PDB) over a local
Unix socket.

The design is intentionally small:

- `gimp_mcp.server` is the MCP stdio server used by an MCP client.
- `bridge/gimp_mcp_bridge.py` is installed as a GIMP plug-in.
- The bridge exposes PDB discovery and calls, so the MCP server can reach the
  broad GIMP feature surface without registering thousands of MCP tools.

## Install

The scripts default to the installed app at `/Applications/GIMP.app`.
The installer auto-detects the newest user profile under
`~/Library/Application Support/GIMP`, such as `3.2`.

```sh
git clone https://github.com/your-org/gimp-mcp.git
cd gimp-mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
./scripts/install_bridge.sh
./scripts/smoke_test.sh
```

For a non-standard GIMP app path:

```sh
GIMP_APP=/path/to/GIMP.app ./scripts/install_bridge.sh
```

Restart GIMP after installing the bridge.

## Start The MCP Server

For normal use, point your MCP client at:

```sh
/path/to/gimp-mcp/scripts/run_mcp_server.sh
```

The repository also includes `.mcp.json` for clients that support project-local
MCP discovery.

That script starts the MCP stdio server and automatically launches a private
headless GIMP bridge if the socket is not already running. Users do not need to
open GIMP or click a GIMP menu for automation workflows.

Equivalent direct command:

```sh
gimp-mcp --auto-start-bridge --gimp-app /Applications/GIMP.app
```

The default bridge socket is:

```text
/tmp/gimp-mcp-$USER.sock
```

Set `GIMP_MCP_SOCKET` before launching GIMP and the MCP server if you want a
different socket path.

### Manual Debug Mode

The plug-in still provides a manual fallback for debugging against a visible
GIMP session:

```text
Filters > Development > Start MCP Bridge
```

This is not the recommended production path. It is useful when you explicitly
want MCP calls to operate on the visible GIMP app process.

## MCP Server Command

Use this command in an MCP client configuration:

```sh
/path/to/gimp-mcp/scripts/run_mcp_server.sh
```

Codex can be configured with:

```toml
[mcp_servers.gimp]
command = "/absolute/path/to/gimp-mcp/scripts/run_mcp_server.sh"
```

Or, without the wrapper script:

```sh
gimp-mcp --auto-start-bridge --gimp-app /Applications/GIMP.app
```

## Tools

The MCP server exposes:

- `gimp.status`
- `gimp.get_info`
- `gimp.get_context_state`
- `gimp.get_image_bitmap`
- `gimp.list_images`
- `gimp.get_image`
- `gimp.procedure.search`
- `gimp.procedure.describe`
- `gimp.procedure.call`
- `gimp.open`
- `gimp.export`
- `gimp.draw_polyline`
- `gimp.batch`

Use `gimp.procedure.search` and `gimp.procedure.describe` before calling raw
PDB procedures. Object arguments can be passed as IDs, for example:

```json
{
  "name": "gimp-image-resize",
  "args": {
    "image": { "id": 1, "type": "image" },
    "new-width": 1920,
    "new-height": 1080,
    "offx": 0,
    "offy": 0
  }
}
```

## Current Scope

This is the first usable slice:

- Live GIMP bridge over a local Unix socket.
- PDB search/describe/call.
- MCP image capture for the current image or a cropped/scaled region.
- Context inspection for current colors, brush, opacity, feather, antialias,
  and font.
- GIMP environment/session diagnostics.
- Open image inspection.
- File open/export helpers.
- Batch bridge operations.
- MCP resources for status, images, and procedure index.

Some GIMP UI-only interactions are not directly scriptable. The route for those
is to find the equivalent PDB or GEGL operation and call that instead.

## Production Notes

- Normal operation uses a user-owned Unix socket with `0600` permissions.
- The MCP server can auto-start a headless GIMP bridge; no GIMP menu click is
  required for automation.
- The visible GIMP menu entry remains available only as a manual debug path.
- The bridge must be installed into the user's GIMP profile before auto-start
  can work.
- Do not expose the socket path to untrusted users. Anyone who can connect to
  the socket can drive the GIMP process.
