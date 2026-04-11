#!/usr/bin/env bash
set -euo pipefail

GIMP_APP="${GIMP_APP:-/Applications/GIMP.app}"
GIMP_MCP_SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-${USER}.sock}"
GIMP_BIN="$GIMP_APP/Contents/MacOS/gimp"

if [[ ! -x "$GIMP_BIN" ]]; then
  echo "GIMP binary not found: $GIMP_BIN" >&2
  echo "Set GIMP_APP=/path/to/GIMP.app and rerun." >&2
  exit 1
fi

rm -f "$GIMP_MCP_SOCKET"
export GIMP_MCP_SOCKET

exec "$GIMP_BIN" -i --no-splash --batch-interpreter=python-fu-eval -b "proc = Gimp.get_pdb().lookup_procedure('python-fu-gimp-mcp-bridge-start'); config = proc.create_config(); config.set_property('run-mode', Gimp.RunMode.NONINTERACTIVE); proc.run(config)"
