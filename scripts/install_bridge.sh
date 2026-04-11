#!/usr/bin/env bash
set -euo pipefail

GIMP_APP="${GIMP_APP:-/Applications/GIMP.app}"
BRIDGE_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bridge/gimp_mcp_bridge.py"

if [[ ! -d "$GIMP_APP" ]]; then
  echo "GIMP app not found: $GIMP_APP" >&2
  echo "Set GIMP_APP=/path/to/GIMP.app and rerun." >&2
  exit 1
fi

if [[ -z "${GIMP_PLUGIN_DIR:-}" ]]; then
  GIMP_PROFILE_VERSION="$(find "$HOME/Library/Application Support/GIMP" -maxdepth 1 -type d -name '3.*' -print 2>/dev/null | sed 's#.*/##' | sort -Vr | head -n 1)"
  GIMP_PROFILE_VERSION="${GIMP_PROFILE_VERSION:-3.0}"
  PLUGIN_ROOT="$HOME/Library/Application Support/GIMP/$GIMP_PROFILE_VERSION/plug-ins"
else
  PLUGIN_ROOT="$GIMP_PLUGIN_DIR"
fi
PLUGIN_DIR="$PLUGIN_ROOT/gimp_mcp_bridge"
PLUGIN_TARGET="$PLUGIN_DIR/gimp_mcp_bridge.py"

mkdir -p "$PLUGIN_DIR"
cp "$BRIDGE_SOURCE" "$PLUGIN_TARGET"
chmod +x "$PLUGIN_TARGET"

echo "Installed GIMP MCP bridge plug-in:"
echo "  $PLUGIN_TARGET"
echo
echo "For normal MCP use, run the MCP server with auto-start:"
echo "  scripts/run_mcp_server.sh"
echo
echo "Manual/debug fallback:"
echo "  Restart GIMP, then run Filters > Development > Start MCP Bridge"
