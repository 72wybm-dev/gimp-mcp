#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIMP_APP="${GIMP_APP:-/Applications/GIMP.app}"
GIMP_MCP_SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-doctor-${USER}.sock}"
PLUGIN_PATH="$HOME/Library/Application Support/GIMP"

pass() { printf 'ok - %s\n' "$1"; }
fail() { printf 'error - %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null || fail "python3 is not installed"
pass "python3 found: $(python3 --version)"

[[ -d "$GIMP_APP" ]] || fail "GIMP app not found at $GIMP_APP"
[[ -x "$GIMP_APP/Contents/MacOS/gimp" ]] || fail "GIMP executable not found in $GIMP_APP"
pass "GIMP app found: $GIMP_APP"

python3 -m py_compile "$ROOT/src/gimp_mcp/server.py" "$ROOT/bridge/gimp_mcp_bridge.py"
pass "Python files compile"

installed_bridge="$(find "$PLUGIN_PATH" -path '*/plug-ins/gimp_mcp_bridge/gimp_mcp_bridge.py' -print 2>/dev/null | sort -Vr | head -n 1 || true)"
[[ -n "$installed_bridge" ]] || fail "bridge plug-in is not installed; run ./scripts/install_bridge.sh"
pass "bridge plug-in installed: $installed_bridge"

rm -f "$GIMP_MCP_SOCKET"
GIMP_MCP_SOCKET="$GIMP_MCP_SOCKET" "$ROOT/scripts/run_mcp_server.sh" <<'JSON' >/tmp/gimp-mcp-doctor-out.jsonl
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doctor","version":"1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"gimp.status","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"gimp.get_info","arguments":{}}}
JSON

python3 - <<'PY'
import json
from pathlib import Path

lines = [json.loads(line) for line in Path("/tmp/gimp-mcp-doctor-out.jsonl").read_text().splitlines() if line.strip()]
if len(lines) != 4:
    raise SystemExit(f"expected 4 MCP responses, got {len(lines)}")
for line in lines:
    if "error" in line:
        raise SystemExit(line["error"])
tools = {tool["name"] for tool in lines[1]["result"]["tools"]}
required = {"gimp.status", "gimp.get_info", "gimp.get_image_bitmap", "gimp.list_layers", "gimp.fill_rect", "gimp.fill_ellipse", "gimp.add_text"}
missing = sorted(required - tools)
if missing:
    raise SystemExit(f"missing tools: {missing}")
print("ok - MCP server auto-started GIMP bridge")
print("ok - required tools are registered")
PY

rm -f "$GIMP_MCP_SOCKET" /tmp/gimp-mcp-doctor-out.jsonl
pass "doctor checks complete"
