#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import base64
import platform
import re
import socket
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gio, GLib, GObject
from gi.repository import Gegl

PROC_NAME = "python-fu-gimp-mcp-bridge-start"
DEFAULT_SOCKET = f"/tmp/gimp-mcp-{os.environ.get('USER', 'user')}.sock"

EXPORT_BY_EXTENSION = {
    ".png": "file-png-export",
    ".jpg": "file-jpeg-export",
    ".jpeg": "file-jpeg-export",
    ".webp": "file-webp-export",
    ".tif": "file-tiff-export",
    ".tiff": "file-tiff-export",
    ".gif": "file-gif-export",
    ".bmp": "file-bmp-export",
    ".xcf": "gimp-xcf-save",
}


def start_bridge(procedure, config, data):
    socket_path = os.environ.get("GIMP_MCP_SOCKET", DEFAULT_SOCKET)
    try:
        BridgeServer(socket_path).serve_forever()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except KeyboardInterrupt:
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except Exception as exc:
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))


class GimpMcpBridgePlugin(Gimp.PlugIn):
    def do_set_i18n(self, procname):
        return False

    def do_query_procedures(self):
        return [PROC_NAME]

    def do_create_procedure(self, name):
        procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, start_bridge, None)
        procedure.set_menu_label("Start MCP Bridge")
        procedure.set_documentation(
            "Start a local MCP bridge for GIMP",
            "Starts a Unix-socket JSON bridge that lets a local MCP server inspect images and call GIMP PDB procedures.",
            name,
        )
        procedure.set_attribution("Local GIMP MCP", "Local GIMP MCP", "2026")
        procedure.add_enum_argument(
            "run-mode",
            "Run mode",
            "The run mode",
            Gimp.RunMode,
            Gimp.RunMode.NONINTERACTIVE,
            GObject.ParamFlags.READWRITE,
        )
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.ALWAYS)
        procedure.add_menu_path("<Image>/Filters/Development")
        return procedure


class BridgeServer:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.handlers = {
            "status": self.status,
            "get_info": self.get_info,
            "get_context_state": self.get_context_state,
            "get_image_bitmap": self.get_image_bitmap,
            "list_images": self.list_images,
            "get_image": self.get_image,
            "list_layers": self.list_layers,
            "set_layer_properties": self.set_layer_properties,
            "procedure_search": self.procedure_search,
            "procedure_describe": self.procedure_describe,
            "procedure_call": self.procedure_call,
            "filter_list": self.filter_list,
            "filter_describe": self.filter_describe,
            "filter_apply": self.filter_apply,
            "open_file": self.open_file,
            "export_file": self.export_file,
            "fill_rect": self.fill_rect,
            "fill_ellipse": self.fill_ellipse,
            "add_text": self.add_text,
            "draw_polyline": self.draw_polyline,
            "batch": self.batch,
        }

    def serve_forever(self) -> None:
        existing = Path(self.socket_path)
        if existing.exists():
            existing.unlink()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)
            server.listen(8)
            print(f"GIMP MCP bridge listening on {self.socket_path}", file=sys.stderr, flush=True)
            while True:
                conn, _ = server.accept()
                thread = threading.Thread(target=self._serve_connection, args=(conn,), daemon=True)
                thread.start()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            raw = self._read_line(conn)
            if not raw:
                return
            try:
                request = json.loads(raw.decode("utf-8"))
                method = request.get("method")
                params = request.get("params") or {}
                if method not in self.handlers:
                    raise BridgeError(-32601, f"Unknown bridge method: {method}")
                result = self.handlers[method](params)
                self._write(conn, {"result": result})
            except BridgeError as exc:
                self._write(conn, {"error": {"code": exc.code, "message": exc.message, "data": exc.data}})
            except Exception as exc:
                self._write(
                    conn,
                    {
                        "error": {
                            "code": -32000,
                            "message": str(exc),
                            "data": traceback.format_exc(),
                        }
                    },
                )

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(1)
            if not chunk:
                break
            if chunk == b"\n":
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write(conn: socket.socket, message: dict[str, Any]) -> None:
        conn.sendall(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    def status(self, _params: dict[str, Any]) -> dict[str, Any]:
        pdb = Gimp.get_pdb()
        return {
            "ok": True,
            "socket": self.socket_path,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "procedure_count_sample": len(pdb.query_procedures(".*", ".*", ".*", ".*", ".*", ".*", ".*", ".*")),
        }

    def get_info(self, _params: dict[str, Any]) -> dict[str, Any]:
        pdb = Gimp.get_pdb()
        directories = {}
        for label, method in {
            "user_directory": "directory",
            "data_directory": "data_directory",
            "locale_directory": "locale_directory",
            "plug_in_directory": "plug_in_directory",
            "sysconf_directory": "sysconf_directory",
        }.items():
            if hasattr(Gimp, method):
                directories[label] = serialize_value(safe_call(Gimp, method))

        samples = []
        for name in ("gimp-image-new", "gimp-file-load", "file-png-export", "gimp-text-font", PROC_NAME):
            try:
                proc = pdb.lookup_procedure(name)
                samples.append({"name": name, "available": proc is not None, "type": enum_name(proc.get_proc_type()) if proc else None})
            except Exception as exc:
                samples.append({"name": name, "available": False, "error": str(exc)})

        return {
            "ok": True,
            "socket": self.socket_path,
            "pid": os.getpid(),
            "python": sys.version,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "platform": platform.platform(),
            },
            "gimp": {
                "version": serialize_value(safe_call(Gimp, "version")),
                "module_attributes": len(dir(Gimp)),
            },
            "directories": directories,
            "session": {
                "open_image_count": len(list_open_images()),
                "images": [describe_image(image) for image in list_open_images()],
            },
            "pdb": {
                "available": pdb is not None,
                "procedure_count_sample": len(pdb.query_procedures(".*", ".*", ".*", ".*", ".*", ".*", ".*", ".*")),
                "sample_procedures": samples,
            },
        }

    def get_context_state(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "foreground": serialize_color(safe_call(Gimp, "context_get_foreground")),
            "background": serialize_color(safe_call(Gimp, "context_get_background")),
            "brush": serialize_resource(safe_call(Gimp, "context_get_brush")),
            "brush_size": safe_call(Gimp, "context_get_brush_size"),
            "brush_hardness": safe_call(Gimp, "context_get_brush_hardness"),
            "brush_force": safe_call(Gimp, "context_get_brush_force"),
            "opacity": safe_call(Gimp, "context_get_opacity"),
            "paint_mode": serialize_value(safe_call(Gimp, "context_get_paint_mode")),
            "antialias": safe_call(Gimp, "context_get_antialias"),
            "feather": safe_call(Gimp, "context_get_feather"),
            "feather_radius": safe_call(Gimp, "context_get_feather_radius"),
            "font": serialize_resource(safe_call(Gimp, "context_get_font")),
        }

    def get_image_bitmap(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(int(params["image_id"])) if "image_id" in params else current_image()
        original_width = image.get_width()
        original_height = image.get_height()
        working_image = image
        should_delete = False

        try:
            region = params.get("region")
            max_width = params.get("max_width")
            max_height = params.get("max_height")
            if region is not None or max_width is not None or max_height is not None:
                working_image = image.duplicate()
                should_delete = True

            if region is not None:
                if not isinstance(region, dict):
                    raise BridgeError(-32602, "region must be an object")
                origin_x = int(region.get("origin_x", region.get("x", 0)))
                origin_y = int(region.get("origin_y", region.get("y", 0)))
                width = int(region["width"])
                height = int(region["height"])
                if origin_x < 0 or origin_y < 0 or width <= 0 or height <= 0:
                    raise BridgeError(-32602, "region coordinates and size must be positive")
                if origin_x + width > original_width or origin_y + height > original_height:
                    raise BridgeError(-32602, f"region exceeds image bounds {original_width}x{original_height}")
                working_image.crop(width, height, origin_x, origin_y)

            target_width, target_height = scaled_dimensions(
                working_image.get_width(),
                working_image.get_height(),
                int(max_width) if max_width is not None else None,
                int(max_height) if max_height is not None else None,
            )
            if (target_width, target_height) != (working_image.get_width(), working_image.get_height()):
                working_image.scale(target_width, target_height)

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_path = temp_file.name
            try:
                export_image_to_path(working_image, temp_path, "file-png-export", {})
                data = Path(temp_path).read_bytes()
            finally:
                Path(temp_path).unlink(missing_ok=True)

            return {
                "_mcp_image": True,
                "mime_type": "image/png",
                "data": base64.b64encode(data).decode("ascii"),
                "width": working_image.get_width(),
                "height": working_image.get_height(),
                "original_width": original_width,
                "original_height": original_height,
                "region": region,
                "scaled": (working_image.get_width(), working_image.get_height()) != (original_width, original_height),
            }
        finally:
            if should_delete:
                try:
                    working_image.delete()
                except Exception:
                    pass

    def list_images(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"images": [describe_image(image) for image in list_open_images()]}

    def get_image(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        return describe_image(image)

    def list_layers(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        return {"image_id": image.get_id(), "layers": [describe_item(layer) for layer in safe_list_items(image, "list_layers", "get_layers")]}

    def set_layer_properties(self, params: dict[str, Any]) -> dict[str, Any]:
        layer = layer_from_id(_require_int(params, "layer_id"))
        if "name" in params:
            layer.set_name(str(params["name"]))
        if "visible" in params:
            layer.set_visible(bool(params["visible"]))
        if "opacity" in params:
            layer.set_opacity(float(params["opacity"]))
        if "mode" in params:
            mode = enum_member(Gimp.LayerMode, str(params["mode"]))
            layer.set_mode(mode)
        return describe_item(layer)

    def procedure_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", ""))
        limit = int(params.get("limit", 50))
        regex = ".*" if not query else f".*{re.escape(query)}.*"
        pdb = Gimp.get_pdb()

        names = set()
        for field_args in (
            (regex, ".*", ".*"),
            (".*", regex, ".*"),
            (".*", ".*", regex),
        ):
            names.update(pdb.query_procedures(field_args[0], field_args[1], field_args[2], ".*", ".*", ".*", ".*", ".*"))

        sorted_names = sorted(names)
        items = []
        for name in sorted_names[:limit]:
            proc = pdb.lookup_procedure(name)
            items.append(summarize_procedure(proc) if proc is not None else {"name": name})
        return {"query": query, "count": len(sorted_names), "limit": limit, "procedures": items}

    def procedure_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        proc = lookup_procedure(_require_str(params, "name"))
        return describe_procedure(proc)

    def procedure_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _require_str(params, "name")
        args = params.get("args") or {}
        if not isinstance(args, dict):
            raise BridgeError(-32602, "args must be an object")
        proc = lookup_procedure(name)
        config = proc.create_config()
        apply_config_args(proc, config, args)
        result = proc.run(config)
        return serialize_result(proc, result)

    def filter_list(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").lower()
        operations = sorted(Gimp.DrawableFilter.operation_get_available())
        matches = [name for name in operations if query in name.lower()]
        return {"query": query, "count": len(matches), "operations": matches}

    def filter_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = _require_str(params, "operation")
        if operation not in Gimp.DrawableFilter.operation_get_available():
            raise BridgeError(-32602, f"Unknown GEGL operation: {operation}")
        specs = Gimp.DrawableFilter.operation_get_pspecs(operation)
        properties = [describe_param_spec(specs.index(i)) for i in range(specs.length())]
        return {"operation": operation, "properties": properties}

    def filter_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        drawable = drawable_from_id(_require_int(params, "drawable_id"))
        operation = _require_str(params, "operation")
        if operation not in Gimp.DrawableFilter.operation_get_available():
            raise BridgeError(-32602, f"Unknown GEGL operation: {operation}")
        properties = params.get("properties") or {}
        if not isinstance(properties, dict):
            raise BridgeError(-32602, "properties must be an object")

        drawable_filter = Gimp.DrawableFilter.new(drawable, operation, str(params.get("name") or operation))
        config = drawable_filter.get_config()
        specs = {spec.name: spec for spec in config.list_properties()}
        try:
            for key, value in properties.items():
                if key not in specs:
                    raise BridgeError(-32602, f"{operation} has no property named {key!r}", {"available": sorted(specs)})
                set_filter_value(config, specs[key], value)
        except Exception:
            drawable_filter.delete()
            raise
        if "opacity" in params:
            drawable_filter.set_opacity(float(params["opacity"]))
        if params.get("blend_mode"):
            drawable_filter.set_blend_mode(enum_member(Gimp.LayerMode, str(params["blend_mode"])))
        drawable_filter.update()

        applied = {spec.name: serialize_value(config.get_property(spec.name)) for spec in specs.values()}
        if params.get("merge", True):
            drawable.merge_filter(drawable_filter)
            filter_id = None
        else:
            drawable.append_filter(drawable_filter)
            filter_id = drawable_filter.get_id()
        flush_displays()
        return {
            "success": True,
            "drawable_id": drawable.get_id(),
            "operation": operation,
            "merged": filter_id is None,
            "filter_id": filter_id,
            "properties": applied,
        }

    def open_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = _require_abs_path(params, "path", must_exist=True)
        proc = lookup_procedure("gimp-file-load")
        config = proc.create_config()
        set_if_present(config, "run-mode", Gimp.RunMode.NONINTERACTIVE)
        set_if_present(config, "file", Gio.File.new_for_path(path))
        result = proc.run(config)
        return serialize_result(proc, result)

    def export_file(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        path = _require_abs_path(params, "path", must_exist=False)
        proc_name = params.get("procedure") or EXPORT_BY_EXTENSION.get(Path(path).suffix.lower())
        if not proc_name:
            raise BridgeError(-32602, f"No default export procedure for {Path(path).suffix!r}; pass procedure explicitly.")
        options = params.get("options") or {}
        if not isinstance(options, dict):
            raise BridgeError(-32602, "options must be an object")
        return export_image_to_path(image, path, str(proc_name), options)

    def fill_rect(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        drawable = drawable_from_id(_require_int(params, "drawable_id"))
        color = color_from_value(params.get("color", "black"))
        Gimp.context_set_foreground(color)
        image.select_rectangle(
            Gimp.ChannelOps.REPLACE,
            float(params.get("x", 0)),
            float(params.get("y", 0)),
            float(params.get("width", 1)),
            float(params.get("height", 1)),
        )
        Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
        clear_selection(image)
        flush_displays()
        return {"success": True, "image_id": image.get_id(), "drawable_id": drawable.get_id()}

    def fill_ellipse(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        drawable = drawable_from_id(_require_int(params, "drawable_id"))
        color = color_from_value(params.get("color", "black"))
        Gimp.context_set_foreground(color)
        image.select_ellipse(
            Gimp.ChannelOps.REPLACE,
            float(params.get("x", 0)),
            float(params.get("y", 0)),
            float(params.get("width", 1)),
            float(params.get("height", 1)),
        )
        Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
        clear_selection(image)
        flush_displays()
        return {"success": True, "image_id": image.get_id(), "drawable_id": drawable.get_id()}

    def add_text(self, params: dict[str, Any]) -> dict[str, Any]:
        image = image_from_id(_require_int(params, "image_id"))
        drawable = None
        if "drawable_id" in params and params["drawable_id"] is not None:
            drawable = drawable_from_id(int(params["drawable_id"]))
        text_value = _require_str(params, "text")
        color = color_from_value(params.get("color", "black"))
        Gimp.context_set_foreground(color)
        font_name = str(params.get("font", "Sans-serif"))
        font = Gimp.Font.get_by_name(font_name)
        if font is None:
            raise BridgeError(-32602, f"Unknown font: {font_name!r}")
        layer = Gimp.text_font(
            image,
            drawable,
            float(params.get("x", 0)),
            float(params.get("y", 0)),
            text_value,
            int(params.get("border", 0)),
            bool(params.get("antialias", True)),
            float(params.get("size", 32)),
            font,
        )
        flush_displays()
        return {"success": layer is not None, "text_layer": serialize_value(layer)}

    def draw_polyline(self, params: dict[str, Any]) -> dict[str, Any]:
        drawable = drawable_from_id(_require_int(params, "drawable_id"))

        points = params.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise BridgeError(-32602, "points must contain at least two [x, y] pairs")
        flat: list[float] = []
        for point in points:
            if not isinstance(point, list | tuple) or len(point) != 2:
                raise BridgeError(-32602, f"Invalid point: {point!r}")
            flat.extend([float(point[0]), float(point[1])])

        color = color_from_value(params.get("color", "black"))
        brush_size = float(params.get("brush_size", 8.0))
        Gimp.context_set_foreground(color)
        Gimp.context_set_brush_size(brush_size)
        width = int(safe_call(drawable, "get_width") or 0)
        height = int(safe_call(drawable, "get_height") or 0)
        radius = max(1, int(round(brush_size / 2.0)))
        radius_sq = radius * radius
        pixels: set[tuple[int, int]] = set()

        def paint_disk(cx: float, cy: float) -> None:
            x0 = max(0, int(round(cx)) - radius)
            x1 = min(width - 1, int(round(cx)) + radius)
            y0 = max(0, int(round(cy)) - radius)
            y1 = min(height - 1, int(round(cy)) + radius)
            for y in range(y0, y1 + 1):
                dy_sq = (y - cy) * (y - cy)
                for x in range(x0, x1 + 1):
                    if (x - cx) * (x - cx) + dy_sq <= radius_sq:
                        pixels.add((x, y))

        for start, end in zip(points, points[1:]):
            x0, y0 = float(start[0]), float(start[1])
            x1, y1 = float(end[0]), float(end[1])
            steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0))))
            for step in range(steps + 1):
                ratio = step / steps
                paint_disk(x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio)

        painted = 0
        for x, y in pixels:
            if drawable.set_pixel(x, y, color):
                painted += 1

        if painted and hasattr(drawable, "merge_shadow"):
            drawable.merge_shadow(True)
        if hasattr(drawable, "update"):
            drawable.update(0, 0, width, height)
        return {"success": painted > 0, "drawable_id": drawable.get_id(), "painted_pixels": painted, "point_count": len(points)}

    def batch(self, params: dict[str, Any]) -> dict[str, Any]:
        operations = params.get("operations")
        if not isinstance(operations, list):
            raise BridgeError(-32602, "operations must be an array")
        results = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise BridgeError(-32602, f"operation {index} must be an object")
            method = operation.get("method")
            if method not in self.handlers or method == "batch":
                raise BridgeError(-32602, f"operation {index} has unsupported method: {method}")
            results.append({"method": method, "result": self.handlers[method](operation.get("params") or {})})
        return {"results": results}


class BridgeError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def lookup_procedure(name: str):
    proc = Gimp.get_pdb().lookup_procedure(name)
    if proc is None:
        raise BridgeError(-32602, f"Unknown GIMP PDB procedure: {name}")
    return proc


def describe_procedure(proc) -> dict[str, Any]:
    data = summarize_procedure(proc)
    data["arguments"] = [describe_param_spec(arg) for arg in proc.get_arguments()]
    data["return_values"] = [describe_param_spec(ret) for ret in proc.get_return_values()]
    return data


def summarize_procedure(proc) -> dict[str, Any]:
    name = getattr(proc, "get_name", lambda: None)()
    return {
        "name": name,
        "type": enum_name(getattr(proc, "get_proc_type", lambda: None)()),
        "is_internal": bool(getattr(proc, "is_internal", lambda: False)()),
        "help": safe_call(proc, "get_help"),
        "help_id": safe_call(proc, "get_help_id"),
        "attribution": safe_call(proc, "get_attribution"),
    }


def describe_param_spec(spec) -> dict[str, Any]:
    value_type = getattr(spec, "value_type", None)
    owner_type = getattr(spec, "owner_type", None)
    default = getattr(spec, "default_value", None)
    data = {
        "name": getattr(spec, "name", None),
        "nick": getattr(spec, "nick", None),
        "blurb": getattr(spec, "blurb", None),
        "value_type": getattr(value_type, "name", str(value_type)),
        "owner_type": getattr(owner_type, "name", str(owner_type)),
        "default": serialize_value(default),
        "flags": int(getattr(spec, "flags", 0)),
        "param_spec_type": type(spec).__name__,
    }
    for bound in ("minimum", "maximum"):
        if hasattr(spec, bound):
            data[bound] = serialize_value(getattr(spec, bound))
    choices = enum_choices(spec)
    if choices:
        data["choices"] = choices
    return data


def enum_choices(spec) -> list[str]:
    if type(spec).__name__ == "ParamChoice" and hasattr(Gimp, "param_spec_choice_get_choice"):
        try:
            return list(Gimp.param_spec_choice_get_choice(spec).list_nicks())
        except Exception:
            return []
    default = getattr(spec, "default_value", None)
    if not type(spec).__name__.lower().endswith("enum") or default is None:
        return []
    values = getattr(type(default), "__enum_values__", {})
    return [str(member.value_nick) for member in values.values()]


def apply_config_args(proc, config, args: dict[str, Any]) -> None:
    specs = {spec.name: spec for spec in proc.get_arguments()}
    for key, value in args.items():
        if key not in specs:
            raise BridgeError(-32602, f"{proc.get_name()} has no argument named {key!r}")
        set_config_value(config, specs[key], value)


def set_if_present(config, name: str, value: Any) -> None:
    if config.find_property(name) is not None:
        config.set_property(name, value)


def set_config_value(config, spec, value: Any) -> None:
    type_name = getattr(getattr(spec, "value_type", None), "name", "").lower()
    param_type = type(spec).__name__.lower()

    value_type_name = getattr(getattr(spec, "value_type", None), "name", "")

    if value is None:
        config.set_property(spec.name, None)
        return

    if "runmode" in type_name and isinstance(value, str):
        value = enum_member(Gimp.RunMode, value)
    elif param_type.endswith("enum") and isinstance(value, str):
        enum_cls = getattr(Gimp, value_type_name.removeprefix("Gimp"), None)
        if enum_cls is not None:
            value = enum_member(enum_cls, value)
    elif type_name == "gimpimage":
        value = image_from_id(extract_id(value))
    elif type_name in {"gimplayer", "gimptextlayer", "gimpvectorlayer", "gimpgrouplayer", "gimplinklayer"}:
        value = Gimp.Layer.get_by_id(extract_id(value))
    elif type_name == "gimpdrawable":
        value = Gimp.Drawable.get_by_id(extract_id(value))
    elif type_name == "gimpitem":
        value = Gimp.Item.get_by_id(extract_id(value))
    elif type_name == "gimpdisplay":
        value = Gimp.Display.get_by_id(extract_id(value))
    elif type_name == "gimpfont" and isinstance(value, str):
        font_name = value
        value = Gimp.Font.get_by_name(font_name)
        if value is None:
            raise BridgeError(-32602, f"Unknown font: {font_name!r}")
    elif "geglcolor" in type_name:
        value = color_from_value(value)
    elif "giofile" in type_name or param_type.endswith("file"):
        value = Gio.File.new_for_path(str(value))
    elif type_name in {"gimpdoublearray", "gimpint32array"}:
        value = gimp_array_from_list(spec, value)
    elif isinstance(value, list) and hasattr(config, "set_core_object_array"):
        try:
            config.set_core_object_array(spec.name, [object_from_ref(item) for item in value])
            return
        except Exception:
            pass

    config.set_property(spec.name, value)


def gimp_array_from_list(spec, value: Any):
    if not isinstance(value, (list, tuple)):
        raise BridgeError(-32602, f"{spec.name} expects an array of numbers, got {value!r}")
    gvalue = GObject.Value()
    gvalue.init(spec.value_type)
    # Setting the GValue itself on the config segfaults GIMP 3.2; pass the boxed array instead.
    if spec.value_type.name == "GimpDoubleArray":
        Gimp.value_set_double_array(gvalue, [float(item) for item in value])
    else:
        Gimp.value_set_int32_array(gvalue, [int(item) for item in value])
    return gvalue.get_boxed()


def set_filter_value(config, spec, value: Any) -> None:
    type_name = getattr(getattr(spec, "value_type", None), "name", "").lower()
    if "geglcolor" in type_name:
        value = color_from_value(value)
    elif isinstance(value, str) and type(spec).__name__.lower().endswith("enum"):
        value = filter_enum_member(spec, value)
    elif isinstance(value, int) and type_name in {"gdouble", "gfloat"}:
        value = float(value)
    if type(spec).__name__ == "ParamChoice":
        choices = enum_choices(spec)
        if choices and value not in choices:
            # GIMP silently falls back to the default for unknown choices; fail loudly instead.
            raise BridgeError(-32602, f"Unknown value {value!r} for {spec.name}; choices: {choices}")
    minimum, maximum = getattr(spec, "minimum", None), getattr(spec, "maximum", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and minimum is not None and maximum is not None:
        if not minimum <= value <= maximum:
            raise BridgeError(-32602, f"{spec.name}={value} is outside [{minimum}, {maximum}]")
    config.set_property(spec.name, value)


def filter_enum_member(spec, value: str):
    wanted = value.replace("_", "-").lower()
    for member in getattr(type(spec.default_value), "__enum_values__", {}).values():
        if wanted in {str(member.value_nick).lower(), str(member.value_name).replace("_", "-").lower()}:
            return member
    raise BridgeError(-32602, f"Unknown value {value!r} for {spec.name}; choices: {enum_choices(spec)}")


def object_from_ref(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    kind = str(value.get("type", "")).lower()
    ref_id = extract_id(value)
    if kind == "image":
        return image_from_id(ref_id)
    if kind == "layer":
        return Gimp.Layer.get_by_id(ref_id)
    if kind == "drawable":
        return Gimp.Drawable.get_by_id(ref_id)
    if kind == "display":
        return Gimp.Display.get_by_id(ref_id)
    return Gimp.Item.get_by_id(ref_id)


def current_image():
    images = list_open_images()
    if not images:
        raise BridgeError(-32004, "No images are currently open in GIMP")
    return images[0]


def scaled_dimensions(width: int, height: int, max_width: int | None, max_height: int | None) -> tuple[int, int]:
    if max_width is None and max_height is None:
        return width, height
    if max_width is not None and max_width <= 0:
        raise BridgeError(-32602, "max_width must be positive")
    if max_height is not None and max_height <= 0:
        raise BridgeError(-32602, "max_height must be positive")

    scale = 1.0
    if max_width is not None:
        scale = min(scale, max_width / width)
    if max_height is not None:
        scale = min(scale, max_height / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def export_image_to_path(image, path: str, proc_name: str, options: dict[str, Any]) -> dict[str, Any]:
    proc = lookup_procedure(proc_name)
    config = proc.create_config()
    set_if_present(config, "run-mode", Gimp.RunMode.NONINTERACTIVE)
    set_if_present(config, "image", image)
    set_if_present(config, "file", Gio.File.new_for_path(path))
    apply_config_args(proc, config, options)
    result = proc.run(config)
    return serialize_result(proc, result)


def color_from_value(value: Any):
    if isinstance(value, str):
        return Gegl.Color.new(value)
    if isinstance(value, dict):
        if "css" in value:
            return Gegl.Color.new(str(value["css"]))
        channels = [value.get(name) for name in ("r", "g", "b", "a")]
    elif isinstance(value, (list, tuple)):
        channels = list(value)
    else:
        raise BridgeError(-32602, f"Expected CSS color, channel array, or color object, got {value!r}")

    if len(channels) == 3:
        channels.append(1.0)
    if len(channels) != 4:
        raise BridgeError(-32602, "Color channels must be [r, g, b] or [r, g, b, a]")

    normalized = [float(channel) for channel in channels]
    if any(channel > 1.0 for channel in normalized):
        normalized = [channel / 255.0 if index < 3 else channel for index, channel in enumerate(normalized)]

    color = Gegl.Color.new("black")
    color.set_rgba(*normalized)
    return color


def serialize_color(color: Any) -> dict[str, Any] | None:
    if color is None:
        return None
    data: dict[str, Any] = {"object": str(color)}
    if hasattr(color, "get_rgba"):
        try:
            rgba = color.get_rgba()
            if rgba is not None:
                data["rgba"] = [float(channel) for channel in rgba]
                data["rgba_255"] = [round(float(channel) * 255) if index < 3 else float(channel) for index, channel in enumerate(rgba)]
        except Exception as exc:
            data["rgba_error"] = str(exc)
    return data


def serialize_resource(resource: Any) -> dict[str, Any] | None:
    if resource is None:
        return None
    data = {"type": type(resource).__name__, "value": str(resource)}
    if hasattr(resource, "get_id"):
        try:
            data["id"] = resource.get_id()
        except Exception:
            pass
    if hasattr(resource, "get_name"):
        try:
            data["name"] = resource.get_name()
        except Exception:
            pass
    return data


def image_from_id(image_id: int):
    image = Gimp.Image.get_by_id(image_id)
    if image is None or not image.is_valid():
        raise BridgeError(-32602, f"Invalid image ID: {image_id}")
    return image


def layer_from_id(layer_id: int):
    layer = Gimp.Layer.get_by_id(layer_id)
    if layer is None or not layer.is_valid():
        raise BridgeError(-32602, f"Invalid layer ID: {layer_id}")
    return layer


def drawable_from_id(drawable_id: int):
    drawable = Gimp.Drawable.get_by_id(drawable_id)
    if drawable is None or not drawable.is_valid():
        raise BridgeError(-32602, f"Invalid drawable ID: {drawable_id}")
    return drawable


def clear_selection(image) -> None:
    if hasattr(Gimp, "Selection") and hasattr(Gimp.Selection, "none"):
        Gimp.Selection.none(image)
    elif hasattr(image, "select_none"):
        image.select_none()


def flush_displays() -> None:
    if hasattr(Gimp, "displays_flush"):
        Gimp.displays_flush()


def describe_image(image) -> dict[str, Any]:
    layers = safe_list_items(image, "list_layers", "get_layers")
    channels = safe_list_items(image, "list_channels", "get_channels")
    selected_drawables = safe_list_items(image, "list_selected_drawables", "get_selected_drawables")
    return {
        "id": image.get_id(),
        "name": safe_call(image, "get_name"),
        "file": serialize_value(safe_call(image, "get_file")),
        "width": safe_call(image, "get_width"),
        "height": safe_call(image, "get_height"),
        "base_type": enum_name(safe_call(image, "get_base_type")),
        "layers": [describe_item(layer) for layer in layers],
        "channels": [describe_item(channel) for channel in channels],
        "selected_drawables": [describe_item(drawable) for drawable in selected_drawables],
    }


def list_open_images() -> list[Any]:
    if hasattr(Gimp, "list_images"):
        return list(Gimp.list_images())
    return list(Gimp.get_images())


def safe_list_items(obj: Any, *methods: str) -> list[Any]:
    for method in methods:
        if hasattr(obj, method):
            try:
                return list(getattr(obj, method)())
            except Exception:
                pass
    return []


def describe_item(item) -> dict[str, Any]:
    return {
        "id": item.get_id(),
        "name": safe_call(item, "get_name"),
        "type": type(item).__name__,
        "visible": safe_call(item, "get_visible"),
        "width": safe_call(item, "get_width"),
        "height": safe_call(item, "get_height"),
        "offsets": safe_call(item, "get_offsets"),
    }


def serialize_result(proc, result) -> dict[str, Any]:
    values = []
    status = None
    try:
        status = result.index(0)
    except Exception:
        pass

    return_specs = list(proc.get_return_values())
    for index, spec in enumerate(return_specs, start=1):
        try:
            # pygobject can't unbox object/color arrays via index(); ValueArray has typed getters for them.
            if type(spec).__name__ == "ParamCoreObjectArray":
                raw = list(result.get_core_object_array(index))
            elif type(spec).__name__ == "ParamColorArray":
                raw = list(result.get_color_array(index))
            else:
                raw = result.index(index)
        except Exception as exc:
            values.append({"name": spec.name, "error": str(exc)})
            continue
        try:
            values.append({"name": spec.name, "value": serialize_value(raw)})
        except Exception as exc:
            values.append({"name": spec.name, "error": f"could not serialize {type(raw).__name__}: {exc}"})

    return {
        "procedure": proc.get_name(),
        "status": serialize_value(status),
        "success": is_success_status(status),
        "return_values": values,
        "last_error": safe_call(Gimp.get_pdb(), "get_last_error"),
    }


def serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [serialize_value(item) for item in value]
    if isinstance(value, Gegl.Color):
        return serialize_color(value)
    if isinstance(value, Gimp.DoubleArray):
        return list(Gimp.double_array_get_values(value))
    if isinstance(value, Gimp.Int32Array):
        return list(Gimp.int32_array_get_values(value))
    if hasattr(value, "get_path"):
        try:
            return value.get_path()
        except Exception:
            pass
    if hasattr(value, "get_id"):
        data = {"id": value.get_id(), "type": type(value).__name__}
        name = safe_call(value, "get_name")
        if name is not None:
            data["name"] = name
        return data
    name = enum_name(value)
    if name is not None:
        return name
    return str(value)


def enum_name(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value_name"):
        return str(value.value_name).split("-")[-1].upper()
    if hasattr(value, "name"):
        return str(value.name)
    return None


def enum_member(enum_cls, value: str):
    normalized = value.replace("-", "_").upper()
    if hasattr(enum_cls, normalized):
        return getattr(enum_cls, normalized)
    raise BridgeError(-32602, f"Unknown {enum_cls.__name__} value: {value}")


def is_success_status(status: Any) -> bool:
    if enum_name(status) == "SUCCESS":
        return True
    try:
        return int(status) == int(Gimp.PDBStatusType.SUCCESS)
    except Exception:
        return False


def extract_id(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except Exception as exc:
        raise BridgeError(-32602, f"Expected object reference ID, got {value!r}") from exc


def safe_call(obj: Any, method: str) -> Any:
    try:
        return getattr(obj, method)()
    except Exception:
        return None


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise BridgeError(-32602, f"Missing required string: {key}")
    return value


def _require_int(params: dict[str, Any], key: str) -> int:
    try:
        return int(params[key])
    except Exception as exc:
        raise BridgeError(-32602, f"Missing required integer: {key}") from exc


def _require_abs_path(params: dict[str, Any], key: str, must_exist: bool) -> str:
    value = _require_str(params, key)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BridgeError(-32602, f"{key} must be an absolute path")
    if must_exist and not path.exists():
        raise BridgeError(-32602, f"{key} does not exist: {path}")
    return str(path)


Gimp.main(GimpMcpBridgePlugin.__gtype__, sys.argv)
