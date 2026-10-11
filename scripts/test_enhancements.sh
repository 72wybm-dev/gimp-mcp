#!/usr/bin/env bash
set -euo pipefail

# Regression tests for the GEGL filter tools and PDB value conversions.
# Builds a synthetic image in a private headless GIMP and checks pixel values,
# so it verifies effects actually happened rather than just that calls returned.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="${GIMP_MCP_SOCKET:-/tmp/gimp-mcp-test-${USER}.sock}"
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
failures = []


class ToolError(Exception):
    pass


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
        raise ToolError(reply["error"]["message"])
    return reply["result"]


def tool(name, arguments=None):
    result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
    if result.get("isError"):
        raise ToolError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def pdb(procedure, /, **args):
    result = tool("gimp.procedure.call", {"name": procedure, "args": args})
    if not result["success"]:
        raise ToolError(f"{procedure} failed: {result['last_error']}")
    return {value["name"]: value.get("value", value.get("error")) for value in result["return_values"]}


def check(label, condition, detail=""):
    print(f"{'ok' if condition else 'FAIL'} - {label}{f' ({detail})' if detail else ''}")
    if not condition:
        failures.append(label)


def expect_error(label, fragment, fn):
    try:
        fn()
    except ToolError as exc:
        check(label, fragment in str(exc), str(exc)[:120])
        return
    check(label, False, "call succeeded but should have been rejected")


def ref(kind, item_id):
    return {"id": item_id, "type": kind}


def new_layer(color):
    layer_id = pdb("gimp-layer-new", image=ref("image", image_id), name=color, width=16, height=16,
                   type=0, opacity=100.0, mode=28)["layer"]["id"]
    pdb("gimp-image-insert-layer", image=ref("image", image_id), layer=ref("layer", layer_id), parent=None, position=0)
    tool("gimp.fill_rect", {"image_id": image_id, "drawable_id": layer_id, "x": 0, "y": 0, "width": 16, "height": 16, "color": color})
    return layer_id


def pixel(layer_id):
    return pdb("gimp-drawable-get-pixel", drawable=ref("layer", layer_id), **{"x-coord": 8, "y-coord": 8})["color"]["rgba"]


def filter_names(layer_id):
    return [item["name"] for item in pdb("gimp-drawable-get-filters", drawable=ref("layer", layer_id))["filters"]]


rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "enhancement-tests", "version": "1"}})
tools = {item["name"] for item in rpc("tools/list")["tools"]}
check("filter tools registered", {"gimp.filter.list", "gimp.filter.describe", "gimp.filter.apply"} <= tools)

image_id = pdb("gimp-image-new", width=16, height=16, type=0)["image"]["id"]

# Return value serialization
grey = new_layer("#808080")
rgba = pixel(grey)
check("get-pixel returns RGBA floats", isinstance(rgba, list) and len(rgba) == 4 and 0 < rgba[0] < 1, str(rgba))

path_id = pdb("gimp-path-new", image=ref("image", image_id), name="p")["path"]["id"]
pdb("gimp-image-insert-path", image=ref("image", image_id), path=ref("path", path_id), parent=None, position=0)
stroke_id = pdb("gimp-path-bezier-stroke-new-moveto", path=ref("path", path_id), x0=1.0, y0=2.0)["stroke-id"]
strokes = pdb("gimp-path-get-strokes", path=ref("path", path_id))["stroke-ids"]
check("Int32Array return serializes to a list", strokes == [stroke_id], str(strokes))
points = pdb("gimp-path-stroke-get-points", path=ref("path", path_id), **{"stroke-id": stroke_id})["controlpoints"]
check("DoubleArray return serializes to a list", points[:2] == [1.0, 2.0], str(points))

# Double-array arguments: the curve must actually brighten mid-grey
pdb("gimp-drawable-curves-spline", drawable=ref("layer", grey), channel=0, points=[0.0, 0.0, 0.5, 0.8, 1.0, 1.0])
after = pixel(grey)
check("curves-spline accepts JSON array and brightens mid-grey", after[0] > rgba[0] + 0.1, f"{rgba[0]:.3f} -> {after[0]:.3f}")
expect_error("non-array for GimpDoubleArray is rejected", "expects an array", lambda: pdb(
    "gimp-drawable-curves-spline", drawable=ref("layer", grey), channel=0, points="0 0 1 1"))

# Filter discovery
check("filter.list finds unsharp-mask", "gegl:unsharp-mask" in tool("gimp.filter.list", {"query": "sharp"})["operations"])
blur = {item["name"]: item for item in tool("gimp.filter.describe", {"operation": "gegl:gaussian-blur"})["properties"]}
check("filter.describe reports choices", blur["filter"].get("choices") == ["auto", "fir", "iir"], str(blur["filter"].get("choices")))
check("filter.describe reports ranges", blur["std-dev-x"].get("maximum", 0) > 0, str(blur["std-dev-x"].get("maximum")))

# Filters apply with requested settings and change pixels
dark = new_layer("#202020")
before = pixel(dark)
tool("gimp.filter.apply", {"drawable_id": dark, "operation": "gegl:invert-gamma"})
check("merged filter changes pixels", pixel(dark)[0] > 0.6, f"{before[0]:.3f} -> {pixel(dark)[0]:.3f}")

result = tool("gimp.filter.apply", {"drawable_id": grey, "operation": "gegl:unsharp-mask",
                                     "properties": {"std-dev": 2, "scale": 1.2, "threshold": 0.02}})
check("custom numeric properties are applied", result["properties"] == {"std-dev": 2.0, "scale": 1.2, "threshold": 0.02}, str(result["properties"]))

result = tool("gimp.filter.apply", {"drawable_id": grey, "operation": "gegl:gaussian-blur", "properties": {"filter": "iir"}})
check("choice property is applied", result["properties"]["filter"] == "iir", result["properties"]["filter"])

result = tool("gimp.filter.apply", {"drawable_id": grey, "operation": "gegl:color-to-alpha", "properties": {"color": "#ff0000"},
                                     "merge": False, "name": "c2a"})
check("color property is applied", [round(c, 2) for c in result["properties"]["color"]["rgba"]][:3] == [1.0, 0.0, 0.0],
      str(result["properties"]["color"].get("rgba")))

# Opacity and blend mode
faint = new_layer("#202020")
tool("gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:invert-gamma", "opacity": 0.0, "blend_mode": "NORMAL"})
check("opacity 0 leaves pixels unchanged", abs(pixel(faint)[0] - before[0]) < 0.01, f"{before[0]:.3f} -> {pixel(faint)[0]:.3f}")
expect_error("unknown blend mode is rejected", "Unknown LayerMode", lambda: tool(
    "gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:invert-gamma", "blend_mode": "BOGUS"}))

# Non-destructive filters and rejection without leftovers
tool("gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:unsharp-mask", "merge": False, "name": "keep-me"})
expect_error("unknown property is rejected", "no property named", lambda: tool(
    "gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:unsharp-mask", "properties": {"bogus": 1}}))
expect_error("invalid choice is rejected", "choices", lambda: tool(
    "gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:gaussian-blur", "properties": {"filter": "huge"}}))
expect_error("out-of-range value is rejected", "outside", lambda: tool(
    "gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:unsharp-mask", "properties": {"std-dev": 99999}}))
expect_error("unknown operation is rejected", "Unknown GEGL operation", lambda: tool(
    "gimp.filter.apply", {"drawable_id": faint, "operation": "gegl:does-not-exist"}))
check("rejected filters leave no residue", filter_names(faint) == ["keep-me"], str(filter_names(faint)))

# Not covered: GimpInt32Array arguments and GimpColorArray return values. No GIMP 3.2
# PDB procedure uses them, so those branches cannot be exercised here.

import time


def running(pid):
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def wait_gone(pids, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and any(running(pid) for pid in pids):
        time.sleep(0.5)
    leftover = [pid for pid in pids if running(pid)]
    for pid in leftover:
        subprocess.run(["kill", str(pid)])
    return leftover


def gimp_pids():
    bridge = tool("gimp.status")["pid"]
    gimp = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(bridge)], capture_output=True, text=True).stdout)
    return bridge, gimp


# Closing stdin stops the server, which terminates GIMP; the bridge plug-in must exit
# too instead of lingering as an orphan that answers on the socket with no GIMP behind it.
bridge_pid, gimp_pid = gimp_pids()
proc.stdin.close()
proc.wait(timeout=30)
leftover = wait_gone([bridge_pid, gimp_pid])
check("GIMP and bridge exit when server stdin closes", not leftover, f"still running: {leftover}" if leftover else "")
check("bridge removes its socket on exit", not os.path.exists(os.environ["GIMP_MCP_SOCKET"]))

# Clients may instead stop the server with SIGTERM; auto-started GIMP must not survive that.
proc = subprocess.Popen([server], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        env={**os.environ, "GIMP_MCP_SOCKET": os.environ["GIMP_MCP_SOCKET"]})
rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "enhancement-tests", "version": "1"}})
bridge_pid, gimp_pid = gimp_pids()
proc.terminate()
proc.wait(timeout=30)
leftover = wait_gone([bridge_pid, gimp_pid])
check("GIMP and bridge exit when server gets SIGTERM", not leftover, f"still running: {leftover}" if leftover else "")

if failures:
    print(f"\n{len(failures)} failed: {failures}")
    sys.exit(1)
print("\nall enhancement tests passed")
PY
