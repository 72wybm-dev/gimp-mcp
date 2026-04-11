#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-smoke-${USER}.sock}"
SERVER="$ROOT/scripts/run_mcp_server.sh"

rm -f "$SOCKET"
GIMP_MCP_SOCKET="$SOCKET" python3 - "$SERVER" <<'PY'
import json
import os
import subprocess
import sys

server = sys.argv[1]
proc = subprocess.Popen(
    [server],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env={**os.environ, "GIMP_MCP_SOCKET": os.environ["GIMP_MCP_SOCKET"]},
)
next_id = 1

def rpc(method, params=None):
    global next_id
    payload = {"jsonrpc": "2.0", "id": next_id, "method": method}
    next_id += 1
    if params is not None:
        payload["params"] = params
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(proc.stderr.read())
    reply = json.loads(line)
    if "error" in reply:
        raise RuntimeError(json.dumps(reply["error"], indent=2))
    return reply["result"]

def tool(name, arguments=None):
    result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
    if result.get("isError"):
        raise RuntimeError(result)
    return result

def tool_json(name, arguments=None):
    return json.loads(tool(name, arguments)["content"][0]["text"])

rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1"}})
tools = rpc("tools/list")["tools"]
required = {"gimp.status", "gimp.get_info", "gimp.get_context_state", "gimp.get_image_bitmap", "gimp.procedure.search"}
available = {item["name"] for item in tools}
missing = sorted(required - available)
if missing:
    raise RuntimeError(f"Missing tools: {missing}")

status = tool_json("gimp.status")
info = tool_json("gimp.get_info")
context = tool_json("gimp.get_context_state")
search = tool_json("gimp.procedure.search", {"query": "image-new", "limit": 5})

print(json.dumps({
    "status_ok": status["ok"],
    "info_ok": info["ok"],
    "context_keys": sorted(context.keys())[:6],
    "procedure_hits": search["count"],
}, indent=2))
proc.terminate()
PY
