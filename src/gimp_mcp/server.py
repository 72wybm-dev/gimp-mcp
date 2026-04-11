from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__

DEFAULT_SOCKET = f"/tmp/gimp-mcp-{os.environ.get('USER', 'user')}.sock"
PROTOCOL_VERSION = "2025-06-18"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


class GimpBridgeClient:
    def __init__(self, socket_path: str, timeout: float = 30.0, auto_start: bool = False, gimp_app: str = "/Applications/GIMP.app") -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.auto_start = auto_start
        self.gimp_app = gimp_app
        self.bridge_process: subprocess.Popen[str] | None = None
        if self.auto_start:
            atexit.register(self.stop_bridge)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        message = {"method": method, "params": params or {}}
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            raw = self._send(payload)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            if not self.auto_start:
                if isinstance(exc, FileNotFoundError):
                    raise JsonRpcError(
                        -32001,
                        "GIMP MCP bridge is not running",
                        {"socket": self.socket_path, "hint": "Start GIMP and run Filters > Development > Start MCP Bridge, or run the MCP server with --auto-start-bridge."},
                    ) from exc
                raise JsonRpcError(-32002, f"Could not reach GIMP MCP bridge: {exc}", {"socket": self.socket_path}) from exc
            self.start_bridge()
            try:
                raw = self._send(payload)
            except FileNotFoundError as retry_exc:
                raise JsonRpcError(
                    -32001,
                    "GIMP MCP bridge did not start",
                    {"socket": self.socket_path, "hint": "Run scripts/install_bridge.sh, then retry."},
                ) from retry_exc
            except OSError as retry_exc:
                raise JsonRpcError(-32002, f"Could not reach GIMP MCP bridge after auto-start: {retry_exc}", {"socket": self.socket_path}) from retry_exc

        try:
            response = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise JsonRpcError(-32003, "GIMP MCP bridge returned invalid JSON", raw.decode("utf-8", "replace")) from exc

        if "error" in response:
            error = response["error"]
            raise JsonRpcError(error.get("code", -32000), error.get("message", "GIMP bridge error"), error.get("data"))
        return response.get("result")

    def _send(self, payload: bytes) -> bytes:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(payload)
                return _read_line(sock)
        except FileNotFoundError:
            raise
        except OSError:
            raise

    def start_bridge(self) -> None:
        if self.bridge_process and self.bridge_process.poll() is None:
            return
        socket_file = Path(self.socket_path)
        if socket_file.exists():
            socket_file.unlink()

        gimp_binary = gimp_binary_path(self.gimp_app)
        env = os.environ.copy()
        env["GIMP_MCP_SOCKET"] = self.socket_path
        code = (
            "proc = Gimp.get_pdb().lookup_procedure('python-fu-gimp-mcp-bridge-start'); "
            "config = proc.create_config(); "
            "config.set_property('run-mode', Gimp.RunMode.NONINTERACTIVE); "
            "proc.run(config)"
        )
        self.bridge_process = subprocess.Popen(
            [gimp_binary, "-i", "--no-splash", "--batch-interpreter=python-fu-eval", "-b", code],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                return
            if self.bridge_process.poll() is not None:
                break
            time.sleep(0.25)
        raise JsonRpcError(-32004, "Timed out waiting for GIMP MCP bridge to start", {"socket": self.socket_path, "gimp_app": self.gimp_app})

    def stop_bridge(self) -> None:
        if self.bridge_process and self.bridge_process.poll() is None:
            self.bridge_process.terminate()
            try:
                self.bridge_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bridge_process.kill()


class GimpMcpServer:
    def __init__(self, bridge: GimpBridgeClient) -> None:
        self.bridge = bridge
        self.tools = self._build_tools()

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if isinstance(request, list):
                    responses = [self._handle_message(item) for item in request]
                    responses = [item for item in responses if item is not None]
                    if responses:
                        _write_json(responses)
                else:
                    response = self._handle_message(request)
                    if response is not None:
                        _write_json(response)
            except json.JSONDecodeError as exc:
                _write_json(_error_response(None, -32700, "Parse error", str(exc)))
            except Exception as exc:  # Keep stdio alive on unexpected failures.
                _write_json(_error_response(None, -32603, "Internal error", str(exc)))

    def _handle_message(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("jsonrpc") != "2.0":
            return _error_response(request.get("id"), -32600, "Invalid Request", "Expected JSON-RPC 2.0")

        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if request_id is None:
            self._handle_notification(method, params)
            return None

        try:
            result = self._dispatch(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            return _error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            return _error_response(request_id, -32603, "Internal error", str(exc))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in {"notifications/initialized", "$/cancelRequest"}:
            return
        print(f"ignored MCP notification: {method} {params}", file=sys.stderr)

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "gimp-mcp", "version": __version__},
            }
        if method == "tools/list":
            return {"tools": [self._tool_metadata(tool) for tool in self.tools.values()]}
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if tool_name not in self.tools:
                raise JsonRpcError(-32602, f"Unknown tool: {tool_name}")
            result = self.tools[tool_name].handler(arguments)
            return _tool_result(result)
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": "gimp://status",
                        "name": "GIMP status",
                        "description": "Current bridge and GIMP process status.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "gimp://images",
                        "name": "Open images",
                        "description": "Currently open GIMP images and their layer trees.",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "gimp://procedures",
                        "name": "PDB procedure index",
                        "description": "Searchable list of GIMP PDB procedures.",
                        "mimeType": "application/json",
                    },
                ]
            }
        if method == "resources/read":
            uri = params.get("uri")
            if uri == "gimp://status":
                data = self.bridge.request("status")
            elif uri == "gimp://images":
                data = self.bridge.request("list_images")
            elif uri == "gimp://procedures":
                data = self.bridge.request("procedure_search", {"query": "", "limit": 500})
            elif isinstance(uri, str) and uri.startswith("gimp://procedure/"):
                name = uri.removeprefix("gimp://procedure/")
                data = self.bridge.request("procedure_describe", {"name": name})
            elif isinstance(uri, str) and uri.startswith("gimp://image/"):
                image_id = int(uri.removeprefix("gimp://image/"))
                data = self.bridge.request("get_image", {"image_id": image_id})
            else:
                raise JsonRpcError(-32602, f"Unknown resource URI: {uri}")
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": _json_text(data)}]}
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": "resize-and-export",
                        "description": "Resize an open image and export it through GIMP's PDB.",
                        "arguments": [
                            {"name": "image_id", "description": "GIMP image ID", "required": True},
                            {"name": "width", "description": "Target width", "required": True},
                            {"name": "height", "description": "Target height", "required": True},
                            {"name": "output_path", "description": "Absolute export path", "required": True},
                        ],
                    },
                    {
                        "name": "batch-convert-folder",
                        "description": "Use GIMP file load/export PDB procedures to convert a folder of images.",
                    },
                ]
            }
        if method == "prompts/get":
            return self._get_prompt(params)

        raise JsonRpcError(-32601, f"Method not found: {method}")

    def _get_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "resize-and-export":
            return {
                "description": "Resize and export a GIMP image.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                "Use GIMP MCP tools to inspect image "
                                f"{args.get('image_id')}, call the appropriate PDB resize procedure to make it "
                                f"{args.get('width')}x{args.get('height')}, then export to {args.get('output_path')}."
                            ),
                        },
                    }
                ],
            }
        if name == "batch-convert-folder":
            return {
                "description": "Batch convert images with GIMP.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": "Search GIMP's PDB for file load/export procedures, then use gimp.batch to convert the requested folder.",
                        },
                    }
                ],
            }
        raise JsonRpcError(-32602, f"Unknown prompt: {name}")

    def _build_tools(self) -> dict[str, Tool]:
        return {
            "gimp.status": Tool(
                "gimp.status",
                "Check whether the GIMP bridge is alive and which GIMP/Python process is serving requests.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _args: self.bridge.request("status"),
            ),
            "gimp.get_info": Tool(
                "gimp.get_info",
                "Get GIMP version, directories, session details, platform information, and PDB health checks.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _args: self.bridge.request("get_info"),
            ),
            "gimp.get_context_state": Tool(
                "gimp.get_context_state",
                "Get current GIMP context state such as foreground/background colors, brush, opacity, feather, antialias, and font.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _args: self.bridge.request("get_context_state"),
            ),
            "gimp.get_image_bitmap": Tool(
                "gimp.get_image_bitmap",
                "Return a GIMP image as MCP image content with optional max-size scaling and region extraction.",
                {
                    "type": "object",
                    "properties": {
                        "image_id": {"type": "integer", "description": "Optional image ID. Defaults to the first open image."},
                        "max_width": {"type": "integer", "minimum": 1, "description": "Optional maximum output width."},
                        "max_height": {"type": "integer", "minimum": 1, "description": "Optional maximum output height."},
                        "region": {
                            "type": "object",
                            "description": "Optional crop region before scaling.",
                            "required": ["width", "height"],
                            "properties": {
                                "origin_x": {"type": "integer", "minimum": 0},
                                "origin_y": {"type": "integer", "minimum": 0},
                                "x": {"type": "integer", "minimum": 0},
                                "y": {"type": "integer", "minimum": 0},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("get_image_bitmap", args),
            ),
            "gimp.list_images": Tool(
                "gimp.list_images",
                "List open images, dimensions, layers, channels, and selected drawables.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _args: self.bridge.request("list_images"),
            ),
            "gimp.get_image": Tool(
                "gimp.get_image",
                "Inspect one open image by GIMP image ID.",
                {
                    "type": "object",
                    "required": ["image_id"],
                    "properties": {"image_id": {"type": "integer"}},
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("get_image", args),
            ),
            "gimp.procedure.search": Tool(
                "gimp.procedure.search",
                "Search GIMP's Procedure Database by name/help text. This is the discovery entry point for most GIMP features.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Substring or regex fragment to match."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 50},
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("procedure_search", args),
            ),
            "gimp.procedure.describe": Tool(
                "gimp.procedure.describe",
                "Describe a PDB procedure's arguments and return values.",
                {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("procedure_describe", args),
            ),
            "gimp.procedure.call": Tool(
                "gimp.procedure.call",
                "Call a GIMP PDB procedure. Use describe first; pass object arguments as {\"id\": 123} or typed refs.",
                {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "args": {"type": "object", "description": "Property names to values for the procedure config."},
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("procedure_call", args),
            ),
            "gimp.open": Tool(
                "gimp.open",
                "Open an image file in GIMP through the file load PDB procedure.",
                {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string", "description": "Absolute path to open."}},
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("open_file", args),
            ),
            "gimp.export": Tool(
                "gimp.export",
                "Export an image to a file. The bridge chooses a file-* export procedure from the extension unless procedure is supplied.",
                {
                    "type": "object",
                    "required": ["image_id", "path"],
                    "properties": {
                        "image_id": {"type": "integer"},
                        "path": {"type": "string", "description": "Absolute export path."},
                        "procedure": {"type": "string", "description": "Optional explicit PDB export procedure."},
                        "options": {"type": "object", "description": "Additional procedure-specific properties."},
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("export_file", args),
            ),
            "gimp.draw_polyline": Tool(
                "gimp.draw_polyline",
                "Draw a polyline on a drawable using GIMP's active pencil/brush engine.",
                {
                    "type": "object",
                    "required": ["drawable_id", "points"],
                    "properties": {
                        "drawable_id": {"type": "integer"},
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 2,
                                "items": {"type": "number"},
                            },
                        },
                        "color": {
                            "description": "CSS color string, [r,g,b], [r,g,b,a], or {r,g,b,a}.",
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 4},
                                {"type": "object"},
                            ],
                        },
                        "brush_size": {"type": "number", "default": 8},
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("draw_polyline", args),
            ),
            "gimp.batch": Tool(
                "gimp.batch",
                "Run a list of bridge operations in order. Stops at the first failing operation.",
                {
                    "type": "object",
                    "required": ["operations"],
                    "properties": {
                        "operations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["method"],
                                "properties": {"method": {"type": "string"}, "params": {"type": "object"}},
                            },
                        }
                    },
                    "additionalProperties": False,
                },
                lambda args: self.bridge.request("batch", args),
            ),
        }

    @staticmethod
    def _tool_metadata(tool: Tool) -> dict[str, Any]:
        return {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}


def _read_line(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            break
        chunks.append(chunk)
    return b"".join(chunks)


def gimp_binary_path(gimp_app: str) -> str:
    path = Path(gimp_app).expanduser()
    if path.suffix == ".app":
        binary = path / "Contents" / "MacOS" / "gimp"
    else:
        binary = path
    if not binary.exists():
        raise JsonRpcError(-32602, f"GIMP binary not found: {binary}", {"gimp_app": str(path)})
    return str(binary)


def _tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and result.get("_mcp_image"):
        metadata = {key: value for key, value in result.items() if key not in {"_mcp_image", "data", "mime_type"}}
        return {
            "content": [
                {"type": "text", "text": _json_text(metadata)},
                {"type": "image", "data": result["data"], "mimeType": result.get("mime_type", "image/png")},
            ]
        }
    return {"content": [{"type": "text", "text": _json_text(result)}]}


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _write_json(message: Any) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GIMP MCP stdio server")
    parser.add_argument("--socket", default=os.environ.get("GIMP_MCP_SOCKET", DEFAULT_SOCKET), help="Unix socket exposed by the GIMP bridge.")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("GIMP_MCP_TIMEOUT", "30")), help="Bridge request timeout in seconds.")
    parser.add_argument("--auto-start-bridge", action="store_true", default=os.environ.get("GIMP_MCP_AUTO_START", "").lower() in {"1", "true", "yes"}, help="Start a private headless GIMP bridge automatically if the socket is not running.")
    parser.add_argument("--gimp-app", default=os.environ.get("GIMP_APP", "/Applications/GIMP.app"), help="Path to GIMP.app or a GIMP executable for --auto-start-bridge.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    server = GimpMcpServer(GimpBridgeClient(args.socket, args.timeout, args.auto_start_bridge, args.gimp_app))
    server.run()


if __name__ == "__main__":
    main()
