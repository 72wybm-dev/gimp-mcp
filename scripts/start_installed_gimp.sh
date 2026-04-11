#!/usr/bin/env bash
set -euo pipefail

GIMP_APP="${GIMP_APP:-/Applications/GIMP.app}"
GIMP_MCP_SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-${USER}.sock}"

if [[ ! -d "$GIMP_APP" ]]; then
  echo "GIMP app not found: $GIMP_APP" >&2
  echo "Set GIMP_APP=/path/to/GIMP.app and rerun." >&2
  exit 1
fi

export GIMP_MCP_SOCKET
open -a "$GIMP_APP"

echo "Launched $GIMP_APP"
echo "Socket path for bridge/MCP server:"
echo "  $GIMP_MCP_SOCKET"
echo
echo "Manual/debug fallback:"
echo "  Filters > Development > Start MCP Bridge"
echo
echo "For normal MCP use without clicking GIMP menus:"
echo "  scripts/run_mcp_server.sh"
