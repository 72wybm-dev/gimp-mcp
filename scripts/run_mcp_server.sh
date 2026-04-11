#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIMP_APP="${GIMP_APP:-/Applications/GIMP.app}"
GIMP_MCP_SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-${USER}.sock}"

ARGS=(--socket "$GIMP_MCP_SOCKET" --gimp-app "$GIMP_APP" --auto-start-bridge)

if [[ -x "$ROOT/.venv/bin/gimp-mcp" ]]; then
  exec "$ROOT/.venv/bin/gimp-mcp" "${ARGS[@]}"
fi

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m gimp_mcp.server "${ARGS[@]}"
