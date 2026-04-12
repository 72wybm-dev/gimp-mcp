# GIMP MCP Server

Control GIMP from Claude Desktop, Codex, Cursor, and other Model Context
Protocol clients. This project provides a local MCP server and a GIMP 3.x
Python plug-in that expose GIMP automation through a secure Unix socket,
including Procedure Database discovery, image export, canvas snapshots, context
inspection, and batch operations.

Keywords: GIMP MCP, GIMP Model Context Protocol, Claude GIMP, Codex GIMP,
AI image editing, GIMP automation, GIMP Python plug-in, GIMP PDB, MCP server.

## Why This Exists

GIMP has a powerful Python and Procedure Database API, but it is not easy for AI
assistants to use directly. GIMP MCP bridges that gap:

- Ask an AI assistant to inspect or edit images in GIMP.
- Search and call GIMP PDB procedures from MCP tools.
- Capture the current canvas as MCP image content for visual verification.
- Run repeatable image workflows without clicking through the GIMP UI.
- Keep automation local by default with a user-owned Unix socket.

The normal MCP path auto-starts a private headless GIMP bridge, so users do not
need to open GIMP or click `Filters > Development`.

## Features

- **GIMP 3.x automation through MCP**
- **PDB search, describe, and call** for broad GIMP feature coverage
- **MCP image capture** with optional crop and max-size scaling
- **Open image and layer inspection**
- **First-class layer tools** for listing and updating layer properties
- **Friendly drawing tools** for rectangles, ellipses, text, and polylines
- **Foreground/background, brush, font, opacity, antialias, and feather state**
- **File open/export helpers** for PNG, JPEG, WebP, TIFF, GIF, BMP, and XCF
- **Batch bridge operations**
- **Headless auto-start** for production-style automation
- **Manual visible-GIMP bridge** for debugging interactive sessions
- **macOS-friendly installer** for `/Applications/GIMP.app`

## Requirements

- GIMP 3.x installed locally
- Python 3.11+
- An MCP-compatible client such as Codex, Claude Desktop, Cursor, or another
  stdio MCP client

This repo defaults to macOS and `/Applications/GIMP.app`. The bridge itself is
Python/GIMP based and can be adapted for Linux by installing the plug-in into
the relevant GIMP profile directory.

## Quick Start

```sh
git clone https://github.com/abelduarte/gimp-mcp.git
cd gimp-mcp

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

./scripts/install_bridge.sh
./scripts/smoke_test.sh
```

If GIMP is not installed at `/Applications/GIMP.app`:

```sh
GIMP_APP=/path/to/GIMP.app ./scripts/install_bridge.sh
GIMP_APP=/path/to/GIMP.app ./scripts/smoke_test.sh
```

## MCP Client Configuration

Use the wrapper script as your MCP server command:

```sh
/absolute/path/to/gimp-mcp/scripts/run_mcp_server.sh
```

The wrapper starts the stdio MCP server and automatically launches a private
headless GIMP bridge if one is not already running.

### Codex

Add this to your Codex MCP config:

```toml
[mcp_servers.gimp]
command = "/absolute/path/to/gimp-mcp/scripts/run_mcp_server.sh"
```

### Claude Desktop

Add this to your Claude Desktop config:

```json
{
  "mcpServers": {
    "gimp": {
      "command": "/absolute/path/to/gimp-mcp/scripts/run_mcp_server.sh"
    }
  }
}
```

### Project-local MCP discovery

The repository includes `.mcp.json` for clients that support project-local MCP
server discovery.

## How It Works

There are two pieces:

- `gimp_mcp.server`: the stdio MCP server used by the AI client.
- `bridge/gimp_mcp_bridge.py`: a GIMP Python plug-in that runs inside GIMP and
  exposes a JSON bridge over a local Unix socket.

Default socket:

```text
/tmp/gimp-mcp-$USER.sock
```

The socket is created with `0600` permissions. Anyone who can connect to it can
drive the GIMP process, so do not expose it to untrusted users.

## Available Tools

- `gimp.status`: check bridge health
- `gimp.get_info`: inspect GIMP version, directories, session, platform, and PDB
- `gimp.get_context_state`: inspect colors, brush, opacity, font, antialias, and feather
- `gimp.get_image_bitmap`: return a GIMP image as MCP image content
- `gimp.list_images`: list open images and layer trees
- `gimp.get_image`: inspect one image by ID
- `gimp.list_layers`: list layers for one image
- `gimp.set_layer_properties`: rename layers or update visibility, opacity, and blend mode
- `gimp.procedure.search`: search GIMP's Procedure Database
- `gimp.procedure.describe`: inspect procedure arguments and return values
- `gimp.procedure.call`: call a GIMP PDB procedure
- `gimp.open`: open an image file
- `gimp.export`: export an image file
- `gimp.fill_rect`: fill a rectangle on a drawable
- `gimp.fill_ellipse`: fill an ellipse on a drawable
- `gimp.add_text`: create a text layer
- `gimp.draw_polyline`: draw deterministic pixel polylines
- `gimp.batch`: run bridge operations in sequence

## Example PDB Call

Search before calling:

```json
{
  "query": "image-resize",
  "limit": 10
}
```

Then call the procedure with exact PDB argument names:

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

## Visual Verification

Use `gimp.get_image_bitmap` to let the AI client see the current GIMP image:

```json
{
  "image_id": 1,
  "max_width": 1024,
  "max_height": 1024
}
```

Crop a region for faster iteration:

```json
{
  "image_id": 1,
  "region": {
    "origin_x": 100,
    "origin_y": 100,
    "width": 400,
    "height": 300
  },
  "max_width": 800,
  "max_height": 600
}
```

## Manual Visible-GIMP Mode

For normal automation, use `scripts/run_mcp_server.sh`. It auto-starts a
headless bridge.

For debugging against the visible GIMP app:

1. Open GIMP.
2. Run `Filters > Development > Start MCP Bridge`.
3. Start your MCP client with the same `GIMP_MCP_SOCKET`.

This manual menu is a debug fallback, not the recommended production path.

## Development

Run the smoke test:

```sh
./scripts/smoke_test.sh
```

Run full local diagnostics:

```sh
./scripts/doctor.sh
```

Compile-check the Python files:

```sh
python3 -m py_compile src/gimp_mcp/server.py bridge/gimp_mcp_bridge.py
```

Install the bridge after editing it:

```sh
./scripts/install_bridge.sh
```

## Examples

Generated examples are in `examples/generated/`, including:

- `horse-from-gimp-mcp.png`
- `bar-chart-from-gimp-mcp.png`
- `shapes-text-demo.png`

## Security

GIMP MCP is a local automation bridge. Treat access to the Unix socket as access
to the running GIMP process.

- Do not expose the socket to untrusted users.
- Do not run the bridge as root.
- Prefer the default Unix socket transport over TCP.
- Review `SECURITY.md` before enabling any future raw Python execution feature.

## License

MIT. See `LICENSE`.
